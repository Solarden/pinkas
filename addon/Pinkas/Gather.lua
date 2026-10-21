local _, ns = ...

-- TODO(beta): only Skinning (8613) was seen; the herbalism and mining ids are Classic's.
local GATHER = {
  [8613] = "skinning",
  [2366] = "herbalism",
  [2575] = "mining",
}

ns.on("UNIT_SPELLCAST_SUCCEEDED", function(_, _, spellID)
  local profession = GATHER[spellID]
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
