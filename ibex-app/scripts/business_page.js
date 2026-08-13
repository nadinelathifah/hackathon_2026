(function () {
  var tabs = document.querySelectorAll(".bz-tab");
  var result = document.getElementById("result");

  tabs.forEach(function (t) {
    t.addEventListener("click", function () {
      tabs.forEach(function (x) { x.classList.remove("active"); });
      t.classList.add("active");
      ["pane-details", "pane-file", "pane-hash"].forEach(function (id) {
        document.getElementById(id).hidden = (id !== t.dataset.pane);
      });
      result.hidden = true;
    });
  });

  function esc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function row(k, v) {
    if (v === null || v === undefined || v === "") { return ""; }
    return "<tr><td style='color:var(--gray-500);width:210px'>" + esc(k) +
           "</td><td>" + esc(v) + "</td></tr>";
  }

  function pending(msg) {
    result.hidden = false;
    result.innerHTML = "<div class='bz-verdict bz-pending'>" +
      "<span class='bz-dot'></span>" + esc(msg) + "</div>" +
      "<div class='bz-body'><p class='bz-help' style='margin:0'>Reading the " +
      "contract. This can take a few seconds.</p></div>";
  }

  function failure(msg) {
    result.hidden = false;
    result.innerHTML = "<div class='bz-verdict bz-no'>" +
      "<span class='bz-dot'></span>Could not verify</div>" +
      "<div class='bz-body'><p class='bz-help' style='margin:0'>" +
      esc(msg) + "</p></div>";
  }

  function render(d) {
    var matched = d.match === true || d.found === true;
    var cls = matched ? "bz-ok" : "bz-no";
    var head = matched ? "Verified by Ibex" : "No matching record";
    var claimed = d.claimed || {};
    var rec = d.record || d.onchain || {};
    var html = "<div class='bz-verdict " + cls + "'>" +
      "<span class='bz-dot'></span>" + esc(head) + "</div><div class='bz-body'>";
    if (d.verdict) {
      html += "<p class='bz-help' style='margin:0 0 20px'>" + esc(d.verdict) + "</p>";
    }
    html += "<table class='bz-table'><tbody>";
    html += row("Name checked", claimed.name);
    html += row("Score claimed", claimed.score);
    html += row("Score on chain", rec.score);
    html += row("Band", claimed.band || rec.band);
    html += row("Issued", claimed.timestamp || rec.timestamp);
    html += row("Model version", claimed.modelVersion);
    html += row("Reference", d.user_hash);
    html += row("Score event hash", rec.scoreEventHash);
    html += row("Transaction", rec.txHash);
    html += row("Network", d.network);
    html += row("Contract", d.contract);
    html += "</tbody></table>";
    if (d.explorer) {
      html += "<p style='margin:16px 0 0'><a class='btn btn-secondary btn-medium' " +
        "target='_blank' rel='noopener' href='" + esc(d.explorer) +
        "'>View on the block explorer</a></p>";
    }
    if (d.limitation) {
      html += "<p class='bz-note'>" + esc(d.limitation) + "</p>";
    }
    html += "</div>";
    result.hidden = false;
    result.innerHTML = html;
  }

  function post(url, body, label) {
    pending(label);
    fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().then(function (j) { return { ok: r.ok, j: j }; });
    }).then(function (o) {
      if (!o.ok) { failure(o.j.detail || "The server rejected that request."); return; }
      render(o.j);
    }).catch(function (e) { failure(String(e)); });
  }

  document.getElementById("go-details").addEventListener("click", function () {
    var name = document.getElementById("d-name").value.trim();
    var email = document.getElementById("d-email").value.trim();
    var score = document.getElementById("d-score").value.trim();
    if (!name || !email) { failure("Enter both the name and the email."); return; }
    post("/api/v4/verify/identity", {
      name: name,
      email: email,
      claimed_score: score ? parseFloat(score) : null,
      claimed_band: document.getElementById("d-band").value || null
    }, "Checking the record");
  });

  document.getElementById("go-file").addEventListener("click", function () {
    var txt = document.getElementById("f-json").value.trim();
    if (!txt) { failure("Paste the score-event.json contents first."); return; }
    post("/api/v4/verify/event", { event_json: txt }, "Hashing and comparing");
  });

  document.getElementById("go-hash").addEventListener("click", function () {
    var h = document.getElementById("h-hash").value.trim();
    if (!h) { failure("Enter the reference."); return; }
    post("/api/v4/verify/hash", { user_hash: h }, "Reading the record");
  });
})();
