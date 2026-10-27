local _, ns = ...

local NULL = ns.NULL
local array = ns.array

local function orNull(v)
  if v == nil then
    return NULL
  end

  return v
end

local function items(list)
  local out = array()
  for i, it in ipairs(list or {}) do
    out[i] = {
      id = it.id,
      name = orNull(it.name),
      count = it.count,
      quality = orNull(it.quality),
      class = orNull(it.class),
      subclass = orNull(it.subclass),
    }
  end

  return out
end

local function container(c)
  if not c then
    return NULL
  end

  return { as_of = c.as_of, slots = orNull(c.slots), items = items(c.items) }
end

local function mailbox(m)
  if not m then
    return NULL
  end
  local mails = array()
  for i, mail in ipairs(m.mails or {}) do
    mails[i] = { from = mail.from, money = mail.money, items = items(mail.items) }
  end

  return { as_of = m.as_of, mails = mails }
end

-- Events and totals carry nested tables. Their list fields are named, because an empty list
-- would otherwise encode as an object.
local LIST_FIELDS = { items = true, spots = true }

local function withArrays(v, isList)
  if type(v) ~= "table" or v == NULL then
    return v
  end
  local out = isList and array() or {}
  for k, x in pairs(v) do
    out[k] = withArrays(x, LIST_FIELDS[k])
  end

  return out
end

local function events(list)
  local out = array()
  for i, e in ipairs(list or {}) do
    out[i] = { id = e.id, at = e.at, kind = e.kind, value = withArrays(e.value) }
  end

  return out
end

local function totals(map)
  local ids = {}
  for id in pairs(map or {}) do
    ids[#ids + 1] = id
  end
  table.sort(ids)
  local out = array()
  for i, id in ipairs(ids) do
    local row = map[id]
    out[i] = { session = row.session, kind = row.kind, key = row.key, value = withArrays(row.value) }
  end

  return out
end

local function professions(list)
  local out = array()
  for i, p in ipairs(list or {}) do
    out[i] = { skill_line = orNull(p.skill_line), name = orNull(p.name), rank = orNull(p.rank), max = orNull(p.max) }
  end

  return out
end

-- Every key is always present (pinkas/parse.py checks the contract), so each object is built from a
-- fixed field list rather than by walking the cache, where an unknown value is simply absent.
local function character(c)
  -- A string indexes without error in Lua, so a damaged entry would export as a nameless character
  -- that makes the parser refuse the whole file.
  assert(type(c) == "table" and type(c.name) == "string" and type(c.realm) == "string", "not a character")
  local session = c.session or {}
  local played = c.played and { total = c.played.total, level = c.played.level, at = c.played.at } or NULL

  return {
    name = orNull(c.name),
    realm = orNull(c.realm),
    class = orNull(c.class),
    race = orNull(c.race),
    level = orNull(c.level),
    xp = orNull(c.xp),
    xp_max = orNull(c.xp_max),
    rested = orNull(c.rested),
    resting = orNull(c.resting),
    money = orNull(c.money),
    zone = orNull(c.zone),
    map = orNull(c.map),
    x = orNull(c.x),
    y = orNull(c.y),
    session_started_at = orNull(session.started_at),
    logout_at = orNull(c.logout_at),
    played = played,
    professions = professions(c.professions),
    bags = items(c.bags),
    bag_slots = orNull(c.bag_slots),
    bank = container(c.bank),
    mailbox = mailbox(c.mailbox),
    events = events(c.events),
    totals = totals(c.totals),
  }
end

function ns.buildExport()
  local chars = {}
  -- A damaged entry is left out and noted, so it can't stop every other character's export.
  for key, c in pairs(PinkasDB.chars) do
    local ok, built = pcall(character, c)
    if ok then
      chars[key] = built
    else
      ns.fault("export " .. key, built)
    end
  end

  local version, build
  if GetBuildInfo then
    version, build = GetBuildInfo()
  end

  return ns.json({
    v = ns.EXPORT_VERSION,
    exported_at = time(),
    -- tostring(): a nil from the client must not cost the whole export.
    build = tostring(version) .. "." .. tostring(build),
    routes = PinkasDB.routes,
    chars = chars,
  })
end

-- A real logout has already emptied the unit, so nothing is read from the game here.
ns.on("PLAYER_LOGOUT", function()
  ns.char.logout_at = time()
  PinkasDB.export = ns.buildExport()
end)
