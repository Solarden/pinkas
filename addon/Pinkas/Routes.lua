local _, ns = ...

-- Trade-goods subclasses, checked against the Forever beta's Item table (build 1.60.1.70450).
local KINDS = {
  herb = { class = 7, subclass = 9 },
  cloth = { class = 7, subclass = 5 },
  leather = { class = 7, subclass = 6 },
  ore = { class = 7, subclass = 7 },
  enchanting = { class = 7, subclass = 12 },
}
local ORDER = { "herb", "cloth", "leather", "ore", "enchanting" }

local function say(text)
  print("|cff33ff99pinkas|r " .. text)
end

local function bagItems(kind)
  local found = {}
  for _, bag in ipairs(ns.BAGS) do
    for slot = 1, C_Container.GetContainerNumSlots(bag) do
      local info = C_Container.GetContainerItemInfo(bag, slot)
      -- A locked item is already attached to this mail or mid-move.
      if info and not info.isLocked then
        local _, _, _, _, _, class, subclass = C_Item.GetItemInfoInstant(info.itemID)
        if class == kind.class and subclass == kind.subclass then
          found[#found + 1] = { bag = bag, slot = slot }
        end
      end
    end
  end

  return found
end

local function mailIsEmpty()
  for i = 1, ATTACHMENTS_MAX_SEND do
    if GetSendMailItem(i) then
      return false
    end
  end

  return true
end

-- Fills the mail but never sends it: the player checks the recipient and presses Send.
function ns.fillNextMail()
  if not mailIsEmpty() then
    say("send or clear the current mail first")
    return
  end
  for _, name in ipairs(ORDER) do
    -- No route ships with a recipient (a made-up name could be a real player's), and a character
    -- never mails itself, however the route was typed.
    local to = PinkasDB.routes[name]
    local items = (to and to ~= "" and ns.ownKey(to) ~= ns.key) and bagItems(KINDS[name]) or {}
    if #items > 0 then
      local count = math.min(#items, ATTACHMENTS_MAX_SEND)
      for i = 1, count do
        C_Container.PickupContainerItem(items[i].bag, items[i].slot)
        ClickSendMailItemButton(i)
      end
      ClearCursor()
      SendMailNameEditBox:SetText(to)
      SendMailSubjectEditBox:SetText(name)
      say(("%d stacks of %s for %s - check and press Send"):format(count, name, to))
      return
    end
  end
  say("nothing to route from this character (set routes with /pinkas route <kind> <Name>)")
end

local function listRoutes()
  for _, name in ipairs(ORDER) do
    say(name .. " -> " .. (PinkasDB.routes[name] or "(off)"))
  end
end

local function command(input)
  local verb, kind, who = (input or ""):match("^%s*(%S*)%s*(%S*)%s*(%S*)")
  if verb == "route" and KINDS[kind] then
    PinkasDB.routes[kind] = who ~= "" and who or nil
    say(kind .. " -> " .. (who ~= "" and who or "(off)"))
    return
  end
  if verb == "routes" then
    listRoutes()
    return
  end
  say("/pinkas route <herb|cloth|leather|ore|enchanting> <Name>  (no name turns it off)")
  say("/pinkas routes")
end

SLASH_PINKAS1 = "/pinkas"
SlashCmdList.PINKAS = command

if SendMailFrame then
  local button = CreateFrame("Button", nil, SendMailFrame, "UIPanelButtonTemplate")
  button:SetSize(100, 22)
  -- Outside the frame: its bottom row (money, Send, Cancel) has no free space.
  button:SetPoint("TOPLEFT", SendMailFrame, "TOPRIGHT", 4, -30)
  button:SetText("Route mats")
  button:SetScript("OnClick", ns.fillNextMail)
end
