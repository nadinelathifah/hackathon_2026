"""BUILD 20 -- FastAPI serving app for the open-banking credit-score demo.

Endpoints
  GET  /                -> the dashboard (serve/static/index.html)
  GET  /api/health      -> artifact + build status
  GET  /api/profiles    -> the mock open-banking risk profiles
  POST /api/score       -> declared form + chosen mock account -> credit score

Run (from the project root, after building artifacts with calibrate_score.py):
  uvicorn serve.app:app --reload --port 8000
then open http://127.0.0.1:8000

The artifacts directory can be overridden with the OBCREDIT_ARTIFACTS env var;
it defaults to <project>/artifacts.

CHANGES FROM BUILD 18 (all additive; no existing behaviour removed)
  1. /callback now passes `declared` and `months` into client.fetch_user().
     Previously the declared form was attached to the payload only AFTER the
     fetch, so the client logged "no declared attributes passed to fetch_user"
     and every declared_* feature fell back to a default.
  2. The realised history window is measured from the returned payload and
     carried through to the score result as months_of_history, so the
     dashboard can display it. /api/score already did this for mock accounts;
     /api/score-live did not, which is why the field rendered blank.
  3. credit_accounts is counted and returned, separately from num_obligations.
     They are different things and were being conflated.
  4. /api/score-live no longer destroys the connection on first use. The token
     is reusable until it expires, so re-scoring after a re-login works
     instead of returning 400.
  5. /connect accepts a months parameter.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import sys
import time
from datetime import date
from typing import Dict, List, Optional

from fastapi.responses import FileResponse
from serve.score_event import router as score_event_router

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from serve.mock_ob import (PROFILES, build_from_profile,        # noqa: E402
                           build_custom, DEFAULT_MONTHS)
from serve.score_service import ScoringService                  # noqa: E402
from obcredit.truelayer.client import TrueLayerDataClient        # noqa: E402
from serve.history import realised_history, count_credit_accounts  # BUILD 19 history fix

log = logging.getLogger("serve.app")

ARTIFACTS = os.environ.get("OBCREDIT_ARTIFACTS",
                           os.path.join(_ROOT, "artifacts"))
STATIC_DIR = os.path.join(_HERE, "static")

# ---- TrueLayer sandbox OAuth (env-driven; see serve/README.md) ------------
TL_CLIENT_ID = os.environ.get("TRUELAYER_CLIENT_ID", "")
TL_CLIENT_SECRET = os.environ.get("TRUELAYER_CLIENT_SECRET", "")
TL_REDIRECT_URI = os.environ.get("TRUELAYER_REDIRECT_URI",
                                 "http://localhost:8000/callback")
TL_SANDBOX = os.environ.get("TRUELAYER_SANDBOX", "1") not in ("0", "false", "False")

# How long a completed bank connection stays scoreable, in seconds.
TL_TOKEN_TTL = int(os.environ.get("IBEX_TL_TOKEN_TTL", "3600"))

# In-memory demo stores. Fine for a single-user local demo; NOT for production
# (use a signed session / server-side store there).
_PENDING: Dict[str, dict] = {}      # oauth state -> {declared, case_id, months}
_CONNECTED: Dict[str, dict] = {}    # one-time token -> {"payload": ...}

app = FastAPI(title="Open-Banking Credit Score", version="BUILD 20")

_service: Optional[ScoringService] = None
_load_error: Optional[str] = None


def get_service() -> ScoringService:
    """Lazily load the model artifacts on first use so the app can boot (and
    serve a helpful error) even before calibrate_score.py has been run."""
    global _service, _load_error
    if _service is None:
        try:
            _service = ScoringService(ARTIFACTS)
            _load_error = None
        except Exception as e:  # surfaced by /api/health and /api/score
            _load_error = str(e)
            raise
    return _service


# =========================================================================== #
# Payload introspection helpers
#
# The canonical payload shape differs a little between the mock builder and
# the TrueLayer adapter, so these walk the structure defensively rather than
# assuming key names. They never raise -- a missing value returns None and the
# dashboard shows a dash, exactly as before.
# =========================================================================== #

_DATE_KEYS = ("timestamp", "booking_date", "booking_datetime", "date",
              "value_date", "transaction_date", "posted_at")
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _walk(obj, depth: int = 0):
    """Yield every dict nested anywhere inside obj."""
    if depth > 8:
        return
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk(v, depth + 1)


def _collect_dates(payload) -> List[str]:
    out: List[str] = []
    for d in _walk(payload):
        for key in _DATE_KEYS:
            v = d.get(key)
            if isinstance(v, str):
                m = _DATE_RE.search(v)
                if m:
                    out.append(m.group(1))
            elif hasattr(v, "isoformat"):
                try:
                    out.append(v.isoformat()[:10])
                except Exception:
                    pass
    return out


def _legacy_realised_history(payload) -> dict:
    """Measure the transaction window actually returned by the provider.

    Returns {} when no dates can be found, so callers can fall back to the
    requested window rather than displaying a wrong number.
    """
    dates = _collect_dates(payload)
    if not dates:
        return {}
    first, last = min(dates), max(dates)
    try:
        d0 = date.fromisoformat(first)
        d1 = date.fromisoformat(last)
    except Exception:
        return {}
    days = (d1 - d0).days
    if days < 0:
        return {}
    return {
        "history_start": first,
        "history_end": last,
        "history_days": days,
        "months_of_history": round(days / 30.44, 1),
    }


def _legacy_count_credit_accounts(payload) -> Optional[int]:
    """Count cards and loans. This is NOT the same as num_obligations, which
    counts active payment commitments detected in the transaction stream."""
    for d in _walk(payload):
        for key in ("credit_accounts", "cards", "credit_cards"):
            v = d.get(key)
            if isinstance(v, list):
                return len(v)
            if isinstance(v, int):
                return v
    return None


class ScoreRequest(BaseModel):
    employment: str = Field("MORE_ONE_YEAR", description="employment tenure bucket")
    income_type: str = Field("SALARIED", description="declared income type")
    education: str = Field("HIGHER_EDU", description="declared education level")
    housing: str = Field("OWNED", description="declared housing situation")
    stated_income: float = Field(3200.0, ge=0, description="stated monthly income")
    profile: str = Field("clean", description="which mock open-banking account")
    case_id: str = Field("demo-applicant", description="identifier for the pull")
    months: int = Field(DEFAULT_MONTHS, ge=3, le=60,
                        description="months of synthetic history to generate")
    custom: Optional[Dict[str, object]] = Field(
        None, description="when profile=='custom', the account spec to build "
                          "(loan_instalment, rent, missed_cycles, nsf_cycles, "
                          "gambling_monthly, overdraft_cycles, income_freq, ...)")

    def declared(self) -> Dict[str, object]:
        return {
            "employment": self.employment,
            "income_type": self.income_type,
            "education": self.education,
            "housing": self.housing,
            "stated_income": float(self.stated_income),
        }


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    path = os.path.join(STATIC_DIR, "index.html")
    if not os.path.exists(path):
        raise HTTPException(500, "dashboard not found")
    with open(path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/api/health")
def health() -> JSONResponse:
    ok = True
    detail = "artifacts loaded"
    build = None
    try:
        svc = get_service()
        build = svc.card.get("build")
    except Exception as e:
        ok = False
        detail = str(e)
    return JSONResponse({"ok": ok, "detail": detail, "build": build,
                         "artifacts_dir": ARTIFACTS})


@app.get("/api/profiles")
def profiles() -> JSONResponse:
    out = {k: v["label"] for k, v in PROFILES.items()}
    out["custom"] = "Custom (define the account behaviour yourself)"
    return JSONResponse(out)


@app.post("/api/score")
def score(req: ScoreRequest) -> JSONResponse:
    try:
        svc = get_service()
    except Exception as e:
        raise HTTPException(503, f"model not ready: {e}")

    if req.profile != "custom" and req.profile not in PROFILES:
        raise HTTPException(400, f"unknown profile '{req.profile}'")

    try:
        if req.profile == "custom":
            spec = dict(req.custom or {})
            spec.setdefault("months", int(req.months))
            payload = build_custom(
                case_id=req.case_id, as_of=date.today(),
                monthly_income=float(req.stated_income), spec=spec,
                declared=req.declared())
            label = "Custom account"
        else:
            payload = build_from_profile(
                case_id=req.case_id, as_of=date.today(),
                monthly_income=float(req.stated_income), profile=req.profile,
                declared=req.declared(), months=int(req.months))
            label = PROFILES[req.profile]["label"]
    except Exception as e:
        raise HTTPException(400, f"could not build mock account: {e}")

    try:
        result = svc.score_payload(payload)
    except Exception as e:
        raise HTTPException(500, f"scoring failed: {e}")

    result["profile"] = req.profile
    result["profile_label"] = label
    result["source"] = "mock"
    result["months_of_history"] = int(req.months)

    ca = count_credit_accounts(payload)
    if ca is not None:
        result["credit_accounts"] = ca
    return JSONResponse(result)


# =========================================================================== #
# Live TrueLayer sandbox OAuth flow
#   GET  /connect         stash the declared form, redirect to TrueLayer auth
#   GET  /callback          branded loader page, answers instantly
#   GET  /callback/process  exchange code, pull the account, stash, return the /ibex URL
#   POST /api/score-live  score the pulled account with the SAME f() as the rest
# =========================================================================== #

def _tl_configured() -> bool:
    return bool(TL_CLIENT_ID and TL_CLIENT_SECRET)


@app.get("/api/tl/status")
def tl_status() -> JSONResponse:
    return JSONResponse({"configured": _tl_configured(),
                         "sandbox": TL_SANDBOX,
                         "redirect_uri": TL_REDIRECT_URI})


@app.get("/connect")
def connect(employment: str = "MORE_ONE_YEAR", income_type: str = "SALARIED",
            education: str = "", housing: str = "",
            stated_income: float = 0.0, case_id: str = "demo-applicant",
            months: int = DEFAULT_MONTHS):
    if not _tl_configured():
        raise HTTPException(503, "TrueLayer credentials are not configured. Set "
                                 "TRUELAYER_CLIENT_ID and TRUELAYER_CLIENT_SECRET "
                                 "then restart the server.")
    # The provider caps how far back it will go; clamp rather than fail.
    months = max(3, min(int(months or DEFAULT_MONTHS), 24))

    state = secrets.token_urlsafe(24)
    declared = {"employment": employment, "income_type": income_type,
                "education": education, "housing": housing,
                "stated_income": float(stated_income or 0.0)}
    # Unanswered fields stay missing -- never recorded as a fabricated
    # default. Housing and education are no longer asked at all.
    declared = {k: v for k, v in declared.items()
                if v not in ("", None) and not (k == "stated_income" and not v)}
    _PENDING[state] = {
        "declared": declared,
        "case_id": case_id,
        "months": months,
    }
    client = TrueLayerDataClient(TL_CLIENT_ID, TL_CLIENT_SECRET, sandbox=TL_SANDBOX)
    url = client.authorization_url(redirect_uri=TL_REDIRECT_URI, state=state)
    return RedirectResponse(url, status_code=302)


_CALLBACK_LOADER_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connecting your bank \u00b7 Ibex Credit</title>
<style>
:root{--bg-inverse:#0B2A20;--bg-brand:#0B3D2E;--lime-500:#D0FF71}
*{box-sizing:border-box}
body{margin:0;font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:var(--bg-inverse)}
.ibex-loader{position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;background:var(--bg-inverse);transition:opacity 0.4s ease, visibility 0.4s ease}
.ibex-loader.ibex-loader-hidden{opacity:0;visibility:hidden;pointer-events:none}
.ibex-loader-col{display:flex;flex-direction:column;align-items:center;gap:22px;padding:0 24px}
.ibex-loader-mark{width:64px;height:64px;border-radius:50%;display:flex;align-items:center;justify-content:center;background:var(--bg-brand);box-shadow:0 0 0 0 rgba(208,255,113,.5);animation:ibex-pulse 1.2s ease-out infinite}
@keyframes ibex-pulse{0%{box-shadow:0 0 0 0 rgba(208,255,113,.45)}70%{box-shadow:0 0 0 22px rgba(208,255,113,0)}100%{box-shadow:0 0 0 0 rgba(208,255,113,0)}}
@media (prefers-reduced-motion: reduce){.ibex-loader-mark{animation:none}}
.ibex-loader-text{color:#fff;font-size:16px;font-weight:600;letter-spacing:-.01em;text-align:center}
.ibex-loader-sub{color:#D4EEE4;font-size:13px;opacity:.75;max-width:340px;text-align:center;line-height:1.5}
.ibex-back{display:none;color:var(--lime-500);font-size:14px;text-decoration:none;font-weight:600}
</style>
</head>
<body>
<div id="ibex-loader" class="ibex-loader">
  <div class="ibex-loader-col">
    <div class="ibex-loader-mark">
      <svg width="40" height="40" viewBox="0 0 24 24" fill="none"><path d="M2 20L8.5 7l3.5 6 1.8-2.8L22 20H2z" fill="white"/><path d="M8.5 7l3.5 6 1.8-2.8L22 20H13.8z" fill="var(--lime-500)"/></svg>
    </div>
    <div class="ibex-loader-text" id="ibex-load-text">Securing your bank connection</div>
    <div class="ibex-loader-sub" id="ibex-load-sub">Pulling up to 24 months of transactions from your bank. This can take up to a minute.</div>
    <a class="ibex-back" id="ibex-back" href="/">Back to start</a>
  </div>
</div>
<script>
(async function(){
  try{
    const r = await fetch("/callback/process" + (location.search || ""), {credentials:"same-origin"});
    const j = await r.json().catch(function(){ return {}; });
    if(r.ok && j.ok && j.redirect){
      const loader = document.getElementById("ibex-loader");
      if(loader){ loader.classList.add("ibex-loader-hidden"); }
      setTimeout(function(){ location.replace(j.redirect); }, 350);
      return;
    }
    throw new Error(j.error || ("HTTP " + r.status));
  }catch(e){
    const mark = document.querySelector(".ibex-loader-mark");
    if(mark){ mark.style.animation = "none"; }
    document.getElementById("ibex-load-text").textContent = "The connection could not be completed";
    document.getElementById("ibex-load-sub").textContent = String((e && e.message) || e);
    document.getElementById("ibex-back").style.display = "";
  }
})();
</script>
</body>
</html>"""


