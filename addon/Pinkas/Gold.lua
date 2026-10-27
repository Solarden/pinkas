local _, ns = ...

local plain = ns.plain

-- PLAYER_MONEY gives only the new total, so each change takes the label set just before it.
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
