local _, ns = ...

local plain = ns.plain

-- How much of the XP gained came from the rested bonus.
local lastXP, lastRested, lastLevel

ns.on("PLAYER_ENTERING_WORLD", function()
  lastXP, lastRested, lastLevel = plain(UnitXP("player")), plain(GetXPExhaustion()) or 0, UnitLevel("player")
end)

ns.on("PLAYER_XP_UPDATE", function()
  local rawRested = GetXPExhaustion()
  local xp, rested, level = plain(UnitXP("player")), plain(rawRested) or 0, UnitLevel("player")
  -- A secret rested would read as 0 and book the whole bar as used: wait for a readable update.
  if ns.leaving or not xp or ns.isSecret(rawRested) then
    return
  end
  if lastXP and level == lastLevel and xp > lastXP then
    local used = math.max(0, (lastRested or 0) - rested)
    ns.total("rested", "", function(v)
      ns.add(v, "xp", xp - lastXP)
      ns.add(v, "rested_used", used)
    end)
  end
  lastXP, lastRested, lastLevel = xp, rested, level
end)