@app.get("/callback")
def callback(code: str = "", state: str = "", error: str = ""):
    # Answer instantly with the branded loader page. The page itself calls
    # /callback/process, which does the slow exchange + fetch. This removes
    # the long white screen while the bank data is pulled.
    if error:
        return HTMLResponse(f"<p>TrueLayer returned an error: {error}. "
                            f"<a href='/'>Back to dashboard</a></p>",
                            status_code=400)

    pending = _PENDING.get(state)
    if not code or pending is None:
        return HTMLResponse("<p>Invalid or expired connection attempt. "
                            "<a href='/'>Back to dashboard</a></p>",
                            status_code=400)
    return HTMLResponse(_CALLBACK_LOADER_HTML)


@app.get("/callback/process")
def callback_process(code: str = "", state: str = "", error: str = ""):
    if error:
        return JSONResponse({"ok": False,
                             "error": f"TrueLayer returned an error: {error}"},
                            status_code=400)

    pending = _PENDING.pop(state, None)
    if not code or pending is None:
        return JSONResponse({"ok": False,
                             "error": "Invalid or expired connection attempt."},
                            status_code=400)

    declared = pending.get("declared") or {}
    months = int(pending.get("months") or DEFAULT_MONTHS)

    try:
        client = TrueLayerDataClient(TL_CLIENT_ID, TL_CLIENT_SECRET,
                                     sandbox=TL_SANDBOX)
        client.exchange_code(code=code, redirect_uri=TL_REDIRECT_URI)

        # FIX 1: hand the declared form to the client BEFORE the fetch, so the
        # declared_* features are built from the applicant's real answers
        # instead of silently falling back to defaults. Older client builds do
        # not accept these kwargs, so degrade gracefully rather than 502.
        try:
            payload = client.fetch_user(case_id=pending["case_id"],
                                        as_of=date.today(),
                                        months=months,
                                        declared=declared)
        except TypeError:
            log.warning("client.fetch_user does not accept months/declared; "
                        "falling back to the legacy signature")
            payload = client.fetch_user(case_id=pending["case_id"],
                                        as_of=date.today())
    except Exception as e:
        return JSONResponse({"ok": False,
                             "error": f"Could not fetch account data: {e}"},
                            status_code=502)

    # Keep this for backwards compatibility: anything downstream that reads
    # payload["declared"] still works.
    payload["declared"] = declared

    # FIX 2: measure the window the provider actually returned, once, here,
    # while the payload is intact.
    hist = realised_history(payload)
    if hist:
        log.info("realised history %s .. %s (%s days, %s months)",
                 hist["history_start"], hist["history_end"],
                 hist["history_days"], hist["months_of_history"])
    else:
        log.warning("could not measure the realised history window from the "
                    "payload; months_of_history will fall back to the request")

    token = secrets.token_urlsafe(24)
    _CONNECTED[token] = {
        "payload": payload,
        "declared": declared,
        "months_requested": months,
        "history": hist,
        "credit_accounts": count_credit_accounts(payload),
        "created": time.time(),
    }
    _sweep_connections()
    return JSONResponse({"ok": True, "redirect": f"/ibex?tl={token}"})


