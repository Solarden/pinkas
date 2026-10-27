local addonName, ns = ...

ns.NAME = addonName
-- Two versions: the saved data's own layout, and the export contract the pull parses.
ns.DB_VERSION = 1
ns.EXPORT_VERSION = 3
-- Each event is in the file twice (the cache and the export): a full 8-character account at 500
-- measures 3.7 MB against the pull's 5 MB cap (addon_test.lua).
ns.EVENT_CAP = 500
ns.FAULT_CAP = 20
-- Totals of older sessions have been pulled long before; keeping them only bloats the file.
ns.TOTALS_KEEP_SECONDS = 14 * 24 * 3600

local handlers = {}
local units = {}
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

-- `unit` has the game deliver a UNIT_* event for that unit only, instead of for everyone nearby.
-- The filter is per event, so every handler of the event must ask for the same one.
function ns.on(event, fn, unit)
  if not handlers[event] then
    handlers[event] = {}
    units[event] = unit
    -- Forever blocks some events for addons (the combat log); one refusal must not stop the rest.
    -- No trailing nil: RegisterEvent takes the name alone.
    local ok, err
    if unit then
      ok, err = pcall(frame.RegisterUnitEvent, frame, event, unit)
    else
      ok, err = pcall(frame.RegisterEvent, frame, event)
    end
    if not ok then
      ns.fault("register " .. event, err)
    end
  end
  assert(units[event] == unit, event .. " is already registered with another unit filter")
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

-- For an API that returns a number or nil: nil can mean "none" (no rested XP), a secret means
-- "unknown right now", and the two must not be confused.
function ns.isSecret(value)
  return value ~= nil and ns.plain(value) == nil
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

-- The server takes a recipient in any case ("altname"), and writes another realm without its
-- spaces ("Alt-OtherRealm" for "Other Realm"), so keys are compared in that form.
local function plainKey(key)
  return (key:lower():gsub("[%s%-]", ""))
end

-- "Name" or "Name-Realm" → the account's character key, or nil for anyone else.
function ns.ownKey(name)
  if type(name) ~= "string" or name == "" then
    return nil
  end
  local short, realm = name:match("^([^-]+)-(.+)$")
  local key = (realm or ns.char.realm) .. "/" .. (short or name)
  if PinkasDB.chars[key] then
    return key
  end
  local wanted = plainKey(key)
  for known in pairs(PinkasDB.chars) do
    if plainKey(known) == wanted then
      return known
    end
  end

  return nil
end

ns.on("ADDON_LOADED", function(name)
  if name ~= ns.NAME then
    return
  end
  -- A damaged file must not stop the addon: a part that isn't a table starts empty.
  if type(PinkasDB) ~= "table" then
    PinkasDB = {}
  end
  -- A DB_VERSION bump needs its migration here, before the stamp.
  PinkasDB.v = ns.DB_VERSION
  for _, part in ipairs({ "routes", "chars", "faults" }) do
    if type(PinkasDB[part]) ~= "table" then
      PinkasDB[part] = {}
    end
  end
end)

ns.on("PLAYER_LOGIN", function()
  local name, realm = UnitName("player"), GetRealmName()
  ns.key = realm .. "/" .. name
  local c = type(PinkasDB.chars[ns.key]) == "table" and PinkasDB.chars[ns.key] or {}
  PinkasDB.chars[ns.key] = c
  c.name, c.realm = name, realm
  c.events = type(c.events) == "table" and c.events or {}
  c.totals = type(c.totals) == "table" and c.totals or {}
  c.nextEventId = type(c.nextEventId) == "number" and c.nextEventId or 1
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
      -- A damaged row goes too: it would otherwise break this loop and every export.
      if type(row) ~= "table" or type(row.session) ~= "number" or row.session < cutoff then
        c.totals[id] = nil
      end
    end
  end
end)

-- Leaving the world (logout, loading screen) empties unit data before the cache could notice.
ns.on("PLAYER_LEAVING_WORLD", function()
  ns.leaving = true
end)
