local _, ns = ...

local plain = ns.plain

-- C_DamageMeter is readable only once the combat restriction lifts, and only the player's
-- own row is read; it lists everyone nearby.
local METER = { damage = 0, healing = 2, taken = 7, avoidable = 8 }
local BEST_FIGHT_MIN_SECONDS = 10
-- A session's duration still ticks for about a second after combat, and settles within 5 s.
local SETTLE_SECONDS = 5

-- nil only for a secret amount, which a later read can still get. A meter the client doesn't serve
-- counts as 0: retrying it would hold back every later session for good.
local function ownAmount(sessionID, meterType)
  local ok, session = pcall(C_DamageMeter.GetCombatSessionFromID, sessionID, meterType)
  if not ok or type(session) ~= "table" then
    return 0
  end
  for _, source in ipairs(session.combatSources or {}) do
    if source.isLocalPlayer then
      if ns.isSecret(source.totalAmount) then
        return nil
      end

      return plain(source.totalAmount) or 0
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
