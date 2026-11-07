local _, ns = ...

-- A gathering spell's id changes with its rank (Forever's Mining came as 2576), so a cast is matched by
-- the name of these rank 1 spells, read in the client's language. TODO(beta): no herb seen yet.
local RANK_ONE = {
  [8613] = "skinning",
  [2366] = "herbalism",
  [2575] = "mining",
}
local byName

local function gathering(spellID)
  if RANK_ONE[spellID] or not (C_Spell and C_Spell.GetSpellName) then
    return RANK_ONE[spellID]
  end
  if not byName then
    byName = {}
    for id, profession in pairs(RANK_ONE) do
      local name = C_Spell.GetSpellName(id)
      if name then
        byName[name] = profession
      end
    end
  end
  local name = C_Spell.GetSpellName(spellID)

  return name and byName[name]
end

ns.on("UNIT_SPELLCAST_SUCCEEDED", function(_, _, spellID)
  local profession = gathering(spellID)
  if not profession then
    return
  end
  local where = {}
  ns.position(where)
  ns.total("gather", profession .. "|" .. (where.zone or ""), function(v)
    ns.add(v, "count", 1)
    v.spots = v.spots or {}
    if where.x and #v.spots < 50 then
      v.spots[#v.spots + 1] = { x = where.x, y = where.y, map = where.map }
    end
  end)
end, "player")
