local _, ns = ...

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

-- Entering and leaving an instance shows up as a zone change.
ns.on("ZONE_CHANGED_NEW_AREA", function()
  local inInstance = IsInInstance()
  if inInstance and not ns.char.run then
    ns.char.run = { instance = GetZoneText(), level = UnitLevel("player"), started = time(), deaths = 0 }
  elseif not inInstance then
    closeRun(false)
  end
end)