def _sweep_connections() -> None:
    """Drop connections older than the TTL so the in-memory store cannot grow
    without bound during a long demo session."""
    now = time.time()
    for tok in [t for t, e in _CONNECTED.items()
                if now - e.get("created", now) > TL_TOKEN_TTL]:
        _CONNECTED.pop(tok, None)


class LiveScoreRequest(BaseModel):
    token: str


@app.post("/api/score-live")
def score_live(req: LiveScoreRequest) -> JSONResponse:
    # FIX 4: read without destroying. The old code popped the token, so the
    # first score consumed the connection and any second attempt -- a page
    # refresh, or re-scoring after logging back in -- returned 400.
    _sweep_connections()
    entry = _CONNECTED.get(req.token)
    if entry is None:
        raise HTTPException(400, "connection expired; please reconnect your bank")

    try:
        svc = get_service()
    except Exception as e:
        raise HTTPException(503, f"model not ready: {e}")

    try:
        result = svc.score_payload(entry["payload"])
    except Exception as e:
        raise HTTPException(500, f"scoring failed: {e}")

    result["profile"] = "truelayer"
    result["profile_label"] = "Live TrueLayer sandbox account"
    result["source"] = "truelayer"

    # FIX 2 (cont.): carry the measured window into the result. /api/score has
    # always done this for mock accounts; the live path never did, which is
    # why "Months of history" rendered as a dash on a connection that had, in
    # fact, returned a full 24 months.
    hist = entry.get("history") or {}
    if hist:
        result.update(hist)
    else:
        result["months_of_history"] = entry.get("months_requested")

    # FIX 3: report credit accounts separately. num_obligations counts active
    # payment commitments inferred from the transaction stream; credit lines
    # are cards and loans. Conflating them overstates a thin file.
    ca = entry.get("credit_accounts")
    if ca is not None:
        result["credit_accounts"] = ca
        result["credit_lines"] = ca

    result["declared"] = entry.get("declared") or {}
    result["token_reusable_until"] = int(entry.get("created", time.time())
                                         + TL_TOKEN_TTL)
    return JSONResponse(result)


app.include_router(score_event_router)

from serve.ibex_v3 import router as ibex_v3_router
app.include_router(ibex_v3_router)

from serve.ibex_v4 import router as ibex_v4_router
app.include_router(ibex_v4_router)

from serve.ibex_web import router as ibex_web_router
app.include_router(ibex_web_router)

from serve.intervals import build_router as _build_interval_router
app.include_router(_build_interval_router())


@app.get("/anchor")
def anchor_page():
    return FileResponse("serve/static/anchor.html")

# BUILD 19 STATIC -- expose serve/static as /static so the score card
# can load ibex_intervals.js. The dashboard HTML is inline otherwise.
try:
    import os as _os_static
    from fastapi.staticfiles import StaticFiles as _StaticFiles

    _STATIC_DIR = _os_static.path.join(
        _os_static.path.dirname(_os_static.path.abspath(__file__)),
        'static')
    if _os_static.path.isdir(_STATIC_DIR):
        app.mount('/static', _StaticFiles(directory=_STATIC_DIR),
                  name='static')
    else:
        print('static dir not found:', _STATIC_DIR)
except Exception as _static_err:
    print('static mount not added:', _static_err)

