local _, ns = ...

local plain = ns.plain

local COMBATLOG_OBJECT_CONTROL_PLAYER = 0x00000100

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
  -- Fails closed: a name is kept only when the flags prove an NPC. The beta's recaps always carried
  -- them; without them the source could be a player, so it stays unknown.
  if type(blow.sourceFlags) ~= "number" then
    return ns.NULL
  end
  if bit.band(blow.sourceFlags, COMBATLOG_OBJECT_CONTROL_PLAYER) ~= 0 then
    return "player"
  end

  return blow.sourceName or ns.NULL
end

-- The recap must be read now: a /reload drops it.
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
