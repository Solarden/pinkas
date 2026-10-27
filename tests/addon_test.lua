-- Stub harness for the Pinkas addon: the WoW API shapes follow the beta captures, with made-up data.
-- Run from the repo root: lua tests/addon_test.lua
local ADDON = "addon/Pinkas/"

local function decodeJson(s)
  local pos = 1
  local value

  local function ws()
    pos = s:find("[^ \t\r\n]", pos) or #s + 1
  end

  local function str()
    local out = {}
    pos = pos + 1
    while true do
      local c = s:sub(pos, pos)
      if c == '"' then
        pos = pos + 1
        return table.concat(out)
      end
      if c == "\\" then
        local e = s:sub(pos + 1, pos + 1)
        local map = { ['"'] = '"', ["\\"] = "\\", n = "\n", r = "\r", t = "\t" }
        if e == "u" then
          out[#out + 1] = string.char(tonumber(s:sub(pos + 2, pos + 5), 16))
          pos = pos + 6
        else
          out[#out + 1] = map[e]
          pos = pos + 2
        end
      else
        out[#out + 1] = c
        pos = pos + 1
      end
    end
  end

  value = function()
    ws()
    local c = s:sub(pos, pos)
    if c == "{" then
      local t = {}
      pos = pos + 1
      ws()
      if s:sub(pos, pos) == "}" then
        pos = pos + 1
        return t
      end
      while true do
        ws()
        local k = str()
        ws()
        pos = pos + 1
        t[k] = value()
        ws()
        local sep = s:sub(pos, pos)
        pos = pos + 1
        if sep == "}" then
          return t
        end
      end
    elseif c == "[" then
      local t = { __array = true }
      pos = pos + 1
      ws()
      if s:sub(pos, pos) == "]" then
        pos = pos + 1
        return t
      end
      while true do
        t[#t + 1] = value()
        ws()
        local sep = s:sub(pos, pos)
        pos = pos + 1
        if sep == "]" then
          return t
        end
      end
    elseif c == '"' then
      return str()
    elseif s:sub(pos, pos + 3) == "null" then
      pos = pos + 4
      return "<null>"
    elseif s:sub(pos, pos + 3) == "true" then
      pos = pos + 4
      return true
    elseif s:sub(pos, pos + 4) == "false" then
      pos = pos + 5
      return false
    end
    local num = s:match("^-?[%d%.eE+-]+", pos)
    pos = pos + #num
    return tonumber(num)
  end

  local result = value()
  ws()
  assert(pos == #s + 1, "trailing JSON at " .. pos)

  return result
end

-- Midnight's secret values look like numbers; only issecretvalue tells them apart.
local SECRET = 4242424242

-- World: one fresh client per test, holding the stub state the addon reads.
local world

local function newWorld()
  world = {
    now = 1792540000,
    name = "Testchar",
    realm = "Testrealm",
    level = 10,
    xp = 1664,
    xpMax = 7600,
    rested = 892,
    resting = true,
    money = 1000,
    zone = "Orgrimmar",
    map = 1454,
    x = 0.54,
    y = 0.68,
    inInstance = false,
    inCombat = false,
    bags = { [0] = {} },
    bank = {},
    inbox = {},
    sendItems = {},
    professions = { { "Skinning", 75, 75, 393 } },
    meterSessions = {},
    meterSources = {},
    recap = nil,
    recapLink = nil,
    frames = {},
    timers = {},
    hooks = {},
    played = nil,
    printed = {},
  }
end

local env

local function makeEnv()
  local e = setmetatable({}, { __index = _G })
  e._G = e
  e.time = function()
    return world.now
  end
  e.print = function(text)
    world.printed[#world.printed + 1] = text
  end
  e.CreateFrame = function()
    local f = { events = {} }
    function f:RegisterEvent(...)
      -- The client's RegisterEvent takes the event name alone.
      assert(select("#", ...) == 1, "RegisterEvent takes only the event name")
      local ev = ...
      if ev == "COMBAT_LOG_EVENT_UNFILTERED" then
        error("blocked")
      end
      self.events[ev] = true
    end
    -- The game delivers the event only for that unit, the first argument.
    function f:RegisterUnitEvent(ev, unit)
      self.events[ev] = unit
    end
    function f:SetScript(_, fn)
      self.onEvent = fn
    end
    function f.SetSize() end
    function f.SetPoint() end
    function f.SetText() end
    world.frames[#world.frames + 1] = f
    return f
  end
  e.hooksecurefunc = function(name, fn)
    world.hooks[name] = world.hooks[name] or {}
    table.insert(world.hooks[name], fn)
  end
  e.C_Timer = {
    After = function(_, fn)
      table.insert(world.timers, fn)
    end,
  }
  e.UnitName = function()
    return world.name
  end
  e.GetRealmName = function()
    return world.realm
  end
  e.GetBuildInfo = function()
    return "1.60.1", "70450", "Oct 1 2026", 16001
  end
  e.UnitClass = function()
    return "Rogue", "ROGUE"
  end
  e.UnitRace = function()
    return "Orc", "Orc"
  end
  e.UnitLevel = function()
    return world.level
  end
  e.UnitXP = function()
    return world.xp
  end
  e.UnitXPMax = function()
    return world.xpMax
  end
  e.GetXPExhaustion = function()
    return world.rested
  end
  e.IsResting = function()
    return world.resting
  end
  e.GetMoney = function()
    return world.money
  end
  e.GetZoneText = function()
    return world.zone
  end
  e.IsInInstance = function()
    return world.inInstance
  end
  e.UnitAffectingCombat = function()
    return world.inCombat
  end
  e.C_Map = {
    GetBestMapForUnit = function()
      return world.map
    end,
    GetPlayerMapPosition = function()
      if world.inInstance then
        return nil
      end
      return { x = world.x, y = world.y }
    end,
  }
  e.GetProfessions = function()
    local ids = {}
    for i = 1, #world.professions do
      ids[i] = i
    end
    return ids[1], ids[2], nil, nil, nil
  end
  e.GetProfessionInfo = function(i)
    local p = world.professions[i]
    return p[1], 0, p[2], p[3], 0, 0, p[4]
  end
  e.NUM_BAG_SLOTS = 4
  e.ATTACHMENTS_MAX_SEND = 12
  e.ATTACHMENTS_MAX_RECEIVE = 12
  e.Enum = { BagIndex = { Keyring = -1, CharacterBankTab_1 = 6, CharacterBankTab_2 = 7 } }
  e.C_Container = {
    GetContainerNumSlots = function(bag)
      local slots = world.bags[bag] or world.bank[bag]
      return slots and #slots or 0
    end,
    GetContainerItemInfo = function(bag, slot)
      local slots = world.bags[bag] or world.bank[bag]
      -- `false` in a stub bag is an empty slot, which the game reads as nil.
      return slots and slots[slot] or nil
    end,
    PickupContainerItem = function(bag, slot)
      world.cursor = world.bags[bag][slot]
    end,
  }
  e.C_Item = {
    GetItemInfoInstant = function(id)
      local classes = { [2318] = { 7, 6 }, [2589] = { 7, 5 }, [2447] = { 7, 9 } }
      local c = classes[id] or { 0, 0 }
      return id, nil, nil, nil, nil, c[1], c[2]
    end,
  }
  e.ClickSendMailItemButton = function(i)
    world.sendItems[i] = world.cursor
    world.cursor.isLocked = true
  end
  e.ClearCursor = function()
    world.cursor = nil
  end
  e.GetSendMailItem = function(i)
    local it = world.sendItems[i]
    if it then
      return "Item " .. it.itemID, it.itemID, nil, it.stackCount
    end
  end
  e.SendMailNameEditBox = {
    SetText = function(_, v)
      world.mailTo = v
    end,
  }
  e.SendMailSubjectEditBox = {
    SetText = function(_, v)
      world.mailSubject = v
    end,
  }
  e.SendMailFrame = {}
  e.SlashCmdList = {}
  e.GetInboxNumItems = function()
    return #world.inbox
  end
  e.GetInboxHeaderInfo = function(i)
    local m = world.inbox[i]
    return nil, nil, m.sender, "subject", m.money
  end
  e.GetInboxItem = function(i, a)
    local it = world.inbox[i].items[a]
    if it then
      return "Item " .. it.id, it.id, nil, it.count
    end
  end
  e.RequestTimePlayed = function()
    world.playedRequested = true
  end
  e.ChatFrameUtil = {
    DisplayTimePlayed = function()
      world.playedShown = (world.playedShown or 0) + 1
    end,
  }
  e.C_DeathRecap = {
    GetRecapEvents = function()
      return world.recap
    end,
    GetRecapLink = function()
      return world.recapLink
    end,
  }
  e.C_DamageMeter = {
    GetAvailableCombatSessions = function()
      return world.meterSessions
    end,
    GetCombatSessionFromID = function(id, meterType)
      if world.meterUnserved and world.meterUnserved[meterType] then
        error("no such meter")
      end
      if world.inCombat or world.secret then
        return { combatSources = { { isLocalPlayer = true, totalAmount = SECRET } } }
      end
      local mine = (world.meterSources[id] or {})[meterType] or 0
      return {
        combatSources = {
          { isLocalPlayer = false, totalAmount = 999, name = "Someone Else" },
          { isLocalPlayer = true, totalAmount = mine },
        },
      }
    end,
  }
  e.bit = {
    band = function(a, b)
      local r, bitv = 0, 1
      while a > 0 and b > 0 do
        if a % 2 == 1 and b % 2 == 1 then
          r = r + bitv
        end
        a, b, bitv = math.floor(a / 2), math.floor(b / 2), bitv * 2
      end
      return r
    end,
  }
  e.RepairAllItems = function() end
  e.TakeTaxiNode = function() end
  e.SendMail = function() end
  -- Stand in for Midnight's secret values: the addon must never store one.
  e.issecretvalue = function(v)
    return v == SECRET
  end
  return e
end

-- The game calls a hooked global's hooks after the original.
local function callHooked(name, ...)
  for _, fn in ipairs(world.hooks[name] or {}) do
    fn(...)
  end
end

local function fire(event, ...)
  for _, f in ipairs(world.frames) do
    local filter = f.events[event]
    if filter and f.onEvent and (filter == true or filter == ...) then
      f.onEvent(f, event, ...)
    end
  end
end

local function runTimers()
  local pending = world.timers
  world.timers = {}
  for _, fn in ipairs(pending) do
    fn()
  end
end

local ns

-- `appRoutes`: a file to load in place of the shipped AppRoutes.lua, as the Windows app writes it.
local function load(savedDB, appRoutes)
  -- A /reload replaces the whole UI: the previous addon's frames, hooks and timers are gone.
  world.frames, world.hooks, world.timers = {}, {}, {}
  env = makeEnv()
  -- Blizzard's chat windows load before any addon, and each one showing System messages prints
  -- /played as ChatFrameMixin does.
  for _ = 1, 2 do
    local chat = env.CreateFrame()
    chat:RegisterEvent("TIME_PLAYED_MSG")
    chat:SetScript("OnEvent", function(self, _, total, level)
      env.ChatFrameUtil.DisplayTimePlayed(self, total, level)
    end)
  end
  env.PinkasDB = savedDB
  ns = {}
  local toc = assert(io.open(ADDON .. "Pinkas.toc")):read("a")
  for file in toc:gmatch("([%w_]+%.lua)") do
    local path = file == "AppRoutes.lua" and appRoutes or ADDON .. file
    local chunk = assert(loadfile(path, "t", env))
    chunk("Pinkas", ns)
  end
  fire("ADDON_LOADED", "Pinkas")
end

local function login(initial)
  fire("PLAYER_LOGIN")
  fire("PLAYER_ENTERING_WORLD", initial ~= false, initial == false)
end

local function logoutAndExport()
  -- A real logout empties the unit before PLAYER_LOGOUT (measured in the beta).
  fire("PLAYER_LEAVING_WORLD")
  world.xp, world.xpMax, world.money, world.bags = 0, 0, 0, { [0] = {} }
  fire("PLAYER_LOGOUT")
  return decodeJson(env.PinkasDB.export)
end

local function me(export)
  return export.chars["Testrealm/Testchar"]
end

local tests, failures = {}, 0
local function test(name, fn)
  tests[#tests + 1] = { name = name, fn = fn }
end

-- Sessions ---------------------------------------------------------------------------------------

test("a session starts on a real login and survives a reload", function()
  newWorld()
  load(nil)
  login(true)
  local started = env.PinkasDB.chars["Testrealm/Testchar"].session.started_at
  world.now = world.now + 600
  local db = env.PinkasDB
  load(db)
  fire("PLAYER_LOGIN")
  fire("PLAYER_ENTERING_WORLD", false, true)
  assert(env.PinkasDB.chars["Testrealm/Testchar"].session.started_at == started)
  load(db)
  login(true)
  assert(env.PinkasDB.chars["Testrealm/Testchar"].session.started_at == world.now)
end)

test("a real logout exports the cache, not the emptied unit", function()
  newWorld()
  world.bags = {
    [0] = {
      {
        itemID = 2318,
        stackCount = 11,
        itemName = "Light Leather",
        quality = 1,
        hyperlink = "|cffffffff|Hitem:2318::|h[Light Leather]|h|r",
      },
      false,
    },
    [1] = { false, false },
  }
  load(nil)
  login(true)
  local export = logoutAndExport()
  assert(export.v == 3 and export.build == "1.60.1.70450", "contract version and game build")
  local c = me(export)
  assert(c.xp == 1664 and c.xp_max == 7600 and c.money == 1000, "cache kept")
  assert(c.bags[1].id == 2318 and c.bags[1].count == 11 and c.bags[1].name == "Light Leather")
  assert(c.bags[1].quality == 1 and c.bags[1].class == 7 and c.bags[1].subclass == 6)
  assert(#c.bags == 1 and c.bag_slots == 4, "one item, four slots")
  assert(c.logout_at == world.now and c.rested == 892 and c.resting == true)
  assert(c.professions[1].skill_line == 393 and c.professions[1].rank == 75)
end)

test("a client without a build still writes the export", function()
  newWorld()
  load(nil)
  env.GetBuildInfo = function() end
  login(true)
  assert(logoutAndExport().build == "nil.nil")
  env.GetBuildInfo = nil
  env.PinkasDB.export = nil
  assert(logoutAndExport().build == "nil.nil", "nor without the function")
end)

test("every contract key is present, unknown ones as null", function()
  newWorld()
  world.rested = nil
  load(nil)
  login(true)
  local c = me(logoutAndExport())
  for _, key in ipairs({ "rested", "bank", "mailbox", "played", "class", "race", "x", "y" }) do
    assert(c[key] ~= nil, "missing key " .. key)
  end
  assert(c.rested == "<null>" and c.bank == "<null>" and c.mailbox == "<null>")
  assert(c.bags.__array and c.events.__array and c.totals.__array, "lists are arrays")
end)

-- JSON -------------------------------------------------------------------------------------------

test("JSON escapes quotes, control characters and keeps UTF-8 bytes", function()
  newWorld()
  load(nil)
  local s = ns.json({ a = 'q"\\\n\1', b = "Zoë", c = ns.array({}), d = {}, e = 1.5, f = 3, g = ns.NULL })
  local t = decodeJson(s)
  assert(t.a == 'q"\\\n\1' and t.b == "Zoë" and t.e == 1.5 and t.f == 3 and t.g == "<null>")
  assert(s:find('"c":%[%]') and s:find('"d":{}'), "empty array vs object")
end)

-- Routes -----------------------------------------------------------------------------------------

test("no route ships with a recipient, and a route without one is skipped", function()
  newWorld()
  world.bags = { [0] = { { itemID = 2589, stackCount = 20 } } }
  load(nil)
  login(true)
  ns.fillNextMail()
  assert(world.mailTo == nil and next(world.sendItems) == nil, "nothing filled without routes")
  env.SlashCmdList.PINKAS("route cloth Altname")
  ns.fillNextMail()
  assert(world.mailTo == "Altname" and world.sendItems[1].itemID == 2589)
  world.sendItems, world.mailTo = {}, nil
  world.bags = { [0] = {}, [5] = { { itemID = 2318, stackCount = 3 } } }
  env.SlashCmdList.PINKAS("route leather Altname")
  ns.fillNextMail()
  assert(world.sendItems[1] and world.sendItems[1].itemID == 2318, "the reagent bag is routed too")
  -- Fresh bags: the leather above is still locked as attached.
  world.sendItems, world.mailTo = {}, nil
  world.bags = { [0] = {}, [5] = { { itemID = 2318, stackCount = 3 } } }
  env.SlashCmdList.PINKAS("route leather testchar")
  ns.fillNextMail()
  assert(world.mailTo == nil and next(world.sendItems) == nil, "a route to this character, in any case, is skipped")
end)

-- tests/fixtures/fake_app_routes.lua is the app's writer run on these names (test_desktop.py).
local HOSTILE = {
  herb = '"]] os.exit() --',
  cloth = 'Line\nbreak "quoted" \\',
  leather = "Altname-Testrealm",
  ore = "Zo\195\171-OtherRealm",
}

test("the app's routes file holds hostile names as plain strings, and replaces /pinkas route", function()
  newWorld()
  world.bags = { [0] = {}, [5] = { { itemID = 2318, stackCount = 3 } } }
  load({ routes = { leather = "Stranger", enchanting = "Stranger" } }, "tests/fixtures/fake_app_routes.lua")
  login(true)
  for kind, name in pairs(HOSTILE) do
    assert(ns.appRoutes[kind] == name, kind .. " parsed back to another string")
  end
  ns.fillNextMail()
  assert(world.mailTo == "Altname" and world.sendItems[1].itemID == 2318, "this realm's alt by its name alone")
  assert(world.printed[#world.printed]:find("for Altname - check", 1, true), "and the chat line names it so too")
  local function said(text)
    for _, line in ipairs(world.printed) do
      if line:find(text, 1, true) then
        return true
      end
    end

    return false
  end
  env.SlashCmdList.PINKAS("routes")
  assert(said("leather -> Altname") and not said("Altname-Testrealm"), "/pinkas routes, as the mail has it")
  world.printed = {}
  env.SlashCmdList.PINKAS("")
  assert(said("set in the pinkas app") and not said("<Name>"), "the help points to the app")
  env.SlashCmdList.PINKAS("route leather Other")
  assert(env.PinkasDB.routes.leather == "Stranger", "/pinkas route says to use the app")
  local routes = logoutAndExport().routes
  assert(routes.leather == "Altname-Testrealm" and routes.enchanting == nil, "the export carries the app's routes")
end)

test("a route the app leaves off stays off, whatever /pinkas route set", function()
  newWorld()
  world.bags = { [0] = { { itemID = 2589, stackCount = 20 } } }
  local file = os.tmpname()
  local f = assert(io.open(file, "w"))
  f:write('local _, ns = ...\nns.appRoutes = {\n  herb = "Herbchar\\045Testrealm",\n}\n')
  f:close()
  load({ routes = { cloth = "Altname" } }, file)
  os.remove(file)
  login(true)
  ns.fillNextMail()
  assert(world.mailTo == nil and next(world.sendItems) == nil)
end)

test("a route that isn't a name is skipped, not an error", function()
  newWorld()
  world.bags = { [0] = { { itemID = 2447, stackCount = 5 }, { itemID = 2589, stackCount = 20 } } }
  load({ routes = { herb = 5, cloth = "Altname" } })
  login(true)
  ns.fillNextMail()
  assert(world.mailTo == "Altname" and world.sendItems[1].itemID == 2589, "the next route instead")
  env.SlashCmdList.PINKAS("routes")
  assert(world.printed[#world.printed - 4]:find("herb -> (off)", 1, true))
  env.PinkasDB.routes.gems = "Altname"
  local routes = logoutAndExport().routes
  assert(routes.cloth == "Altname" and routes.herb == nil and routes.gems == nil, "the export carries names only")
end)

-- Mail -------------------------------------------------------------------------------------------

test("mail is recorded only to the account's own characters", function()
  newWorld()
  load(nil)
  login(true)
  env.PinkasDB.chars["Testrealm/Altname"] = { name = "Altname", realm = "Testrealm" }
  world.sendItems = { { itemID = 2318, stackCount = 5 } }
  callHooked("SendMail", "Stranger")
  fire("MAIL_SEND_SUCCESS")
  callHooked("SendMail", "Altname")
  fire("MAIL_SEND_SUCCESS")
  local events = env.PinkasDB.chars["Testrealm/Testchar"].events
  assert(#events == 1 and events[1].kind == "mail" and events[1].value.to == "Testrealm/Altname")
  world.inbox = { { sender = "Stranger", money = 0, items = {} }, { sender = "Altname", money = 5, items = {} } }
  fire("MAIL_SHOW")
  fire("MAIL_INBOX_UPDATE")
  local mails = env.PinkasDB.chars["Testrealm/Testchar"].mailbox.mails
  assert(mails[1].from == "other" and mails[2].from == "Testrealm/Altname")
  callHooked("SendMail", "altname")
  fire("MAIL_SEND_SUCCESS")
  assert(events[2].value.to == "Testrealm/Altname", "a recipient typed in lower case is still an own alt")
  env.PinkasDB.chars["Other Realm/Farmer"] = { name = "Farmer", realm = "Other Realm" }
  world.inbox = { { sender = "Farmer-OtherRealm", money = 0, items = {} } }
  fire("MAIL_INBOX_UPDATE")
  local from = env.PinkasDB.chars["Testrealm/Testchar"].mailbox.mails[1].from
  assert(from == "Other Realm/Farmer", "another realm is written without its spaces")
end)

test("the inbox is read only at a mailbox, so an update elsewhere keeps the last capture", function()
  newWorld()
  load(nil)
  login(true)
  world.inbox = { { sender = "Stranger", money = 7, items = {} } }
  fire("MAIL_INBOX_UPDATE")
  assert(env.PinkasDB.chars["Testrealm/Testchar"].mailbox == nil, "no mailbox open")
  fire("MAIL_SHOW")
  fire("MAIL_INBOX_UPDATE")
  local captured = env.PinkasDB.chars["Testrealm/Testchar"].mailbox
  fire("MAIL_CLOSED")
  world.inbox = {}
  world.now = world.now + 60
  fire("MAIL_INBOX_UPDATE")

  assert(env.PinkasDB.chars["Testrealm/Testchar"].mailbox == captured and #captured.mails == 1)
end)

test("a secret XP read in combat keeps the cache and books no rested", function()
  newWorld()
  load(nil)
  login(true)
  local c = env.PinkasDB.chars["Testrealm/Testchar"]
  -- XP readable, rested secret.
  world.xp, world.rested = 1680, SECRET
  fire("PLAYER_XP_UPDATE")
  assert(c.xp == 1664 and c.rested == 892, "the last good values stay")
  world.xp, world.rested = 1700, 870
  fire("PLAYER_XP_UPDATE")
  local rested
  for _, row in pairs(c.totals) do
    if row.kind == "rested" then
      rested = row.value
    end
  end
  assert(rested and rested.xp == 36 and rested.rested_used == 22, "only what the readable values show")
end)

-- Deaths -----------------------------------------------------------------------------------------

local function deathKiller(recap)
  newWorld()
  load(nil)
  login(true)
  world.recap, world.recapLink = recap, "|cff71d5ff|Hdeath:7|h[You died.]|h"
  fire("PLAYER_DEAD")
  local events = env.PinkasDB.chars["Testrealm/Testchar"].events
  return events[#events].value
end

test("the killer is the killing blow: a mob, a player's pet, a fall", function()
  local mob = deathKiller({
    { event = "SWING_DAMAGE", overkill = -1, sourceName = "Wolf", sourceFlags = 0xA48, timestamp = 1 },
    { event = "SWING_DAMAGE", overkill = 3, sourceName = "Boar", sourceFlags = 0xA48, timestamp = 2 },
  })
  assert(mob.killer == "Boar" and mob.recap_id == 7 and mob.level == 10)
  local pet = deathKiller({
    { event = "SWING_DAMAGE", overkill = 0, sourceName = "Fluffy", sourceFlags = 0x1111, timestamp = 1 },
  })
  assert(pet.killer == "player", "a player's pet is never named")
  local unflagged = deathKiller({ { event = "SWING_DAMAGE", overkill = 0, sourceName = "Someone", timestamp = 1 } })
  assert(unflagged.killer == ns.NULL, "without flags the source could be a player, so no name is kept")
  local fall =
    deathKiller({ { event = "ENVIRONMENTAL_DAMAGE", overkill = 0, environmentalType = "Falling", timestamp = 1 } })
  assert(fall.killer == "Falling")
  newWorld()
  world.inInstance = true
  load(nil)
  login(true)
  fire("PLAYER_DEAD")
  local inside = env.PinkasDB.chars["Testrealm/Testchar"].events[1].value
  assert(inside.x == ns.NULL and inside.y == ns.NULL, "position keys kept as null in an instance")
end)

-- Money ------------------------------------------------------------------------------------------

local function goldBy()
  local out = {}
  for _, row in pairs(env.PinkasDB.chars["Testrealm/Testchar"].totals) do
    if row.kind == "gold" then
      out[row.key] = row.value
    end
  end
  return out
end

local function spend(amount)
  world.money = world.money + amount
  fire("PLAYER_MONEY")
end

test("money changes take their labels; a hook beats an open frame", function()
  newWorld()
  load(nil)
  login(true)
  fire("MERCHANT_SHOW")
  callHooked("RepairAllItems")
  spend(-25)
  spend(28)
  fire("MERCHANT_CLOSED")
  fire("LOOT_OPENED")
  spend(4)
  fire("LOOT_CLOSED")
  callHooked("TakeTaxiNode")
  spend(-110)
  callHooked("SendMail", "Stranger")
  spend(-30)
  spend(100000)
  local g = goldBy()
  assert(g.repair.out == 25 and g.vendor["in"] == 28 and g.loot["in"] == 4)
  assert(g.taxi.out == 110 and g.mail.out == 30 and g.other["in"] == 100000)
end)

test("a stale hook label expires instead of mislabeling later money", function()
  newWorld()
  load(nil)
  login(true)
  callHooked("TakeTaxiNode")
  world.now = world.now + 60
  spend(4)
  assert(goldBy().other["in"] == 4)
end)

-- Combat -----------------------------------------------------------------------------------------

local function combatTotals()
  for _, row in pairs(env.PinkasDB.chars["Testrealm/Testchar"].totals) do
    if row.kind == "combat" then
      return row.value
    end
  end
end

test("combat reads only the player's own row, never in combat, never twice", function()
  newWorld()
  load(nil)
  login(true)
  world.meterSessions = { { sessionID = 1, durationSeconds = 19 } }
  world.meterSources[1] = { [0] = 146, [2] = 0, [7] = 51, [8] = 0 }
  world.inCombat = true
  fire("ADDON_RESTRICTION_STATE_CHANGED", 0, 0)
  runTimers()
  assert(combatTotals() == nil, "no read in combat")
  world.inCombat = false
  fire("ADDON_RESTRICTION_STATE_CHANGED", 0, 0)
  runTimers()
  fire("ADDON_RESTRICTION_STATE_CHANGED", 0, 0)
  runTimers()
  local v = combatTotals()
  assert(v.damage == 146 and v.taken == 51 and v.fights == 1 and v.seconds == 19, "own row, once")
  assert(math.abs(v.best_dps - 146 / 19) < 1e-9)
end)

test("a meter the client doesn't serve counts as 0 instead of stalling every session", function()
  newWorld()
  load(nil)
  login(true)
  world.meterUnserved = { [8] = true }
  world.meterSessions = { { sessionID = 1, durationSeconds = 12 } }
  world.meterSources[1] = { [0] = 240 }
  fire("ADDON_RESTRICTION_STATE_CHANGED", 0, 0)
  runTimers()
  local v = combatTotals()
  assert(v and v.fights == 1 and v.damage == 240 and v.avoidable == 0)
end)

test("meter session ids restart at a real login", function()
  newWorld()
  load(nil)
  login(true)
  world.meterSessions = { { sessionID = 5, durationSeconds = 4 } }
  world.meterSources[5] = { [0] = 10 }
  fire("ADDON_RESTRICTION_STATE_CHANGED", 0, 0)
  runTimers()
  local db = env.PinkasDB
  world.now = world.now + 3600
  load(db)
  login(true)
  world.meterSessions = { { sessionID = 1, durationSeconds = 4 } }
  world.meterSources[1] = { [0] = 7 }
  fire("ADDON_RESTRICTION_STATE_CHANGED", 0, 0)
  runTimers()
  local found = false
  for _, row in pairs(env.PinkasDB.chars["Testrealm/Testchar"].totals) do
    if row.kind == "combat" and row.session == world.now then
      found = row.value.damage == 7
    end
  end
  assert(found, "session 1 after a relog is read")
end)

test("a secret meter value is never stored and the session is retried later", function()
  newWorld()
  load(nil)
  login(true)
  world.meterSessions = { { sessionID = 1, durationSeconds = 19 } }
  world.meterSources[1] = { [0] = 146 }
  world.secret = true
  fire("ADDON_RESTRICTION_STATE_CHANGED", 0, 0)
  runTimers()
  assert(combatTotals() == nil and env.PinkasDB.chars["Testrealm/Testchar"].session.lastMeterId == 0)
  world.secret = false
  fire("ADDON_RESTRICTION_STATE_CHANGED", 0, 0)
  runTimers()
  assert(combatTotals().damage == 146)
end)

test("an emptied unit on a loading screen never overwrites the cache", function()
  newWorld()
  load(nil)
  login(true)
  fire("PLAYER_LEAVING_WORLD")
  world.xp, world.xpMax, world.money = 0, 0, 0
  fire("PLAYER_XP_UPDATE")
  fire("PLAYER_MONEY")
  fire("BAG_UPDATE_DELAYED")
  local c = env.PinkasDB.chars["Testrealm/Testchar"]
  assert(c.xp == 1664 and c.xp_max == 7600 and c.money == 1000)
end)

test("meter sessions are read in id order even when listed out of order", function()
  newWorld()
  load(nil)
  login(true)
  world.meterSessions = { { sessionID = 214, durationSeconds = 27 }, { sessionID = 213, durationSeconds = 19 } }
  world.meterSources[213] = { [0] = 146 }
  world.meterSources[214] = { [0] = 23 }
  fire("ADDON_RESTRICTION_STATE_CHANGED", 0, 0)
  runTimers()
  local v = combatTotals()
  assert(v.fights == 2 and v.damage == 169, "both fights counted")
end)

-- Levels -----------------------------------------------------------------------------------------

test("time per level is the difference of /played level starts", function()
  newWorld()
  load(nil)
  login(true)
  fire("TIME_PLAYED_MSG", 2, 2)
  assert(world.playedShown == nil, "our own /played line is hidden in every chat window")
  runTimers()
  fire("TIME_PLAYED_MSG", 2, 2)
  assert(world.playedShown == 2, "a /played typed by the player still shows")
  world.level = 2
  fire("PLAYER_LEVEL_UP", 2)
  fire("TIME_PLAYED_MSG", 174, 1)
  local events = env.PinkasDB.chars["Testrealm/Testchar"].events
  assert(events[1].kind == "level" and events[1].value.played_level == 173)
end)

test("a level with no seen start is left null", function()
  newWorld()
  load(nil)
  fire("PLAYER_LOGIN")
  fire("PLAYER_ENTERING_WORLD", false, true)
  fire("PLAYER_LEVEL_UP", 11)
  fire("TIME_PLAYED_MSG", 5000, 1)
  local e = env.PinkasDB.chars["Testrealm/Testchar"].events[1]
  assert(e.value.played_level == ns.NULL)
end)

-- Events list ------------------------------------------------------------------------------------

test("event ids grow, the list is capped, exported_at is set", function()
  newWorld()
  load(nil)
  login(true)
  ns.EVENT_CAP = 3
  for q = 1, 5 do
    fire("QUEST_TURNED_IN", q, 10, 0)
  end
  local events = env.PinkasDB.chars["Testrealm/Testchar"].events
  assert(#events == 3 and events[1].id == 3 and events[3].id == 5)
  local export = logoutAndExport()
  assert(export.exported_at == world.now and export.v == 3)
end)

-- Dungeons and bank ------------------------------------------------------------------------------

test("a dungeon run left open is closed at the next real login, not at a reload", function()
  newWorld()
  load(nil)
  login(true)
  world.inInstance, world.zone = true, "Ragefire Chasm"
  fire("ZONE_CHANGED_NEW_AREA")
  world.now = world.now + 300
  fire("PLAYER_LOGOUT")
  local db = env.PinkasDB
  load(db)
  fire("PLAYER_LOGIN")
  fire("PLAYER_ENTERING_WORLD", false, true)
  assert(#db.chars["Testrealm/Testchar"].events == 0, "a reload keeps the run open")
  db.chars["Testrealm/Testchar"].logout_at = world.now
  world.now = world.now + 3600
  load(db)
  login(true)
  local e = db.chars["Testrealm/Testchar"].events[1]
  assert(e.kind == "dungeon" and e.value.ended_by_logout == true and e.value.duration == 300)
end)

test("the bank is read from the tab containers, never the keyring", function()
  newWorld()
  world.bank = {
    [6] = { { itemID = 2318, stackCount = 4 }, false },
    [7] = { false },
    [-1] = { { itemID = 9999, stackCount = 1 } },
  }
  load(nil)
  login(true)
  fire("BANKFRAME_OPENED")
  local bank = env.PinkasDB.chars["Testrealm/Testchar"].bank
  assert(#bank.items == 1 and bank.items[1].id == 2318 and bank.as_of == world.now)
  assert(bank.slots == 3, "both tabs' slots, not the keyring's")
end)

test("a session without a bank or mailbox visit exports the last ones", function()
  newWorld()
  world.bank = { [6] = { { itemID = 2318, stackCount = 4 } } }
  world.inbox = { { sender = "Stranger", money = 0, items = {} } }
  load(nil)
  login(true)
  fire("BANKFRAME_OPENED")
  fire("BANKFRAME_CLOSED")
  fire("MAIL_SHOW")
  fire("MAIL_INBOX_UPDATE")
  fire("MAIL_CLOSED")
  local seen = world.now
  logoutAndExport()
  world.now = world.now + 86400
  world.bank, world.inbox = {}, {}
  load(env.PinkasDB)
  login(true)
  fire("BAG_UPDATE_DELAYED")
  fire("MAIL_INBOX_UPDATE")
  local c = me(logoutAndExport())

  assert(c.bank.as_of == seen and #c.bank.items == 1 and c.bank.items[1].count == 4)
  assert(c.mailbox.as_of == seen and #c.mailbox.mails == 1)
end)

test("two levels before one /played answer are two events, their times unknown", function()
  newWorld()
  load(nil)
  login(true)
  fire("TIME_PLAYED_MSG", 100, 100)
  fire("PLAYER_LEVEL_UP", 2)
  fire("PLAYER_LEVEL_UP", 3)
  fire("TIME_PLAYED_MSG", 300, 1)
  local events = env.PinkasDB.chars["Testrealm/Testchar"].events
  assert(#events == 2 and events[1].value.level == 2 and events[2].value.level == 3)
  assert(events[1].value.played_level == ns.NULL and events[2].value.played_level == ns.NULL)
end)

test("a damaged totals row is pruned at login and the character still exports", function()
  newWorld()
  load({ chars = { ["Testrealm/Testchar"] = { totals = { broken = "x", nosession = {} } } } })
  login(true)
  local totals = env.PinkasDB.chars["Testrealm/Testchar"].totals
  assert(totals.broken == nil and totals.nosession == nil)
  fire("PLAYER_LOGOUT")
  assert(env.PinkasDB.export:find('"Testrealm/Testchar"', 1, true))
end)

-- Saved data and gathering ----------------------------------------------------------------------

test("a damaged SavedVariables file starts empty instead of breaking the addon", function()
  for _, damaged in ipairs({ "garbage", { chars = "x", routes = 5, faults = true } }) do
    newWorld()
    load(damaged)
    login(true)
    assert(type(env.PinkasDB.chars["Testrealm/Testchar"]) == "table")
    fire("PLAYER_LOGOUT")
    assert(env.PinkasDB.export:find('"v":3', 1, true), "the export still works")
  end
end)

test("a damaged character entry is rebuilt, the rest is kept", function()
  newWorld()
  load({
    chars = {
      ["Testrealm/Testchar"] = { events = "x", totals = 3, nextEventId = "y" },
      ["Testrealm/Alt"] = { name = "Alt" },
    },
  })
  login(true)
  local c = env.PinkasDB.chars["Testrealm/Testchar"]
  assert(type(c.events) == "table" and type(c.totals) == "table" and c.nextEventId == 1)
  assert(env.PinkasDB.chars["Testrealm/Alt"].name == "Alt")
end)

test("an alt cached by an older addon exports its missing item fields and slot totals as null", function()
  newWorld()
  local old = { id = 2589, name = "Linen Cloth", count = 20 }
  load({
    chars = {
      ["Testrealm/Alt"] = { name = "Alt", realm = "Testrealm", bags = { old }, bank = { as_of = 1, items = { old } } },
    },
  })
  login(true)
  local alt = logoutAndExport().chars["Testrealm/Alt"]
  assert(alt.bag_slots == "<null>" and alt.bank.slots == "<null>")
  assert(alt.bags[1].quality == "<null>" and alt.bank.items[1].class == "<null>" and alt.bags[1].subclass == "<null>")
end)

test("a damaged alt is left out of the export, the others still go", function()
  newWorld()
  load({
    chars = { ["Testrealm/Broken"] = "x", ["Testrealm/Alt"] = { name = "Alt", realm = "Testrealm", events = 5 } },
  })
  login(true)
  fire("PLAYER_LOGOUT")
  local export = env.PinkasDB.export
  assert(export:find('"Testrealm/Testchar"', 1, true), "the logged-in character is exported")
  assert(not export:find("Broken", 1, true) and not export:find('"Testrealm/Alt"', 1, true))
  assert(#env.PinkasDB.faults == 2, "both damaged entries are noted")
end)

test("only the player's own gathering casts count", function()
  newWorld()
  load(nil)
  login(true)
  fire("UNIT_SPELLCAST_SUCCEEDED", "player", "cast-1", 8613)
  fire("UNIT_SPELLCAST_SUCCEEDED", "party1", "cast-2", 8613)
  fire("UNIT_SPELLCAST_SUCCEEDED", "player", "cast-3", 133)
  local counted = 0
  for _, row in pairs(env.PinkasDB.chars["Testrealm/Testchar"].totals) do
    if row.kind == "gather" then
      assert(row.key == "skinning|Orgrimmar" and #row.value.spots == 1)
      counted = counted + row.value.count
    end
  end
  assert(counted == 1, "one skinning cast of the player's, not the party member's, not a fireball")
end)

-- Size -------------------------------------------------------------------------------------------

-- Writes a table the way the Forever client writes SavedVariables (measured in the beta's files):
-- no indentation, CRLF line ends, no array index comments.
local function savedVariables(v)
  if type(v) == "string" then
    return ("%q"):format(v)
  end
  if type(v) ~= "table" then
    return tostring(v)
  end
  local out = { "{\r\n" }
  if #v > 0 then
    for _, x in ipairs(v) do
      out[#out + 1] = savedVariables(x) .. ",\r\n"
    end
  else
    for k, x in pairs(v) do
      out[#out + 1] = ("[%q] = "):format(k) .. savedVariables(x) .. ",\r\n"
    end
  end
  out[#out + 1] = "}"
  return table.concat(out)
end

local function fullCharacter(n)
  local items = {}
  for i = 1, 12 do
    items[i] = { id = 4304, name = "Thick Leather", count = 20, quality = 1, class = 7, subclass = 6 }
  end
  local events = {}
  for i = 1, ns.EVENT_CAP do
    local kind, value = "quest", { quest = 97908, xp = 2450, money = 3500 }
    local slot = i % 20
    if slot >= 12 and slot < 15 then
      kind, value =
        "death", {
          zone = "Thousand Needles",
          map = 1441,
          x = 0.123456789,
          y = 0.987654321,
          killer = "Galak Messenger",
          recap_id = 1234,
          level = 34,
        }
    elseif slot >= 15 and slot < 17 then
      kind, value = "mail", { to = "Testrealm/Alt" .. n, money = 0, items = items }
    elseif slot >= 17 and slot < 19 then
      kind, value =
        "dungeon", {
          zone = "Scarlet Monastery",
          started = world.now,
          duration = 1800,
          deaths = 2,
          ended_by_logout = false,
          level = 38,
        }
    elseif slot == 19 then
      kind, value = "level", { level = 40, zone = "Desolace", played_total = 512345, played_level = 14321 }
    end
    events[i] = { id = i, at = world.now + i, kind = kind, value = value }
  end
  local bags, bank, mails = {}, {}, {}
  for i = 1, 120 do
    bags[i] = items[1]
  end
  for i = 1, 9 * 36 do
    bank[i] = items[1]
  end
  for i = 1, 50 do
    mails[i] = { from = "other", money = 100, items = { items[1] } }
  end
  local totals = {}
  for session = 1, 14 do
    for row = 1, 20 do
      local at = world.now - session * 86400
      totals[at .. "|gold|label" .. row] =
        { session = at, kind = "gold", key = "label" .. row, value = { ["in"] = 123456, out = 65432 } }
    end
  end
  return {
    name = "Alt" .. n,
    realm = "Testrealm",
    class = "ROGUE",
    race = "Orc",
    level = 60,
    xp = 0,
    xp_max = 0,
    rested = 0,
    resting = true,
    money = 12345678,
    zone = "Orgrimmar",
    map = 1454,
    x = 0.54,
    y = 0.68,
    played = { total = 1, level = 1, at = 1 },
    professions = {},
    bags = bags,
    bag_slots = 120,
    bank = { as_of = world.now, slots = 9 * 36, items = bank },
    mailbox = { as_of = world.now, mails = mails },
    events = events,
    totals = totals,
    nextEventId = ns.EVENT_CAP + 1,
  }
end

test("a full account at the event cap stays well under the pull's 5 MB", function()
  newWorld()
  load(nil)
  login(true)
  for n = 1, 8 do
    env.PinkasDB.chars["Testrealm/Alt" .. n] = fullCharacter(n)
  end
  env.PinkasDB.export = ns.buildExport()
  local bytes = #("PinkasDB = " .. savedVariables(env.PinkasDB) .. "\r\n")
  assert(bytes < 4e6, ("%.2f MB leaves too little headroom under the 5 MB cap"):format(bytes / 1e6))
end)

-- Runner -----------------------------------------------------------------------------------------

for _, t in ipairs(tests) do
  local ok, err = pcall(t.fn)
  if ok then
    print("ok   " .. t.name)
  else
    failures = failures + 1
    print("FAIL " .. t.name .. "\n     " .. tostring(err))
  end
end
for _, f in ipairs(env and env.PinkasDB and env.PinkasDB.faults or {}) do
  print("fault recorded: " .. f.where .. " " .. f.err)
end
print(("%d tests, %d failed"):format(#tests, failures))
os.exit(failures == 0 and 0 or 1)
