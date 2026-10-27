// Served by the app itself (CSP script-src 'self'). Every page works without it.
// Card data comes from the JSON block that web.card_keys fills in index.html.
(function () {
  "use strict";

  function load(key) {
    try {
      return localStorage.getItem(key);
    } catch (e) {
      return null;
    }
  }

  function save(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch (e) {
      // storage blocked: the choice just isn't remembered
    }
  }

  function remember(details, key, openByDefault) {
    if (!details) return;
    var open = load(key);
    details.open = open === null ? openByDefault : open === "1";
    details.addEventListener("toggle", function () {
      save(key, details.open ? "1" : "0");
    });
  }

  var side = document.querySelector("details.side");
  remember(side, "pinkas.guide", side && side.open);
  // The account summary fills a phone's first screen, so a phone starts with it closed.
  remember(document.querySelector("details.account"), "pinkas.account", !window.matchMedia("(max-width: 40rem)").matches);

  // Ctrl+K / Cmd+K, not a bare "/": a single-character shortcut fires by accident under speech input (WCAG 2.1.4).
  var search = document.querySelector("header input[type=search]");
  document.addEventListener("keydown", function (e) {
    if (!search || !(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey || e.key.toLowerCase() !== "k") return;
    e.preventDefault();
    search.focus();
    search.select();
  });

  var tools = document.getElementById("card-tools");
  var grid = document.querySelector(".chars");
  var source = document.getElementById("cards");
  if (!tools || !grid || !source) return;

  var keys;
  try {
    keys = JSON.parse(source.textContent);
  } catch (e) {
    return;
  }

  var cards = Array.prototype.filter.call(grid.children, function (c) {
    return keys[c.id];
  });
  cards.forEach(function (c, i) {
    keys[c.id].index = i;
  });

  function desc(field) {
    return function (a, b) {
      var x = a[field];
      var y = b[field];
      if (x === y) return 0;
      if (x === null || x === undefined) return 1;
      if (y === null || y === undefined) return -1;

      return y - x;
    };
  }

  var orders = {
    rested: desc("rested"),
    level: desc("level"),
    seen: desc("seen"),
    name: function (a, b) {
      return a.name.localeCompare(b.name);
    },
  };

  function arrange(order) {
    var by = orders[order];
    cards
      .slice()
      .sort(function (a, b) {
        var x = keys[a.id];
        var y = keys[b.id];

        return y.playing - x.playing || (by ? by(x, y) : 0) || x.index - y.index;
      })
      .forEach(function (c) {
        grid.appendChild(c);
      });
  }

  // #card-count is an aria-live region, so a screen reader hears how many cards are left.
  var count = document.getElementById("card-count");

  function filter(query) {
    var q = query.trim().toLocaleLowerCase();
    var shown = 0;
    cards.forEach(function (c) {
      var k = keys[c.id];
      var text = [k.name, k.role || ""].concat(k.professions || []).join(" ").toLocaleLowerCase();
      c.hidden = q !== "" && text.indexOf(q) < 0;
      if (!c.hidden) shown += 1;
    });
    if (count) count.textContent = q === "" ? "" : shown + " of " + cards.length + " characters";
  }

  var sort = document.getElementById("sort");
  var find = document.getElementById("filter");
  sort.value = load("pinkas.sort") || "";
  if (sort.selectedIndex < 0) sort.value = "";
  arrange(sort.value);
  sort.addEventListener("change", function () {
    save("pinkas.sort", sort.value);
    arrange(sort.value);
  });
  find.addEventListener("input", function () {
    filter(find.value);
  });
  tools.hidden = false;
})();
