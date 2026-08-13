/* Ibex marketing site -- plain JS, no build step, no dependencies.
   Safe to edit: this file only controls the landing page at /home.
   It cannot affect scoring, the model, or the app at /ibex. */

(function () {
  "use strict";

  /* ---- mobile nav ---------------------------------------------------- */
  var toggle = document.getElementById("navtoggle");
  var links = document.getElementById("navlinks");
  if (toggle && links) {
    toggle.addEventListener("click", function () {
      links.classList.toggle("open");
    });
    links.addEventListener("click", function (e) {
      if (e.target.tagName === "A") links.classList.remove("open");
    });
  }

  /* ---- model section tabs -------------------------------------------- */
  /* To add a tab: add a <button class="tab" data-p="p7">Name</button>
     inside #tabs, and a matching <div class="pane" id="p7">...</div>. */
  var tabs = document.querySelectorAll("#tabs .tab");
  Array.prototype.forEach.call(tabs, function (t) {
    t.addEventListener("click", function () {
      Array.prototype.forEach.call(tabs, function (x) {
        x.classList.remove("on");
      });
      Array.prototype.forEach.call(document.querySelectorAll(".pane"), function (p) {
        p.classList.remove("on");
      });
      t.classList.add("on");
      var pane = document.getElementById(t.getAttribute("data-p"));
      if (pane) pane.classList.add("on");
    });
  });

  /* ---- deep link: /home#model?tab=p4 style support -------------------- */
  var wanted = new URLSearchParams(location.search).get("tab");
  if (wanted) {
    var btn = document.querySelector('#tabs .tab[data-p="' + wanted + '"]');
    if (btn) btn.click();
  }

  /* ---- live stats ------------------------------------------------------
     Optional. If the running server exposes /api/v4/evidence, the headline
     Gini figure refreshes itself so the marketing copy can never drift out
     of step with the deployed model. Fails silently when unavailable. */
  fetch("/api/v4/evidence")
    .then(function (r) {
      return r.ok ? r.json() : null;
    })
    .then(function (d) {
      if (!d) return;
      var g = d.gini || (d.discrimination && d.discrimination.gini);
      var lo = d.gini_ci_low || (d.discrimination && d.discrimination.ci_low);
      var hi = d.gini_ci_high || (d.discrimination && d.discrimination.ci_high);
      if (typeof g !== "number") return;
      var cells = document.querySelectorAll(".stat");
      if (cells.length < 2) return;
      cells[1].querySelector(".v").textContent = g.toFixed(2);
      if (typeof lo === "number" && typeof hi === "number") {
        cells[1].querySelector(".k").textContent =
          "Gini, 95% CI [" + lo.toFixed(2) + ", " + hi.toFixed(2) + "]";
      }
    })
    .catch(function () {});
})();
