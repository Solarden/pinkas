local _, ns = ...

ns.NULL = setmetatable({}, { __tostring = function() return "null" end })
local ARRAY = {}

-- An empty Lua table can't tell an array from an object, so arrays are marked.
function ns.array(t)
  return setmetatable(t or {}, ARRAY)
end

local escapes = { ['"'] = '\\"', ["\\"] = "\\\\", ["\n"] = "\\n", ["\r"] = "\\r", ["\t"] = "\\t" }

local function encodeString(s)
  -- Bytes >= 0x80 pass through, so UTF-8 names stay intact.
  return '"' .. s:gsub('[%c"\\]', function(ch)
    return escapes[ch] or string.format("\\u%04x", ch:byte())
  end) .. '"'
end

local function encodeNumber(n)
  if n ~= n or n == math.huge or n == -math.huge then
    return "null"
  end
  if n == math.floor(n) and math.abs(n) < 2 ^ 53 then
    return string.format("%d", n)
  end

  return string.format("%.14g", n)
end

local encode

local function encodeTable(t)
  if t == ns.NULL then
    return "null"
  end

  local parts = {}
  if getmetatable(t) == ARRAY then
    for i = 1, #t do
      parts[i] = encode(t[i])
    end

    return "[" .. table.concat(parts, ",") .. "]"
  end

  local keys = {}
  for k in pairs(t) do
    keys[#keys + 1] = tostring(k)
  end
  table.sort(keys)
  for i, k in ipairs(keys) do
    local v = t[k]
    if v == nil then
      v = t[tonumber(k)]
    end
    parts[i] = encodeString(k) .. ":" .. encode(v)
  end

  return "{" .. table.concat(parts, ",") .. "}"
end

encode = function(v)
  local kind = type(v)
  if kind == "string" then
    return encodeString(v)
  end
  if kind == "number" then
    return encodeNumber(v)
  end
  if kind == "boolean" then
    return v and "true" or "false"
  end
  if kind == "table" then
    return encodeTable(v)
  end

  return "null"
end

ns.json = encode
