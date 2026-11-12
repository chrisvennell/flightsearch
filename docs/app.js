(function () {
  "use strict";

  var STALE_HOURS = 16;
  var root = document.getElementById("root");
  var meta = document.getElementById("meta");

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null && text !== "") node.textContent = text;
    return node;
  }

  function ago(iso) {
    var seconds = (Date.now() - new Date(iso).getTime()) / 1000;
    if (!isFinite(seconds)) return "unknown";
    if (seconds < 90) return "just now";
    if (seconds < 5400) return Math.round(seconds / 60) + " min ago";
    if (seconds < 172800) return Math.round(seconds / 3600) + " h ago";
    return Math.round(seconds / 86400) + " d ago";
  }

  function sparkline(values) {
    var nums = values.filter(function (v) { return typeof v === "number"; });
    if (nums.length < 2) return null;
    var min = Math.min.apply(null, nums);
    var max = Math.max.apply(null, nums);
    var span = max - min || 1;
    var points = nums.map(function (v, i) {
      var x = (i / (nums.length - 1)) * 100;
      var y = 30 - ((v - min) / span) * 26 + 2;
      return x.toFixed(2) + "," + y.toFixed(2);
    }).join(" ");
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "spark");
    svg.setAttribute("viewBox", "0 0 100 34");
    svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("aria-label", "Price trend, low " + min + ", high " + max);
    var line = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
    line.setAttribute("points", points);
    svg.appendChild(line);
    return svg;
  }

  function card(item) {
    var safeLink = typeof item.link === "string" && item.link.indexOf("https://") === 0;
    var node = el(safeLink ? "a" : "div", "card");
    if (safeLink) {
      node.href = item.link;
      node.target = "_blank";
      node.rel = "noopener noreferrer";
    }
    var row = el("div", "row");
    var left = el("div");
    left.appendChild(el("div", "title", item.title));
    if (item.subtitle) left.appendChild(el("div", "subtitle", item.subtitle));
    row.appendChild(left);
    if (item.value || item.badge) {
      var right = el("div", "right");
      if (item.value) right.appendChild(el("div", "value", item.value));
      if (item.badge) {
        var tone = ["good", "warn", "bad"].indexOf(item.tone) >= 0 ? " " + item.tone : "";
        right.appendChild(el("span", "badge" + tone, item.badge));
      }
      row.appendChild(right);
    }
    node.appendChild(row);
    if (Array.isArray(item.history)) {
      var spark = sparkline(item.history);
      if (spark) node.appendChild(spark);
    }
    return node;
  }

  function render(data) {
    root.textContent = "";
    var ageHours = (Date.now() - new Date(data.updated).getTime()) / 3600000;
    meta.textContent = "Last run " + ago(data.updated);
    meta.className = ageHours > STALE_HOURS ? "stale" : "";

    var keys = Object.keys(data.agents || {});
    if (!keys.length) {
      root.appendChild(el("p", "empty", "No agents are enabled in config.json."));
      return;
    }
    keys.forEach(function (key) {
      var agent = data.agents[key];
      var section = el("section");
      var head = el("div", "agent-head");
      head.appendChild(el("h2", null, agent.title || key));
      var bits = [];
      if (agent.budget) bits.push(agent.budget.used + " of " + agent.budget.limit + " calls this month");
      head.appendChild(el("span", "sub", bits.join(", ")));
      section.appendChild(head);
      if (agent.summary) section.appendChild(el("div", "sub", agent.summary));
      if (agent.error) section.appendChild(el("div", "error", agent.error));
      (agent.items || []).forEach(function (item) { section.appendChild(card(item)); });
      if (!agent.items || !agent.items.length) section.appendChild(el("p", "empty", "Nothing to show yet."));
      root.appendChild(section);
    });
  }

  function load() {
    meta.textContent = "Loading";
    fetch("data/hub.json?t=" + Date.now(), { cache: "no-store" })
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(render)
      .catch(function () {
        meta.textContent = "No data yet. Run the workflow once from the Actions tab.";
        meta.className = "stale";
      });
  }

  document.getElementById("refresh").addEventListener("click", load);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) load();
  });
  load();

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("sw.js").catch(function () {});
  }
})();
