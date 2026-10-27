local _, ns = ...

local plain = ns.plain

ns.on("QUEST_TURNED_IN", function(questID, xp, money)
  ns.addEvent("quest", { quest = questID, xp = plain(xp) or 0, money = plain(money) or 0 })
end)
