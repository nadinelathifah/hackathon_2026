#!/usr/bin/env python3
'''BUILD 22 -- install the declared-attributes form into the v4 page (ibex.html).

WHY THIS EXISTS
---------------
The v5 model's 2nd and 5th most important features (declared_income_type_code,
declared_employment_code) plus declared_income_gap and declared_provided all
come from user declarations. Without a form, /connect silently attaches its
DEFAULTS (SALARIED / MORE_ONE_YEAR / OWNED / stated_income 0) as if the user
had declared them -- the model was scoring fiction.

This installer patches serve/static/ibex.html:
  1. inserts a declaration form (income, employment tenure, income type,
     housing) inside the connect card, above the Connect button;
  2. rewires the Connect button to append the answers to the /connect URL so
     they travel through OAuth state (_PENDING) and attach at callback, exactly
     like the defaults did;
  3. explicitly sends education="" -- education was dropped from the model by
     policy (fairness proxy), so the form does not ask for it and the backend
     default is neutralised;
  4. "Prefer not to say" sends an empty value, which the feature layer treats
     as genuinely missing (native NaN), NOT as a fabricated default.

Run ONCE from the repo root:
    py -3.13 install_declared.py
Idempotent. Backs up to serve/static/ibex.html.declared.bak. Dies loudly if an
anchor is missing. DELETE this file after a successful run.
'''
import os
import shutil
import sys

P = os.path.join("serve", "static", "ibex.html")
MARK = 'id="declaredBox"'

ANCHOR_HTML = '    <button id="btnConnect">Connect bank account</button>'
ANCHOR_JS = 'const r=await api("/api/v4/tl/connect-url");location.href=r.url;'

FORM = '''    <div id="declaredBox" style="margin:14px 0 16px;padding:14px 14px 12px;border:1px solid #1e3b2a;border-radius:10px;background:#0e2018;text-align:left">
      <div style="font-size:14px;font-weight:600;color:#e8f3ec;margin-bottom:4px">Before you connect <span style="color:#8fae9c;font-weight:400">(optional, ~20 seconds)</span></div>
      <p class="sub" style="margin:0 0 10px;font-size:12px;line-height:1.45">Four declarations improve your score&rsquo;s accuracy. Declared income is cross-checked against what your bank shows. Prefer not to say is fine &mdash; unanswered fields are treated as <i>missing</i>, never as a fabricated default.</p>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
        <label style="display:block;font-size:12px;color:#8fae9c">Monthly income before tax (&pound;)
          <input id="dSal" type="number" min="0" step="50" placeholder="e.g. 2500" style="width:100%;box-sizing:border-box;margin-top:4px;padding:8px;border:1px solid #1e3b2a;border-radius:8px;background:#11241a;color:#e8f3ec"></label>
        <label style="display:block;font-size:12px;color:#8fae9c">Employment tenure
          <select id="dEmp" style="width:100%;box-sizing:border-box;margin-top:4px;padding:8px;border:1px solid #1e3b2a;border-radius:8px;background:#11241a;color:#e8f3ec">
            <option value="" selected>Prefer not to say</option>
            <option value="MORE_ONE_YEAR">Employed &gt; 1 year</option>
            <option value="LESS_ONE_YEAR">Employed &lt; 1 year</option>
            <option value="SELF_EMPLOYED">Self-employed</option>
            <option value="RETIRED">Retired</option>
            <option value="UNEMPLOYED">Not currently employed</option>
          </select></label>
        <label style="display:block;font-size:12px;color:#8fae9c">Income type
          <select id="dInc" style="width:100%;box-sizing:border-box;margin-top:4px;padding:8px;border:1px solid #1e3b2a;border-radius:8px;background:#11241a;color:#e8f3ec">
            <option value="" selected>Prefer not to say</option>
            <option value="SALARIED">Salaried employment</option>
            <option value="SELF_EMPLOYED_INCOME">Self-employed income</option>
            <option value="PENSION">Pension</option>
            <option value="BENEFITS">State benefits</option>
            <option value="OTHER">Other</option>
          </select></label>
        <label style="display:block;font-size:12px;color:#8fae9c">Housing
          <select id="dHou" style="width:100%;box-sizing:border-box;margin-top:4px;padding:8px;border:1px solid #1e3b2a;border-radius:8px;background:#11241a;color:#e8f3ec">
            <option value="" selected>Prefer not to say</option>
            <option value="OWNED">Homeowner &ndash; no mortgage</option>
            <option value="OWNED_WITH_MORTGAGE">Homeowner &ndash; mortgage</option>
            <option value="RENTING">Renting</option>
            <option value="LIVING_WITH_FAMILY">Living with family</option>
            <option value="OTHER">Other</option>
          </select></label>
      </div>
      <p class="sub" style="margin:10px 0 0;font-size:11px;color:#8fae9c">We do not ask about education &mdash; it was removed from the model as a fairness precaution.</p>
    </div>
'''

NEW_JS = '''const r=await api("/api/v4/tl/connect-url");
  const u=new URL(r.url,location.origin);
  const g=id=>{const el=document.getElementById(id);return el?el.value.trim():"";};
  u.searchParams.set("employment",g("dEmp"));
  u.searchParams.set("income_type",g("dInc"));
  u.searchParams.set("housing",g("dHou"));
  if(g("dSal"))u.searchParams.set("stated_income",g("dSal"));
  u.searchParams.set("education","");
  location.href=u.toString();'''


def die(msg: str) -> None:
    print("ERROR: " + msg)
    sys.exit(1)


def main() -> None:
    if not os.path.exists(P):
        die(f"{P} not found -- run this from the repo root.")
    src = open(P, encoding="utf-8").read()
    if MARK in src:
        print("declared form already installed -- nothing to do.")
        return
    if src.count(ANCHOR_HTML) != 1:
        die(f"connect-button anchor not unique (found {src.count(ANCHOR_HTML)}); STOP.")
    if src.count(ANCHOR_JS) != 1:
        die(f"connect-handler anchor not unique (found {src.count(ANCHOR_JS)}); STOP.")

    out = src.replace(ANCHOR_HTML, FORM + ANCHOR_HTML)
    out = out.replace(ANCHOR_JS, NEW_JS)

    shutil.copyfile(P, P + ".declared.bak")
    open(P, "w", encoding="utf-8").write(out)
    print("  + declaration form inserted above the Connect button")
    print("  + Connect button now appends employment/income_type/housing/stated_income")
    print("  + education explicitly neutralised (dropped from the model by policy)")
    print("backup: serve/static/ibex.html.declared.bak")
    print("Hard-refresh /ibex (Ctrl+F5) and reconnect to see it.")
    print("DELETE install_declared.py now -- it is a run-once patcher.")


if __name__ == "__main__":
    main()
