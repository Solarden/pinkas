local _, ns = ...

local plain = ns.plain

-- TIME_PLAYED_MSG only knows the current level, so a level's time is the difference
-- between the played totals at which consecutive levels started.
local pendingLevels = {}
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
  table.insert(pendingLevels, level)
  requestPlayed()
end)

ns.on("TIME_PLAYED_MSG", function(total, thisLevel)
  total, thisLevel = plain(total), plain(thisLevel)
  if not total or not thisLevel then
    return
  end
  local c = ns.char
  local levelStart = total - thisLevel
  local where = {}
  if #pendingLevels > 0 then
    ns.position(where)
  end
  local previousStart = c.levelStartPlayed
  for _, level in ipairs(pendingLevels) do
    ns.addEvent("level", {
      level = level,
      zone = where.zone,
      played_total = total,
      -- Null when no start was seen (installed mid-level), or when several levels came before one
      -- answer (a quest worth two levels): only the last level's start is known then.
      played_level = (#pendingLevels == 1 and previousStart) and (levelStart - previousStart) or ns.NULL,
    })
  end
  pendingLevels = {}
  c.levelStartPlayed = levelStart
  c.played = { total = total, level = thisLevel, at = time() }
end)
