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

  // The /stats crosshair: each line chart's readouts from the #charts JSON block, by figure id.
  var block = document.getElementById("charts");
  var hides = [];
  var readouts = null;
  try {
    readouts = block && JSON.parse(block.textContent);
  } catch (e) {
    // A broken block leaves the charts without a readout; the server has drawn the page.
  }
  Array.prototype.forEach.call(document.querySelectorAll("figure.chart[id]"), function (figure) {
    var rows = readouts && readouts[figure.id];
    var box = figure.querySelector(".chart-box");
    var svg = box && box.querySelector("svg");
    var cross = svg && svg.querySelector("line.cross");
    var tip = figure.querySelector(".tip");
    // A screen reader hears the readout the arrow keys move to; the pointer's isn't announced.
    var said = figure.querySelector("[aria-live]");
    if (!rows || !rows.length || !cross || !tip) return;
    var at = -1;

    function scale() {
      return svg.getBoundingClientRect().width / svg.viewBox.baseVal.width;
    }

    function show(i, announce) {
      at = i;
      var row = rows[at];
      cross.x1.baseVal.value = row.x;
      cross.x2.baseVal.value = row.x;
      cross.style.visibility = "visible";
      tip.textContent = "";
      var head = document.createElement("strong");
      head.textContent = row.label;
      tip.appendChild(head);
      row.values.forEach(function (v) {
        var line = document.createElement("span");
        var swatch = document.createElement("span");
        swatch.className = "sw s" + (v.slot || 0);
        line.appendChild(swatch);
        line.appendChild(document.createTextNode(v.value + " " + v.name));
        tip.appendChild(line);
      });
      tip.hidden = false;
      if (announce && said) {
        said.textContent = row.label + ": " + row.values.map(function (v) {
          return v.value + " " + v.name;
        }).join(", ");
      }
      var left = svg.getBoundingClientRect().left - figure.getBoundingClientRect().left;
      tip.style.left = Math.max(8, Math.min(left + row.x * scale() + 12, figure.clientWidth - tip.offsetWidth - 8)) + "px";
    }

    function hide() {
      cross.style.visibility = "hidden";
      tip.hidden = true;
    }

    svg.addEventListener("pointermove", function (e) {
      var point = svg.createSVGPoint();
      point.x = e.clientX;
      point.y = e.clientY;
      var x = point.matrixTransform(svg.getScreenCTM().inverse()).x;
      var nearest = 0;
      rows.forEach(function (row, i) {
        if (Math.abs(row.x - x) < Math.abs(rows[nearest].x - x)) nearest = i;
      });
      show(nearest);
    });
    svg.addEventListener("pointerleave", hide);
    box.addEventListener("blur", hide);
    hides.push(hide);
    box.addEventListener("keydown", function (e) {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      e.preventDefault();
      var step = e.key === "ArrowRight" ? 1 : -1;
      var i = at < 0 ? rows.length - 1 : Math.max(0, Math.min(rows.length - 1, at + step));
      // preventDefault takes the arrows from the box's own scrolling, so the box follows the readout.
      var x = rows[i].x * scale();
      if (x < box.scrollLeft || x > box.scrollLeft + box.clientWidth) box.scrollLeft = x - box.clientWidth / 2;
      show(i, true);
    });
  });

  // Esc closes every readout wherever focus is, the pointer's too (WCAG 1.4.13: dismissible without moving).
  if (hides.length) {
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") return;
      hides.forEach(function (hide) {
        hide();
      });
    });
  }

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
