local addonName, ns = ...

ns.NAME = addonName
ns.VERSION = 1
-- Each event is in the file twice (the cache and the export): a full 8-character account at 500
-- measures 2.9 MB against the pull's 5 MB cap (addon_test.lua).
ns.EVENT_CAP = 500
ns.FAULT_CAP = 20
-- Totals of older sessions have been pulled long before; keeping them only bloats the file.
ns.TOTALS_KEEP_SECONDS = 14 * 24 * 3600

local handlers = {}
local frame = CreateFrame("Frame")

function ns.fault(where, err)
  local faults = PinkasDB and PinkasDB.faults
  if not faults then
    return
  end

  table.insert(faults, { at = time(), where = where, err = tostring(err) })
  while #faults > ns.FAULT_CAP do
    table.remove(faults, 1)
  end
end

function ns.on(event, fn)
  if not handlers[event] then
    handlers[event] = {}
    -- Forever blocks some events for addons (the combat log); one refusal must not stop the rest.
    local ok, err = pcall(frame.RegisterEvent, frame, event)
    if not ok then
      ns.fault("register " .. event, err)
    end
  end
  table.insert(handlers[event], fn)
end

-- Until PLAYER_LOGIN there is no character to record into.
local BEFORE_LOGIN = { ADDON_LOADED = true, PLAYER_LOGIN = true }

frame:SetScript("OnEvent", function(_, event, ...)
  if not ns.char and not BEFORE_LOGIN[event] then
    return
  end
  for _, fn in ipairs(handlers[event]) do
    local ok, err = pcall(fn, ...)
    if not ok then
      ns.fault(event, err)
    end
  end
end)

-- A secret value (Midnight) refuses arithmetic; storing one would break the SavedVariables write.
function ns.plain(value)
  if type(value) ~= "number" then
    return nil
  end
  if issecretvalue and issecretvalue(value) then
    return nil
  end
  local ok = pcall(function()
    return value + 0
  end)

  return ok and value or nil
end

function ns.addEvent(kind, value)
  local c = ns.char
  local id = c.nextEventId
  c.nextEventId = id + 1
  table.insert(c.events, { id = id, at = time(), kind = kind, value = value })
  while #c.events > ns.EVENT_CAP do
    table.remove(c.events, 1)
  end
end

-- Session totals are keyed like their database rows: (session, kind, key).
function ns.total(kind, key, update)
  local c = ns.char
  -- Events can arrive between PLAYER_LOGIN and the first PLAYER_ENTERING_WORLD.
  if not c.session then
    return
  end
  local session = c.session.started_at
  local id = session .. "|" .. kind .. "|" .. key
  local row = c.totals[id]
  if not row then
    row = { session = session, kind = kind, key = key, value = {} }
    c.totals[id] = row
  end
  update(row.value)
end

function ns.add(t, field, amount)
  t[field] = (t[field] or 0) + amount
end

-- "Name" or "Name-Realm" → the account's character key, or nil for anyone else.
function ns.ownKey(name)
  if type(name) ~= "string" or name == "" then
    return nil
  end
  local short, realm = name:match("^([^-]+)-(.+)$")
  local key = (realm or ns.char.realm) .. "/" .. (short or name)

  return PinkasDB.chars[key] and key or nil
end

ns.on("ADDON_LOADED", function(name)
  if name ~= ns.NAME then
    return
  end
  PinkasDB = PinkasDB or {}
  PinkasDB.v = ns.VERSION
  PinkasDB.routes = PinkasDB.routes or {}
  PinkasDB.chars = PinkasDB.chars or {}
  PinkasDB.faults = PinkasDB.faults or {}
end)

ns.on("PLAYER_LOGIN", function()
  local name, realm = UnitName("player"), GetRealmName()
  ns.key = realm .. "/" .. name
  local c = PinkasDB.chars[ns.key] or {}
  PinkasDB.chars[ns.key] = c
  c.name, c.realm = name, realm
  c.events = c.events or {}
  c.totals = c.totals or {}
  c.nextEventId = c.nextEventId or 1
  ns.char = c
end)

-- PLAYER_LOGIN fires on every /reload too; only isInitialLogin marks a new session.
ns.on("PLAYER_ENTERING_WORLD", function(isInitialLogin)
  local c = ns.char
  ns.leaving = false
  if isInitialLogin or not c.session then
    c.session = { started_at = time(), lastMeterId = 0 }
    local cutoff = time() - ns.TOTALS_KEEP_SECONDS
    for id, row in pairs(c.totals) do
      if row.session < cutoff then
        c.totals[id] = nil
      end
    end
  end
end)

-- Leaving the world (logout, loading screen) empties unit data before the cache could notice.
ns.on("PLAYER_LEAVING_WORLD", function()
  ns.leaving = true
end)
