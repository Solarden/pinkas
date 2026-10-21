local _, ns = ...

local plain = ns.plain

-- Gold: PLAYER_MONEY gives only the new total, so each change takes the label set just before it.
-- A hook's label is consumed by the next change and wins over an open frame's label.
local HOOK_LABEL_SECONDS = 5
local hookLabel, hookAt
local frameLabel
local lastMoney

local function setHookLabel(label)
  hookLabel, hookAt = label, time()
end

local function currentLabel()
  if hookLabel and time() - hookAt <= HOOK_LABEL_SECONDS then
    local label = hookLabel
    hookLabel = nil

    return label
  end
  hookLabel = nil

  return frameLabel or "other"
end

ns.on("PLAYER_ENTERING_WORLD", function()
  lastMoney = plain(GetMoney())
end)

ns.on("PLAYER_MONEY", function()
  local now = plain(GetMoney())
  if ns.leaving or not now then
    return
  end
  if lastMoney and now ~= lastMoney then
    local delta = now - lastMoney
    ns.total("gold", currentLabel(), function(v)
      ns.add(v, delta > 0 and "in" or "out", math.abs(delta))
    end)
  end
  lastMoney = now
end)

local FRAMES = {
  { open = "LOOT_OPENED", close = "LOOT_CLOSED", label = "loot" },
  { open = "MERCHANT_SHOW", close = "MERCHANT_CLOSED", label = "vendor" },
  { open = "MAIL_SHOW", close = "MAIL_CLOSED", label = "mail" },
  { open = "TRADE_SHOW", close = "TRADE_CLOSED", label = "trade" },
  { open = "AUCTION_HOUSE_SHOW", close = "AUCTION_HOUSE_CLOSED", label = "auction" },
}
for _, f in ipairs(FRAMES) do
  ns.on(f.open, function()
    frameLabel = f.label
  end)
  ns.on(f.close, function()
    if frameLabel == f.label then
      frameLabel = nil
    end
  end)
end

-- A hook runs at the click, before the server's money change (measured for the repair).
-- TODO(beta): a flight through TakeTaxiNode; the only flight came before the hook existed.
local HOOKS = { RepairAllItems = "repair", TakeTaxiNode = "taxi", SendMail = "mail" }
for fn, label in pairs(HOOKS) do
  if type(_G[fn]) == "function" then
    hooksecurefunc(fn, function()
      setHookLabel(label)
    end)
  end
end

-- TODO(beta): whether QUEST_TURNED_IN comes before the reward's PLAYER_MONEY; both turn-ins paid 0.
ns.on("QUEST_TURNED_IN", function()
  setHookLabel("quest")
end)

-- Combat: C_DamageMeter is readable only once the combat restriction lifts, and only the player's
-- own row is read; it lists everyone nearby.
local METER = { damage = 0, healing = 2, taken = 7, avoidable = 8 }
local BEST_FIGHT_MIN_SECONDS = 10
-- A session's duration still ticks for about a second after combat, and settles within 5 s.
local SETTLE_SECONDS = 5

local function ownAmount(sessionID, meterType)
  local ok, session = pcall(C_DamageMeter.GetCombatSessionFromID, sessionID, meterType)
  if not ok or type(session) ~= "table" then
    return nil
  end
  for _, source in ipairs(session.combatSources or {}) do
    if source.isLocalPlayer then
      return plain(source.totalAmount)
    end
  end

  return 0
end

local function readMeter()
  if UnitAffectingCombat("player") or not C_DamageMeter then
    return
  end
  local ok, sessions = pcall(C_DamageMeter.GetAvailableCombatSessions)
  if not ok or type(sessions) ~= "table" then
    return
  end
  local state = ns.char.session
  local ordered = {}
  for _, s in ipairs(sessions) do
    ordered[#ordered + 1] = s
  end
  -- lastMeterId only moves forward, so a lower unread id must never come after a higher one.
  table.sort(ordered, function(a, b)
    return (plain(a.sessionID) or 0) < (plain(b.sessionID) or 0)
  end)
  for _, s in ipairs(ordered) do
    local id = plain(s.sessionID)
    local seconds = plain(s.durationSeconds)
    if id and id > state.lastMeterId and seconds then
      local amounts = {}
      for field, meterType in pairs(METER) do
        amounts[field] = ownAmount(id, meterType)
        if amounts[field] == nil then
          -- Still secret: leave this session for the next read.
          return
        end
      end
      ns.total("combat", tostring(UnitLevel("player")), function(v)
        for field, amount in pairs(amounts) do
          ns.add(v, field, amount)
        end
        ns.add(v, "seconds", seconds)
        ns.add(v, "fights", 1)
        if seconds >= BEST_FIGHT_MIN_SECONDS then
          v.best_dps = math.max(v.best_dps or 0, amounts.damage / seconds)
        end
      end)
      state.lastMeterId = id
    end
  end
end

ns.on("ADDON_RESTRICTION_STATE_CHANGED", function(kind, state)
  if kind == 0 and state == 0 then
    C_Timer.After(SETTLE_SECONDS, readMeter)
  end
end)

-- Gathering: TODO(beta): only Skinning (8613) was seen; the herbalism and mining ids are Classic's.
local GATHER = {
  [8613] = "skinning",
  [2366] = "herbalism",
  [2575] = "mining",
}

ns.on("UNIT_SPELLCAST_SUCCEEDED", function(unit, _, spellID)
  local profession = unit == "player" and GATHER[spellID]
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
end)

local lastXP, lastRested, lastLevel

ns.on("PLAYER_ENTERING_WORLD", function()
  lastXP, lastRested, lastLevel = plain(UnitXP("player")), plain(GetXPExhaustion()) or 0, UnitLevel("player")
end)

ns.on("PLAYER_XP_UPDATE", function()
  local xp, rested, level = plain(UnitXP("player")), plain(GetXPExhaustion()) or 0, UnitLevel("player")
  if ns.leaving or not xp then
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
