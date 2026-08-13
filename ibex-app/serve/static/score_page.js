/* Ibex credential page. Vanilla JS, no dependencies, no database.
 * Reads:
 *   GET /api/v4/score/current   the live score record
 *   GET /api/v4/score/history   previous scores from the JSONL store
 *   GET /api/v4/chain/status    whether scores are anchored on chain
 */
(function () {
  "use strict";

  function el(id) { return document.getElementById(id); }
  function txt(node, s) { if (node) { node.textContent = s; } }
  function esc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function getJSON(url) {
    return fetch(url, { credentials: "same-origin" }).then(function (r) {
      if (!r.ok) { throw new Error(url + " -> " + r.status); }
      return r.json();
    });
  }
  function pretty(name) {
    return String(name || "").replace(/_/g, " ").replace(/\s+/g, " ").trim()
      .replace(/^./, function (c) { return c.toUpperCase(); });
  }
  var RISK = { A: "Lower risk", B: "Lower risk", C: "Medium risk",
               D: "Higher risk", E: "Higher risk" };

  function renderScore(rec) {
    if (!rec) {
      txt(el("sc-name"), "No score yet");
      txt(el("sc-score"), "--");
      txt(el("sc-band"), "--");
      el("sc-interval").innerHTML =
        '<span class="body-sm text-secondary">Connect a bank and run the ' +
        'demo to generate a credential.</span>';
      return;
    }
    txt(el("sc-name"), rec.name || rec.bank_name || rec.user_id || "Your credential");
    txt(el("sc-score"), rec.score === null ? "--" : Math.round(rec.score));
    var band = rec.band || "--";
    var bandNode = el("sc-band");
    txt(bandNode, RISK[band] || ("Band " + band));
    if (band === "D" || band === "E") {
      bandNode.style.color = "var(--status-rejected-text)";
    } else if (band === "C") {
      bandNode.style.color = "var(--status-pending-text)";
    } else {
      bandNode.style.color = "var(--status-verified-text)";
    }

    var lines = [];
    lines.push("Band " + band + (rec.scored_at ? " &middot; scored " + esc(rec.scored_at) : ""));
    if (typeof rec.pd === "number") {
      lines.push("Probability of default " + (rec.pd * 100).toFixed(2) + "%");
    }
    el("sc-interval").innerHTML = lines.map(function (l) {
      return '<span class="body-sm text-secondary">' + l + "</span>";
    }).join("");

    var src = [];
    src.push(row("Open Banking", true));
    src.push(row("Account-holder name", !!rec.bank_name_verified));
    src.push(row("Model features (" + (rec.n_features || 0) + ")", true));
    el("sc-sources").innerHTML = src.join("");

    if (!rec.bank_name_verified) {
      el("sc-warning").innerHTML =
        '<p class="sc-note">Your bank did not return an account-holder ' +
        'name, so this credential is bound to your email address alone. ' +
        'A business cannot verify it against your name on chain.</p>';
    }

    var stats = [
      ["DETECTED MONTHLY INCOME", rec.income === null || rec.income === undefined
        ? "n/a" : "&pound;" + Number(rec.income).toLocaleString(
            "en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 })],
      ["CREDIT LINES FOUND", rec.credit_lines === null ? "n/a" : rec.credit_lines],
      ["MONTHS OF HISTORY", rec.months === null ? "n/a" : rec.months],
      ["MODEL FEATURES", rec.n_features === null ? "n/a" : rec.n_features]
    ];
    el("sc-stats").innerHTML = stats.map(function (s) {
      return '<div class="detail-stat-block"><div class="detail-stat-label">' +
        s[0] + '</div><div class="detail-stat-value">' + s[1] + "</div></div>";
    }).join("");

    factors(el("sc-negative"), rec.issues || rec.negative_factors,
            "Nothing flagged.", true);
    factors(el("sc-positive"), rec.positives || rec.positive_factors,
            "Nothing yet.", false);
  }

  function row(label, ok) {
    return '<div class="source-row"><span class="body-md">' + esc(label) +
      '</span><span style="color:' +
      (ok ? "var(--status-verified-icon)" : "var(--status-expired-icon)") +
      '">' + (ok ? "&#10003;" : "&mdash;") + "</span></div>";
  }

  function factors(node, list, empty, negative) {
    if (!node) { return; }
    if (!list || !list.length) {
      node.innerHTML = '<span class="body-sm text-secondary">' + empty + "</span>";
      return;
    }
    // The advice text comes from the server (serve/score_advice.py), which is
    // keyed on the features the current build actually uses. Keeping it in one
    // place is deliberate: the previous bug was two copies drifting apart.
    node.innerHTML = list.slice(0, 4).map(function (f) {
      if (f && f.title) {
        var tag = (negative && f.actionable === false)
          ? '<span class="adv-tag">not changeable right now</span>' : "";
        return '<div class="adv-item"><b>' + esc(f.title) + "</b>" + tag +
          (f.detail ? '<span class="adv-body">' + esc(f.detail) + "</span>" : "") +
          "</div>";
      }
      var name = (f && (f.name || f.feature)) || f;
      return '<div class="adv-item"><b>' + esc(pretty(name)) + "</b></div>";
    }).join("");
  }

  function renderHistory(data) {
    var rows = (data && data.history) || [];
    var node = el("sc-history");
    if (!rows.length) {
      node.innerHTML = '<p class="body-sm text-secondary">No previous ' +
        'scores recorded yet. Each run of the demo appends one entry.</p>';
    } else {
      node.innerHTML = rows.map(function (r) {
        var when = String(r.timestamp || "").slice(0, 10);
        var d = r.delta;
        var deltaHtml = '<span class="body-sm text-secondary">&mdash;</span>';
        if (typeof d === "number" && d !== 0) {
          deltaHtml = '<span class="' + (d > 0 ? "sc-delta-up" : "sc-delta-down") +
            '">' + (d > 0 ? "+" : "") + d + "</span>";
        }
        return '<div class="history-row"><div class="flex gap-md items-center">' +
          '<span class="body-sm text-secondary">' + esc(when) + "</span>" +
          '<span class="label-lg">' + Math.round(r.score) + "</span>" +
          deltaHtml + "</div>" +
          '<span class="body-sm text-secondary">Band ' + esc(r.band || "?") +
          "</span></div>";
      }).join("");
    }
    if (data && data.store) {
      txt(el("sc-store-note"), "Stored as one JSON line per score in " + data.store);
    }
  }

  function renderChain(c) {
    var node = el("sc-chain");
    if (!c) {
      node.innerHTML = '<p class="body-sm text-secondary">Chain status ' +
        "unavailable.</p>";
      return;
    }
    var badge = '<span class="sc-chain-state ' +
      (c.enabled ? "sc-chain-on" : "sc-chain-off") + '">' +
      '<span class="sc-dot"></span>' +
      (c.enabled ? "Anchoring on " + esc(c.network) : "Chain off") + "</span>";

    var kv = "";
    if (c.enabled) {
      kv = '<dl class="sc-kv">' +
        "<dt>Network</dt><dd>" + esc(c.network) +
        (c.chain_id ? " (id " + esc(c.chain_id) + ")" : "") + "</dd>" +
        "<dt>Contract</dt><dd>" + esc(c.contract) + "</dd>" +
        "</dl>";
      if (c.explorer_base && c.contract) {
        kv += '<a class="btn btn-secondary btn-medium btn-full" target="_blank" ' +
          'rel="noopener" href="' + esc(c.explorer_base) + "/address/" +
          esc(c.contract) + '">View contract on ' + esc(c.explorer || "explorer") +
          "</a>";
      }
    } else {
      kv = '<p class="sc-note">' + esc(c.reason ||
        "Scores are not being anchored on chain.") + "</p>";
    }

    node.innerHTML = badge + kv +
      '<p class="body-sm text-secondary">An anchor proves the score is ' +
      'authentic and unaltered. It does not prove the person presenting ' +
      'it is its subject.</p>';
  }

  document.addEventListener("DOMContentLoaded", function () {
    getJSON("/api/v4/score/current")
      .then(function (d) { renderScore(d && d.score); })
      .catch(function () { renderScore(null); });
    getJSON("/api/v4/score/history?limit=10")
      .then(renderHistory)
      .catch(function () {
        el("sc-history").innerHTML = '<p class="body-sm text-secondary">' +
          "Sign in to see your previous scores.</p>";
      });
    getJSON("/api/v4/chain/status")
      .then(renderChain)
      .catch(function () { renderChain(null); });
  });
})();
