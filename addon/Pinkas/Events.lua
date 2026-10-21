local _, ns = ...

local plain = ns.plain
local COMBATLOG_OBJECT_CONTROL_PLAYER = 0x00000100

-- Level up: TIME_PLAYED_MSG only knows the current level, so a level's time is the difference
-- between the played totals at which consecutive levels started.
local pendingLevel
local silentPlayed = false

-- If our line is never displayed, the flag must not linger and hide the player's own /played.
local SILENT_SECONDS = 5

local function requestPlayed()
  silentPlayed = true
  RequestTimePlayed()
  C_Timer.After(SILENT_SECONDS, function()
    silentPlayed = false
  end)
end

-- Forever's chat frame prints /played through ChatFrameUtil; the global ChatFrame_DisplayTimePlayed
-- is only a deprecated copy that the chat frame never calls.
if ChatFrameUtil and ChatFrameUtil.DisplayTimePlayed then
  local original = ChatFrameUtil.DisplayTimePlayed
  -- Hide only the line our own request printed; a /played typed by the player still shows.
  ChatFrameUtil.DisplayTimePlayed = function(...)
    if not silentPlayed then
      return original(...)
    end
  end
end

-- Every chat window showing System messages prints the line itself, so the flag drops on the next
-- frame, after all of them.
ns.on("TIME_PLAYED_MSG", function()
  if silentPlayed then
    C_Timer.After(0, function()
      silentPlayed = false
    end)
  end
end)

ns.on("PLAYER_ENTERING_WORLD", function(isInitialLogin)
  if isInitialLogin then
    requestPlayed()
  end
end)

ns.on("PLAYER_LEVEL_UP", function(level)
  pendingLevel = level
  requestPlayed()
end)

ns.on("TIME_PLAYED_MSG", function(total, thisLevel)
  total, thisLevel = plain(total), plain(thisLevel)
  if not total or not thisLevel then
    return
  end
  local c = ns.char
  local levelStart = total - thisLevel
  if pendingLevel then
    local previousStart = c.levelStartPlayed
    local where = {}
    ns.position(where)
    ns.addEvent("level", {
      level = pendingLevel,
      zone = where.zone,
      played_total = total,
      -- No start seen for the finished level (installed mid-level, no login message): leave it null.
      played_level = previousStart and (levelStart - previousStart) or ns.NULL,
    })
    pendingLevel = nil
  end
  c.levelStartPlayed = levelStart
  c.played = { total = total, level = thisLevel, at = time() }
end)

-- Death: the recap must be read now; a /reload drops it.
local function killerOf(events)
  local blow
  for _, e in ipairs(events or {}) do
    local overkill = plain(e.overkill)
    if overkill and overkill >= 0 and (not blow or (e.timestamp or 0) >= (blow.timestamp or 0)) then
      blow = e
    end
  end
  if not blow then
    return ns.NULL
  end
  if blow.environmentalType then
    return blow.environmentalType
  end
  if blow.sourceFlags and bit.band(blow.sourceFlags, COMBATLOG_OBJECT_CONTROL_PLAYER) ~= 0 then
    return "player"
  end

  return blow.sourceName or ns.NULL
end

ns.on("PLAYER_DEAD", function()
  local recap = C_DeathRecap or {}
  local ok, events = pcall(recap.GetRecapEvents or function() end)
  local okLink, link = pcall(recap.GetRecapLink or function() end)
  local where = { level = UnitLevel("player") }
  ns.position(where)
  -- No position inside an instance; the keys stay, as null.
  where.x, where.y = where.x or ns.NULL, where.y or ns.NULL
  where.killer = killerOf(ok and events or nil)
  where.recap_id = okLink and type(link) == "string" and tonumber(link:match("|Hdeath:(%d+)|")) or ns.NULL
  ns.addEvent("death", where)
  if ns.char.run then
    ns.char.run.deaths = ns.char.run.deaths + 1
  end
end)

ns.on("QUEST_TURNED_IN", function(questID, xp, money)
  ns.addEvent("quest", { quest = questID, xp = plain(xp) or 0, money = plain(money) or 0 })
end)

-- Mail to an own alt: read at send time (the player may edit the mail after a fill), kept on success.
local pendingMail

hooksecurefunc("SendMail", function(recipient)
  local key = ns.char and ns.ownKey(recipient)
  if not key then
    pendingMail = nil
    return
  end
  local items = {}
  for i = 1, ATTACHMENTS_MAX_SEND do
    local name, itemID, _, count = GetSendMailItem(i)
    if itemID then
      items[#items + 1] = { id = itemID, name = name, count = count or 1 }
    end
  end
  pendingMail = { to = key, items = items }
end)

ns.on("MAIL_SEND_SUCCESS", function()
  if pendingMail then
    ns.addEvent("mail", pendingMail)
    pendingMail = nil
  end
end)

-- Dungeon runs: entering and leaving an instance shows up as a zone change.
local function closeRun(endedByLogout, endAt)
  local run = ns.char.run
  if not run then
    return
  end
  run.duration = (endAt or time()) - run.started
  run.ended_by_logout = endedByLogout
  ns.addEvent("dungeon", run)
  ns.char.run = nil
end

-- PLAYER_LOGOUT also fires on /reload, so a run left open is closed at the next real login, ending
-- at the logout it never saw.
ns.on("PLAYER_ENTERING_WORLD", function(isInitialLogin)
  if isInitialLogin and ns.char.run then
    closeRun(true, ns.char.logout_at)
  end
end)

ns.on("ZONE_CHANGED_NEW_AREA", function()
  local inInstance = IsInInstance()
  if inInstance and not ns.char.run then
    ns.char.run = { instance = GetZoneText(), level = UnitLevel("player"), started = time(), deaths = 0 }
  elseif not inInstance then
    closeRun(false)
  end
end)

