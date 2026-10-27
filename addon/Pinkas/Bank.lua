local _, ns = ...

local open = false

-- Container -1 is the Keyring in Forever; the character bank is retail's tab containers.
local function tabIds()
  local ids = {}
  local index = Enum and Enum.BagIndex or {}
  for i = 1, 9 do
    local id = index["CharacterBankTab_" .. i]
    if id then
      ids[#ids + 1] = id
    end
  end

  return ids
end

local function read()
  if not open or ns.leaving then
    return
  end
  ns.char.bank = { as_of = time(), items = ns.readContainers(tabIds()) }
end

ns.on("BANKFRAME_OPENED", function()
  open = true
  -- TODO(beta): the read timing; a read right after the deposit missed a tab, so read again shortly after.
  read()
  C_Timer.After(1, read)
end)

ns.on("BANKFRAME_CLOSED", function()
  open = false
end)

ns.on("BAG_UPDATE_DELAYED", read)
