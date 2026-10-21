local _, ns = ...

local plain = ns.plain

function ns.position(target)
  local map = C_Map.GetBestMapForUnit("player")
  local pos = map and C_Map.GetPlayerMapPosition(map, "player")
  target.zone = GetZoneText()
  target.map = map
  -- No map position inside an instance (measured in Ragefire Chasm); the zone still names it.
  target.x = pos and plain(pos.x) or nil
  target.y = pos and plain(pos.y) or nil
end

function ns.readContainers(ids)
  local items = {}
  for _, bag in ipairs(ids) do
    local ok, slots = pcall(C_Container.GetContainerNumSlots, bag)
    for slot = 1, (ok and slots or 0) do
      local info = C_Container.GetContainerItemInfo(bag, slot)
      if info and info.itemID then
        items[#items + 1] = { id = info.itemID, name = info.itemName, count = info.stackCount or 1 }
      end
    end
  end

  return items
end

-- Backpack, the four bag slots and the reagent bag (Enum.BagIndex 0..5 in Forever).
ns.BAGS = { 0, 1, 2, 3, 4, 5 }

local function refreshIdentity()
  local c = ns.char
  local _, class = UnitClass("player")
  local _, race = UnitRace("player")
  c.class, c.race = class, race
  c.level = UnitLevel("player")
end

local function refreshXP()
  local max = plain(UnitXPMax("player"))
  -- An emptied unit (loading screen, logout) reads 0/0; keep the last good values.
  if ns.leaving or not max or max == 0 then
    return
  end
  local xp, rested = UnitXP("player"), GetXPExhaustion()
  -- In combat these can be secret: keep the last good values rather than store unknowns over them.
  if ns.isSecret(xp) or ns.isSecret(rested) then
    return
  end
  local c = ns.char
  c.xp = plain(xp)
  c.xp_max = max
  c.rested = plain(rested)
  c.resting = IsResting() and true or false
end

local function refreshMoney()
  if ns.leaving then
    return
  end
  ns.char.money = plain(GetMoney())
end

local function refreshPosition()
  if ns.leaving then
    return
  end
  ns.position(ns.char)
end

local function professions(...)
  local list = {}
  for i = 1, select("#", ...) do
    local index = select(i, ...)
    if index then
      local name, _, rank, maxRank, _, _, skillLine = GetProfessionInfo(index)
      list[#list + 1] = { skill_line = skillLine, name = name, rank = plain(rank), max = plain(maxRank) }
    end
  end

  return list
end

local function refreshProfessions()
  if ns.leaving then
    return
  end
  ns.char.professions = professions(GetProfessions())
end

local function refreshBags()
  if ns.leaving then
    return
  end
  ns.char.bags = ns.readContainers(ns.BAGS)
end

local function refreshAll()
  refreshIdentity()
  refreshXP()
  refreshMoney()
  refreshPosition()
  refreshProfessions()
  refreshBags()
end

ns.on("PLAYER_ENTERING_WORLD", refreshAll)
ns.on("PLAYER_LEVEL_UP", function(level)
  refreshIdentity()
  -- UnitLevel can still report the old level while this event fires.
  ns.char.level = level
end)
ns.on("PLAYER_XP_UPDATE", refreshXP)
ns.on("UPDATE_EXHAUSTION", refreshXP)
ns.on("PLAYER_UPDATE_RESTING", refreshXP)
ns.on("PLAYER_MONEY", refreshMoney)
ns.on("ZONE_CHANGED_NEW_AREA", refreshPosition)
ns.on("SKILL_LINES_CHANGED", refreshProfessions)
ns.on("BAG_UPDATE_DELAYED", refreshBags)
