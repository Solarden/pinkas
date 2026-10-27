-- The game runs Lua 5.1, whatever the local `lua` is.
std = "lua51"
max_line_length = 120
exclude_files = { "tests/fixtures/" }

-- The only globals the addon writes: the ones the game requires, and the one replaced Blizzard
-- function, which hides the addon's own /played line.
globals = {
  "PinkasDB",
  "SLASH_PINKAS1",
  SlashCmdList = { fields = { "PINKAS" } },
  ChatFrameUtil = { fields = { "DisplayTimePlayed" } },
}

-- The WoW API the addon reads. A name missing here is a typo, or a new API to check against the
-- Forever client before adding it.
read_globals = {
  "ATTACHMENTS_MAX_RECEIVE",
  "ATTACHMENTS_MAX_SEND",
  "bit",
  "C_Container",
  "C_DamageMeter",
  "C_DeathRecap",
  "C_Item",
  "C_Map",
  "C_Timer",
  "ClearCursor",
  "ClickSendMailItemButton",
  "CreateFrame",
  "Enum",
  "GetBuildInfo",
  "GetInboxHeaderInfo",
  "GetInboxItem",
  "GetInboxNumItems",
  "GetMoney",
  "GetProfessionInfo",
  "GetProfessions",
  "GetRealmName",
  "GetSendMailItem",
  "GetXPExhaustion",
  "GetZoneText",
  "hooksecurefunc",
  "IsInInstance",
  "IsResting",
  "issecretvalue",
  "RequestTimePlayed",
  "SendMailFrame",
  "SendMailNameEditBox",
  "SendMailSubjectEditBox",
  "time",
  "UnitAffectingCombat",
  "UnitClass",
  "UnitLevel",
  "UnitName",
  "UnitRace",
  "UnitXP",
  "UnitXPMax",
}

-- The stub harness runs on the local Lua and builds the fake client as its own environment.
files["tests/addon_test.lua"] = { std = "max" }
