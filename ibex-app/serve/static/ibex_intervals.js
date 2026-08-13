/* IBEX interval wiring -- BUILD 19
 *
 * Purely ADDITIVE. It does not modify any existing JavaScript. It reads the
 * rendered score card, asks the server for the Wilson interval around that
 * PD, and injects the result. It also rebuilds the evidence panel body so the
 * stale pre-patch rows (709 ceiling, rule-of-three tail) stop being shown.
 *
 * Requires these element IDs, all of which already exist in ibex.html:
 *   #sVal   the score number
 *   #sBand  the band letter
 *   #sPd    the probability of default, rendered as a percentage
 *   #evBody the evidence panel body (optional -- skipped if absent)
 *
 * Requires these endpoints, both provided by serve/intervals.py:
 *   GET /api/v4/interval?pd=<float>
 *   GET /api/v4/intervals/table?limit=<int>
 *
 * To remove: delete the <script> tag. Nothing else changes.
 */
(function () {
  "use strict";

  var POLL_MS = 400;
  var lastPd = null;

  function el(id) {
    return document.getElementById(id);
  }

  function fmt(x, dp) {
    return (Math.round(x * Math.pow(10, dp)) / Math.pow(10, dp)).toFixed(dp);
  }

  function commas(n) {
    return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  /* Read the PD off the card. It is rendered as e.g. "4.5%". */
  function pdFromCard() {
    var node = el("sPd");
    if (!node) return null;
    var m = (node.textContent || "").match(/([0-9]+(?:\.[0-9]+)?)\s*%/);
    if (!m) return null;
    var pd = parseFloat(m[1]) / 100.0;
    if (!isFinite(pd) || pd <= 0 || pd >= 1) return null;
    return pd;
  }

  /* Container for the interval line, created once, directly after #sVal. */
  function intervalHost() {
    var host = el("sInterval");
    if (host) return host;
    var anchor = el("sVal");
    if (!anchor || !anchor.parentNode) return null;
    host = document.createElement("div");
    host.id = "sInterval";
    host.style.cssText =
      "margin-top:6px;font-size:13px;line-height:1.5;color:#8fae9c;" +
      "font-variant-numeric:tabular-nums;";
    if (anchor.nextSibling) {
      anchor.parentNode.insertBefore(host, anchor.nextSibling);
    } else {
      anchor.parentNode.appendChild(host);
    }
    return host;
  }

  function renderInterval(d) {
    var host = intervalHost();
    if (!host) return;

    var lo = fmt(d.score_lo, 1);
    var hi = fmt(d.score_hi, 1);
    var star = d.upper_is_policy
      ? ' <span style="color:#d8a657">*</span>'
      : "";

    var bandTxt =
      d.band_range && d.band_range !== d.band
        ? '<span style="color:#d8a657">' + d.band_range + "</span>"
        : d.band;

    var html =
      '<div style="color:#e8f3ec;font-size:14px">95% interval ' +
      lo +
      " to " +
      hi +
      star +
      "</div>" +
      "<div>band across the interval " +
      bandTxt +
      " &middot; width " +
      fmt(d.width, 1) +
      " points</div>" +
      "<div>rests on " +
      commas(d.n) +
      " comparable applicants, " +
      commas(d.k) +
      (d.k === 1 ? " default" : " defaults") +
      "</div>" +
      "<div>PD " +
      fmt(d.pd * 100, 2) +
      "% (" +
      fmt(d.pd_lo * 100, 2) +
      "% to " +
      fmt(d.pd_hi * 100, 2) +
      "%)</div>";

    if (d.upper_is_policy) {
      html +=
        '<div style="color:#d8a657">* the upper bound is the policy ceiling, ' +
        "not a measurement -- read it as at or below the floor</div>";
    }

    host.innerHTML = html;
  }

  function fetchInterval(pd) {
    fetch("/api/v4/interval?pd=" + encodeURIComponent(pd))
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (d) {
        if (d && typeof d.score_lo === "number") renderInterval(d);
      })
      .catch(function (e) {
        var host = intervalHost();
        if (host) {
          host.innerHTML =
            '<div style="color:#cf6a5a">interval unavailable (' +
            e.message +
            ") -- is serve/intervals.py wired into app.py?</div>";
        }
      });
  }

  /* Rebuild the evidence panel body from live data. */
  function refreshEvidence() {
    var body = el("evBody");
    if (!body) return;
    fetch("/api/v4/intervals/table?limit=8")
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (d) {
        var rows = (d && (d.rows || d.blocks)) || [];
        if (!rows.length) return;
        var ceil = rows[0];
        var h =
          '<div style="margin-bottom:10px;color:#e8f3ec">' +
          "Score ceiling <b>" +
          fmt(ceil.score, 1) +
          "</b> &middot; PD floor " +
          fmt(ceil.pd * 100, 3) +
          "%<br>" +
          "Ceiling rests on <b>" +
          commas(ceil.n) +
          "</b> people, <b>" +
          commas(ceil.k) +
          "</b> observed " +
          (ceil.k === 1 ? "default" : "defaults") +
          "</div>";
        h +=
          '<table style="width:100%;border-collapse:collapse;font-size:13px;' +
          'font-variant-numeric:tabular-nums"><tr style="color:#8fae9c;' +
          'text-align:right"><th style="text-align:left">block</th><th>n</th>' +
          "<th>defaults</th><th>score</th><th>95% interval</th><th>width</th>" +
          "<th>band</th></tr>";
        rows.forEach(function (r) {
          h +=
            '<tr style="text-align:right;border-top:1px solid #1e3b2a">' +
            '<td style="text-align:left">' +
            r.block +
            "</td><td>" +
            commas(r.n) +
            "</td><td>" +
            commas(r.k) +
            "</td><td>" +
            fmt(r.score, 1) +
            "</td><td>" +
            fmt(r.score_lo, 1) +
            " to " +
            fmt(r.score_hi, 1) +
            (r.upper_is_policy ? " *" : "") +
            "</td><td>" +
            fmt(r.width, 1) +
            "</td><td>" +
            (r.band_range || r.band) +
            "</td></tr>";
        });
        h += "</table>";
        h +=
          '<div style="margin-top:8px;color:#8fae9c;font-size:12px">' +
          "Wilson score intervals on observed default counts. Independence is " +
          "assumed within a block; the measured design effect across " +
          "origination weeks is 1.72x, so treat these widths as a lower bound. " +
          "* marks an upper bound set by the policy ceiling rather than by " +
          "data.</div>";
        body.innerHTML = h;
      })
      .catch(function () {
        /* leave the existing panel alone if the endpoint is not reachable */
      });
  }

  /* Poll the card. Cheap, and immune to however the score gets rendered. */
  function tick() {
    var pd = pdFromCard();
    if (pd !== null && pd !== lastPd) {
      lastPd = pd;
      fetchInterval(pd);
    }
  }

  function start() {
    refreshEvidence();
    tick();
    setInterval(tick, POLL_MS);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
