local _, ns = ...

local open = false

ns.on("MAIL_SHOW", function()
  open = true
end)

ns.on("MAIL_CLOSED", function()
  open = false
end)

-- Read only at a mailbox: an update away from one (another addon's CheckInbox) would store an empty
-- inbox over the last capture. TODO(beta): whether the event fires away from a mailbox at all.
ns.on("MAIL_INBOX_UPDATE", function()
  if not open then
    return
  end
  local mails = {}
  for i = 1, GetInboxNumItems() do
    local _, _, sender, _, money = GetInboxHeaderInfo(i)
    local items = {}
    for a = 1, ATTACHMENTS_MAX_RECEIVE do
      local name, itemID, _, count = GetInboxItem(i, a)
      if itemID then
        items[#items + 1] = { id = itemID, name = name, count = count or 1 }
      end
    end
    -- Other players' names are never stored.
    mails[#mails + 1] = { from = ns.ownKey(sender) or "other", money = ns.plain(money) or 0, items = items }
  end
  ns.char.mailbox = { as_of = time(), mails = mails }
end)

-- Mail to an own alt: read at send time (the player may edit the mail after a fill), kept on success.
local pendingMail

hooksecurefunc("SendMail", function(recipient)
  local key = ns.char and ns.ownKey(recipient)
  if not key then
    pendingMail = nil
    return
  end
  local items = {}
  for i = 1, ATTACHMENTS_MAX_SEND do
    local name, itemID, _, count = GetSendMailItem(i)
    if itemID then
      items[#items + 1] = { id = itemID, name = name, count = count or 1 }
    end
  end
  pendingMail = { to = key, items = items }
end)

ns.on("MAIL_SEND_SUCCESS", function()
  if pendingMail then
    ns.addEvent("mail", pendingMail)
    pendingMail = nil
  end
end)
