"""ibex_v3.py -- unified Ibex dashboard: accounts, scoring, chain anchoring.

Mounts as an ADDITIVE FastAPI router. It does not modify any BUILD 18
endpoint; it calls the same underlying code paths, so scoring behaviour
is identical to /api/score-live.

Routes
------
  GET  /app                       the single-page dashboard
  POST /api/v3/auth/signup        create an account
  POST /api/v3/auth/login         sign in  (sets ibex_session cookie)
  POST /api/v3/auth/logout
  GET  /api/v3/auth/session       who am I
  GET  /api/v3/tl/connect-url     TrueLayer authorise URL for this user
  GET  /api/v3/score/current      the signed-in user's latest score
  POST /api/v3/score/run          score from a TrueLayer token
  GET  /api/v3/chain/status       chain config + last submission
  POST /api/v3/chain/submit       write score-event.json, submit on-chain
  GET  /api/v3/admin/cohort       50-person test panel (admin only)
  GET  /api/v3/admin/cohort/status

Environment
-----------
  IBEX_USER_DB          default serve/users.json
  IBEX_SCORE_EVENT_PATH where score-event.json is written
  IBEX_CHAIN_DIR        the hardhat repo, enables on-chain submission
  IBEX_CHAIN_NETWORK    polygon | amoy | none   (default none)
  IBEX_ADMIN_EMAIL      this address is granted the admin role on signup
  IBEX_OB_MATRIX        OB matrix pickle, for the admin cohort panel
  IBEX_ARTIFACTS        artifacts folder (default artifacts)
  IBEX_APP_SALT         salt for the pseudonymous on-chain handle
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import subprocess
import threading
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Cookie, HTTPException, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from serve.history import realised_history, count_credit_accounts  # BUILD 19 history fix

router = APIRouter()

_HERE = os.path.dirname(os.path.abspath(__file__))
_STATIC = os.path.join(_HERE, "static")

USER_DB = os.environ.get("IBEX_USER_DB", os.path.join(_HERE, "users.json"))
ARTIFACTS = os.environ.get("IBEX_ARTIFACTS", "artifacts")
CHAIN_DIR = os.environ.get("IBEX_CHAIN_DIR", "")
CHAIN_NETWORK = os.environ.get("IBEX_CHAIN_NETWORK", "none").strip().lower()
ADMIN_EMAIL = os.environ.get("IBEX_ADMIN_EMAIL", "").strip().lower()
OB_MATRIX = os.environ.get("IBEX_OB_MATRIX", "")

# Polygon PoS mainnet. Amoy is the current testnet (Mumbai is retired).
# NOTE: WhatsOnChain is a Bitcoin SV explorer and does NOT index Polygon.
EXPLORERS = {
    "polygon": ("Polygonscan", "https://polygonscan.com", 137),
    "amoy": ("Amoy Polygonscan", "https://amoy.polygonscan.com", 80002),
}

CONTRACT = os.environ.get(
    "IBEX_CONTRACT", "0x8621D09F08C2f58803e7239F8D46D444e0eF63e1")

# Scorecard constants, identical to obcredit/modeling/scorecard.py
PDO = 40.0
BASE_SCORE = 600.0
BASE_ODDS = 20.0
FACTOR = PDO / math.log(2.0)              # 57.7078
OFFSET = BASE_SCORE - FACTOR * math.log(BASE_ODDS)   # 427.1229


# --------------------------------------------------------------------
# user store -- JSON file, PBKDF2 password hashing, stdlib only
# --------------------------------------------------------------------
_db_lock = threading.Lock()
_sessions: Dict[str, str] = {}           # token -> email
_scores: Dict[str, Dict[str, Any]] = {}  # email -> latest score payload

PBKDF_ROUNDS = 240_000


def _load_db() -> Dict[str, Any]:
    if not os.path.exists(USER_DB):
        return {"users": {}}
    try:
        with open(USER_DB, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"users": {}}


def _save_db(db: Dict[str, Any]) -> None:
    d = os.path.dirname(os.path.abspath(USER_DB))
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = USER_DB + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(db, fh, indent=2)
    os.replace(tmp, USER_DB)


def _hash_pw(password: str, salt: Optional[str] = None) -> Dict[str, str]:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             bytes.fromhex(salt), PBKDF_ROUNDS)
    return {"salt": salt, "hash": dk.hex(), "rounds": str(PBKDF_ROUNDS)}


def _verify_pw(password: str, rec: Dict[str, Any]) -> bool:
    try:
        rounds = int(rec.get("rounds", PBKDF_ROUNDS))
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(rec["salt"]), rounds)
        return hmac.compare_digest(dk.hex(), rec["hash"])
    except Exception:
        return False


def _current_user(token: Optional[str]) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    email = _sessions.get(token)
    if not email:
        return None
    db = _load_db()
    u = db["users"].get(email)
    if not u:
        return None
    return {"email": email, "name": u.get("name", ""),
            "role": u.get("role", "user"),
            "created": u.get("created", "")}


def _require(token: Optional[str], admin: bool = False) -> Dict[str, Any]:
    u = _current_user(token)
    if not u:
        raise HTTPException(status_code=401, detail="not signed in")
    if admin and u["role"] != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return u


class SignupBody(BaseModel):
    email: str = Field(min_length=3)
    password: str = Field(min_length=8)
    name: str = ""


class LoginBody(BaseModel):
    email: str
    password: str


@router.post("/api/v3/auth/signup")
def signup(body: SignupBody, response: Response):
    email = body.email.strip().lower()
    if "@" not in email:
        raise HTTPException(400, "enter a valid email address")
    with _db_lock:
        db = _load_db()
        if email in db["users"]:
            raise HTTPException(400, "an account with that email exists")
        first = len(db["users"]) == 0
        role = "admin" if (first or email == ADMIN_EMAIL) else "user"
        db["users"][email] = {
            "name": body.name.strip() or email.split("@")[0],
            "role": role,
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **_hash_pw(body.password),
        }
        _save_db(db)
    tok = secrets.token_urlsafe(32)
    _sessions[tok] = email
    response.set_cookie("ibex_session", tok, httponly=True, samesite="lax",
                        max_age=60 * 60 * 12, path="/")
    return {"ok": True, "email": email, "role": role,
            "note": "first account is granted admin" if first else ""}


@router.post("/api/v3/auth/login")
def login(body: LoginBody, response: Response):
    email = body.email.strip().lower()
    db = _load_db()
    rec = db["users"].get(email)
    if not rec or not _verify_pw(body.password, rec):
        raise HTTPException(401, "email or password is incorrect")
    tok = secrets.token_urlsafe(32)
    _sessions[tok] = email
    response.set_cookie("ibex_session", tok, httponly=True, samesite="lax",
                        max_age=60 * 60 * 12, path="/")
    return {"ok": True, "email": email, "role": rec.get("role", "user")}


@router.post("/api/v3/auth/logout")
def logout(response: Response, ibex_session: Optional[str] = Cookie(None)):
    _sessions.pop(ibex_session or "", None)
    response.delete_cookie("ibex_session", path="/")
    return {"ok": True}


@router.get("/api/v3/auth/session")
def session(ibex_session: Optional[str] = Cookie(None)):
    u = _current_user(ibex_session)
    return {"signed_in": bool(u), "user": u}


# --------------------------------------------------------------------
# pseudonymous identity -- binds the on-chain handle to the bank-verified
# account holder name. No PII is written to disk or to the chain.
# --------------------------------------------------------------------
_TITLES = {"MR", "MRS", "MISS", "MS", "DR", "PROF", "SIR", "MX", "REV"}


def normalise_name(raw: str) -> str:
    """Strip accents, punctuation, titles and spacing so the same person
    hashes identically across connections and across banks."""
    s = unicodedata.normalize("NFKD", raw or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^A-Za-z ]", " ", s).upper()
    parts = [p for p in s.split() if p and p not in _TITLES]
    return " ".join(parts)


def chain_handle(email: str, bank_name: str = ""):
    """
    The on-chain identity: keccak256(utf8(userId + ":" + USER_SALT)) where
    userId is chain_user_id(email, bank_name). Matches hashScoreEvent.js.

    make_handle() below is the older sha256(salt|email|name) scheme. It does
    NOT correspond to anything the contract has ever stored, so it must not be
    used for anything the chain has to agree with. It is kept only so existing
    callers and stored history files keep resolving.
    """
    try:
        from serve.chain_hash import chain_user_hash, configured_salt
        salt = configured_salt(os.environ.get("IBEX_CHAIN_DIR", ""))
        return chain_user_hash(email, bank_name, salt) if salt else None
    except Exception:
        return None


def make_handle(email: str, bank_name: str) -> str:
    """Stable pseudonymous handle = sha256(salt | email | normalised name).

    Bound to a bank-verified identity, so the same human cannot farm
    multiple scores under different sign-up emails. Reveals nothing:
    what reaches score-event.json and the chain is a hash of a hash.
    """
    salt = os.environ.get("IBEX_APP_SALT", "ibex-dev-salt-change-me")
    basis = f"{salt}|{(email or '').strip().lower()}|{normalise_name(bank_name)}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _holder_name_for(token: str) -> str:
    """Best-effort read of the bank-verified holder name.

    Must run BEFORE score_live(), which pops the _CONNECTED entry.
    Silent on failure -- the handle degrades to the email and the
    'bank_name_verified' flag reports which happened.
    """
    try:
        from serve.app import _CONNECTED   # type: ignore
    except Exception:
        return ""
    entry = _CONNECTED.get(token) or {}
    if not isinstance(entry, dict):
        return ""
    for key in ("holder_name", "full_name", "account_holder", "name"):
        v = entry.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    payload = entry.get("payload")
    if isinstance(payload, dict):
        info = payload.get("info")
        if isinstance(info, dict):
            results = info.get("results")
            if isinstance(results, list) and results:
                first = results[0]
                if isinstance(first, dict):
                    v = first.get("full_name") or first.get("name")
                    if isinstance(v, str) and v.strip():
                        return v.strip()
        for key in ("holder_name", "full_name", "account_holder"):
            v = payload.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


# --------------------------------------------------------------------
# scoring -- delegates to the BUILD 18 pipeline, unchanged
# --------------------------------------------------------------------
class RunScoreBody(BaseModel):
    token: str
    months: int = 24


def _issues_from_factors(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    Turn the model's negative reason codes into plain-language issues.

    # advice-table-delegated -- the advice table now lives in serve/score_advice.py,
    keyed on the features the CURRENT build uses. The table that used to
    sit here was written for an older feature set: only 5 of its 15 keys
    still existed, so 11 of the 16 live features fell through to the
    generic branch and rendered raw column names at the user.

    Presentation only -- ranking still comes from SHAP contributions.
    """
    from serve.score_advice import build_issues
    return build_issues(payload)


def _band_of(score: float) -> str:
    for lo, name in ((720.0, "A"), (660.0, "B"), (600.0, "C"), (540.0, "D")):
        if score >= lo:
            return name
    return "E"


@router.post("/api/v3/score/run")
def score_run(body: RunScoreBody,
              ibex_session: Optional[str] = Cookie(None)):
    """
    Score the signed-in user from a TrueLayer access token.

    Reuses serve.app's live scoring path so the numbers are identical to
    POST /api/score-live. Nothing about the model or calibrator changes.
    """
    u = _require(ibex_session)

    # Read the bank-verified holder name BEFORE score_live pops the entry.
    holder = _holder_name_for(body.token)

    try:
        from serve.app import score_live          # type: ignore
        from serve.app import LiveScoreRequest    # type: ignore
    except Exception as exc:
        raise HTTPException(500, f"cannot reach scoring pipeline: {exc}")

    try:
        raw = score_live(LiveScoreRequest(token=body.token))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"scoring failed: {exc}")

    # score_live returns a starlette JSONResponse. Taking __dict__ of that
    # yields status_code/background/body/raw_headers, NOT the result --
    # the payload has to be decoded out of .body.
    if isinstance(raw, dict):
        payload = raw
    elif hasattr(raw, "body"):
        try:
            payload = json.loads(bytes(raw.body).decode("utf-8"))
        except Exception as exc:
            raise HTTPException(502, f"could not decode scoring response: {exc}")
    else:
        raise HTTPException(502, f"unrecognised scoring response: {type(raw)}")

    if not isinstance(payload, dict):
        raise HTTPException(502, "scoring response was not a JSON object")

    print("SCORE PAYLOAD KEYS:", sorted(payload.keys()))

    def _pick(*names):
        for n in names:
            v = payload.get(n)
            if v is not None:
                return v
        return None

    _pd = _pick("pd", "probability_of_default", "prob_default",
                "p_default", "probability", "default_probability")
    _sc = _pick("score", "credit_score", "scorecard_score", "points", "value")

    # If only a PD came back, derive the score with the same PDO transform
    # the scorecard uses: score = 427.1229 + 57.7078 * ln((1-PD)/PD)
    if _sc is None and _pd is not None:
        _p = min(max(float(_pd), 1e-9), 1.0 - 1e-9)
        _sc = OFFSET + FACTOR * math.log((1.0 - _p) / _p)

    if _sc is None:
        raise HTTPException(
            502, "scoring returned no score field; keys were: "
                 + ", ".join(sorted(payload.keys())))

    score = float(_sc)

    prev = (_scores.get(u["email"]) or {}).get("score")
    rec = {
        "user_id": u["email"],
        "name": u["name"],
        "score": round(score),
        "band": _pick("band", "score_band", "grade", "risk_band")
                or _band_of(score),
        "pd": float(_pd) if _pd is not None else None,
        "previous_score": prev,
        "detected_income": _pick("detected_income", "monthly_income",
                                 "income", "income_monthly"),
        "credit_lines": _pick("credit_lines", "num_credit_lines",
                              "credit_accounts", "n_credit_accounts"),
        "n_features": _pick("n_features", "num_features",
                            "feature_count", "features_used"),
        "positive_factors": _pick("positive_factors", "top_positive",
                                  "positives", "reasons_positive") or [],
        "negative_factors": _pick("negative_factors", "top_negative",
                                  "negatives", "reasons_negative",
                                  "reason_codes") or [],
        "history_months": _pick("history_months", "months",
                                "months_of_history", "history"),
        "bank_name": holder or "",
        "bank_name_verified": bool(holder),
        "chain_handle": chain_handle(u["email"], holder or ""),
        "scored_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "chain": None,
    }
    rec["issues"] = _issues_from_factors(rec)
    _scores[u["email"]] = rec
    return rec


@router.get("/api/v3/score/current")
def score_current(ibex_session: Optional[str] = Cookie(None)):
    u = _require(ibex_session)
    return {"score": _scores.get(u["email"])}


@router.get("/api/v3/tl/connect-url")
def tl_connect_url(ibex_session: Optional[str] = Cookie(None)):
    """
    Hand back the existing /connect URL. The bank-connection flow is
    BUILD 18's, untouched -- Mock Bank, john / doe.
    """
    _require(ibex_session)
    return {"url": "/connect?months=24", "provider_hint": "uk-cs-mock",
            "username": "john", "password": "doe"}


# --------------------------------------------------------------------
# chain anchoring -- writes score-event.json, then calls hardhat
# --------------------------------------------------------------------
def _explorer():
    return EXPLORERS.get(CHAIN_NETWORK)


def _write_event(rec: Dict[str, Any]) -> str:
    """
    Write score-event.json using the BUILD 18 builder so the JSON shape
    stays byte-compatible with the teammate's hardhat scripts.

    The identity written out is the pseudonymous handle when the bank
    holder name was available, otherwise the sign-up email.
    """
    out = os.environ.get("IBEX_SCORE_EVENT_PATH", "")
    if not out and CHAIN_DIR:
        out = os.path.join(CHAIN_DIR, "score-event.json")
    if not out:
        # Direct-RPC deployments have no chain folder on disk; keep the
        # event beside the anchor records.
        out = os.path.join(_HERE, "_anchors", "latest-score-event.json")
        os.makedirs(os.path.dirname(out), exist_ok=True)

    try:
        from serve.chain_hash import chain_user_id
        from serve.score_event import build_score_event, write_score_event

        # NOTE: the parameter is new_score, not score.
        ev = build_score_event(
            # PLAIN TEXT userId, not a hash. hashScoreEvent.js applies
            # keccak256(userId + ":" + salt) itself, so passing the already
            # hashed chain_handle here would hash it twice and produce a
            # userHash that no lookup could ever reproduce.
            user_id=chain_user_id(rec["user_id"], rec.get("bank_name") or ""),
            new_score=float(rec["score"]),
            pd=rec.get("pd"),
            previous_score=rec.get("previous_score"),
            positive_factors=rec.get("positive_factors") or [],
            negative_factors=rec.get("negative_factors") or [],
        )
        write_score_event(ev, out)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"could not build score event: {exc}")
    return out


def _run_hardhat(script: str, timeout: int = 240) -> Dict[str, Any]:
    if not CHAIN_DIR or not os.path.isdir(CHAIN_DIR):
        raise HTTPException(400, "IBEX_CHAIN_DIR is not set to the "
                                 "hardhat repo folder")
    cmd = ["npm", "run", script]
    try:
        p = subprocess.run(cmd, cwd=CHAIN_DIR, capture_output=True,
                           text=True, timeout=timeout, shell=os.name == "nt")
    except subprocess.TimeoutExpired:
        raise HTTPException(504, f"{script} timed out after {timeout}s")
    except FileNotFoundError:
        raise HTTPException(500, "npm not found on PATH for the server "
                                 "process")
    return {"ok": p.returncode == 0, "code": p.returncode,
            "stdout": p.stdout[-6000:], "stderr": p.stderr[-3000:]}


def _scrape(out: str) -> Dict[str, Optional[str]]:
    """Pull hashes and a tx hash out of the hardhat script's log."""
    found: Dict[str, Optional[str]] = {
        "userHash": None, "scoreEventHash": None,
        "modelVersionHash": None, "merkleRoot": None, "txHash": None,
    }
    for line in (out or "").splitlines():
        low = line.lower()
        tokens = [t.strip().strip(",").strip('"')
                  for t in line.replace(":", " ").split()]
        hexes = [t for t in tokens
                 if t.startswith("0x") and len(t) == 66]
        if not hexes:
            continue
        h = hexes[-1]
        flat = low.replace(" ", "")
        if "userhash" in flat:
            found["userHash"] = h
        elif "scoreeventhash" in flat:
            found["scoreEventHash"] = h
        elif "modelversionhash" in flat:
            found["modelVersionHash"] = h
        elif "merkle" in low:
            found["merkleRoot"] = h
        elif "tx" in low or "transaction" in low:
            found["txHash"] = h
    return found


@router.get("/api/v3/chain/status")
def chain_status(ibex_session: Optional[str] = Cookie(None)):
    u = _require(ibex_session)
    ex = _explorer()
    return {
        "network": CHAIN_NETWORK,
        "enabled": CHAIN_NETWORK in EXPLORERS,
        "chain_id": ex[2] if ex else None,
        "explorer_name": ex[0] if ex else None,
        "explorer_base": ex[1] if ex else None,
        "contract": CONTRACT,
        "contract_url": f"{ex[1]}/address/{CONTRACT}" if ex else None,
        "chain_dir_set": bool(CHAIN_DIR and os.path.isdir(CHAIN_DIR)),
        "event_path": os.environ.get("IBEX_SCORE_EVENT_PATH", ""),
        "last": (_scores.get(u["email"]) or {}).get("chain"),
    }


class SubmitBody(BaseModel):
    dry_run: bool = False


@router.post("/api/v3/chain/submit")
def chain_submit(body: SubmitBody,
                 ibex_session: Optional[str] = Cookie(None)):
    """
    Anchor the signed-in user's latest score.

    dry_run=True runs `npm run demo` against the local hardhat node and
    costs nothing. dry_run=False runs `npm run submit:<network>` and, on
    Polygon mainnet, spends real POL and cannot be undone.
    """
    u = _require(ibex_session)
    rec = _scores.get(u["email"])
    if not rec:
        # v4 scoring writes its record into ibex_v4._LAST. Builds of ibex_v4
        # without the mirror line leave _scores empty, so the anchor step
        # claimed "no score yet" while a score was plainly on screen. Read the
        # v4 store directly rather than trusting the mirror. Imported lazily:
        # ibex_v4 imports this module at load time, so a module level import
        # here would be circular.
        try:
            from serve import ibex_v4 as _v4
            rec = _v4._LAST.get(u["email"])
            if rec:
                _scores[u["email"]] = rec
        except Exception as exc:
            print("could not read the v4 score store:", exc)
    if not rec:
        raise HTTPException(400, "no score yet -- connect a bank first")

    from serve import chain_rpc
    use_rpc = chain_rpc.write_available()
    if not CHAIN_DIR and not use_rpc:
        raise HTTPException(
            500, "no chain backend configured. Set IBEX_CHAIN_DIR to the "
                 "hardhat project folder (local), or PRIVATE_KEY + "
                 "POLYGON_RPC_URL + IBEX_CHAIN_NETWORK=polygon for direct "
                 "anchoring (hosted).")

    path = _write_event(rec)
    print("score event written to:", path)

    # The hardhat scripts resolve SCORE_EVENT_FILE against their own folder
    # (cwd = CHAIN_DIR). If IBEX_SCORE_EVENT_PATH points anywhere else --
    # for example the old V1 chain folder -- the script cannot see the
    # event and fails with ENOENT. Mirror the event into the chain folder
    # so the write location and the read location can never drift apart.
    if CHAIN_DIR:
        mirror = os.path.join(CHAIN_DIR, "score-event.json")
        try:
            if os.path.abspath(path) != os.path.abspath(mirror):
                import shutil
                shutil.copyfile(path, mirror)
                print("score event mirrored to:", mirror)
        except Exception as exc:
            print("could not mirror the score event into the chain folder:",
                  exc)

    hashes: Dict[str, Optional[str]] = {}
    if body.dry_run:
        res = _run_hardhat("demo")
        net = "local"
    elif use_rpc:
        # Direct JSON-RPC: no node runtime. The hashes are computed
        # in-process by chain_hash, byte-identical to the chain repo's
        # utils (proven against the live contract).
        try:
            with open(path, encoding="utf-8") as fh:
                ev = json.load(fh)
            from serve.chain_hash import configured_salt
            salt = configured_salt(CHAIN_DIR)
            if len(salt) < 16:
                raise HTTPException(
                    500, "USER_SALT is not configured (16 chars minimum). "
                         "It must be byte-identical to the value used by "
                         "every previous anchor.")
            from serve.chain_hash import (
                user_hash as _uh, score_event_hash as _seh,
                model_version_hash as _mvh)
            period = chain_rpc.score_period_of(ev)
            uh = _uh(str(ev.get("userId", "")), salt)
            seh = _seh(ev)
            mvh = _mvh(str(ev.get("modelVersion", "")))
            # Preflight: one anchor per identity every 28 days and a
            # strictly newer YYYYMM period. Read first and explain rather
            # than failing at estimateGas with an opaque custom error.
            erec = chain_rpc.read_record(uh)["rec"]
            if erec.get("timestampEpoch") and erec["timestampEpoch"] != "0":
                anchored_at = int(erec["timestampEpoch"])
                next_at = chain_rpc.next_submission_at(uh)
                now_ts = int(time.time())
                when = datetime.fromtimestamp(
                    anchored_at, tz=timezone.utc).strftime(
                        "%d %b %Y %H:%M UTC")
                if next_at and now_ts < next_at:
                    when_next = datetime.fromtimestamp(
                        next_at, tz=timezone.utc).strftime(
                            "%d %b %Y %H:%M UTC")
                    raise HTTPException(
                        429, "this identity is already anchored (" + when +
                             "). The contract allows one update per identity "
                             "every 28 days -- next allowed " + when_next +
                             ". The existing record is still fully "
                             "verifiable on the business page. To test a "
                             "fresh anchor, sign up a second test user: a "
                             "different email is a different identity.")
                old_period = int(erec.get("scorePeriod") or 0)
                if old_period and old_period >= period:
                    raise HTTPException(
                        429, "this identity is already anchored for a score "
                             "period at least as new as this one, and the "
                             "contract requires a strictly newer month. The "
                             "existing record is still fully verifiable on "
                             "the business page.")
        except HTTPException:
            raise
        except chain_rpc.ChainRevert as exc:
            raise HTTPException(exc.status, str(exc))
        except Exception as exc:
            raise HTTPException(
                502, f"could not preflight the anchor: {exc}")

        try:
            tx_hash, rpc_log = chain_rpc.submit_score_root(
                uh, seh, seh, mvh, period)
        except chain_rpc.ChainRevert as exc:
            raise HTTPException(exc.status, str(exc))
        except Exception as exc:
            raise HTTPException(502, f"direct RPC anchoring failed: {exc}")

        hashes = {"userHash": uh, "scoreEventHash": seh,
                  "modelVersionHash": mvh, "merkleRoot": seh,
                  "txHash": tx_hash}
        res = {"ok": True, "code": 0, "stdout": rpc_log, "stderr": ""}
        net = CHAIN_NETWORK if CHAIN_NETWORK in EXPLORERS else "polygon"
    elif CHAIN_NETWORK not in EXPLORERS:
        res = _run_hardhat("demo")
        net = "local"
    else:
        # Preflight: the V2 contract allows one anchor per identity every 28
        # days and requires a strictly newer score period (YYYYMM). A rejected
        # submission reverts with an opaque custom error, so read the existing
        # record first and explain instead of failing at estimateGas.
        pre = _run_hardhat(f"read:{CHAIN_NETWORK}", timeout=120)
        if pre["ok"]:
            pout = pre["stdout"]
            mts = re.search(r"timestamp:\s*(\d+)", pout)
            mnxt = re.search(r"next contract-allowed update:\s*(\d+)", pout)
            mper = re.search(r"scorePeriod:\s*(\d{4})-(\d{2})", pout)
            if mts and int(mts.group(1)) > 0:
                anchored_at = int(mts.group(1))
                next_at = int(mnxt.group(1)) if mnxt else 0
                now_ts = int(time.time())
                when = datetime.fromtimestamp(
                    anchored_at, tz=timezone.utc).strftime("%d %b %Y %H:%M UTC")
                if next_at and now_ts < next_at:
                    when_next = datetime.fromtimestamp(
                        next_at, tz=timezone.utc).strftime("%d %b %Y %H:%M UTC")
                    raise HTTPException(
                        429, "this identity is already anchored (" + when +
                             "). The contract allows one update per identity "
                             "every 28 days -- next allowed " + when_next +
                             ". The existing record is still fully "
                             "verifiable on the business page. To test a "
                             "fresh anchor, sign up a second test user: a "
                             "different email is a different identity.")
                if mper:
                    anchored_period = (int(mper.group(1)), int(mper.group(2)))
                    cur = datetime.now(timezone.utc)
                    if anchored_period >= (cur.year, cur.month):
                        raise HTTPException(
                            429, "this identity is already anchored for "
                                 + mper.group(1) + "-" + mper.group(2) +
                                 " and the contract requires a strictly newer "
                                 "month for the next update. The existing "
                                 "record is still fully verifiable on the "
                                 "business page.")
        res = _run_hardhat(f"submit:{CHAIN_NETWORK}", timeout=420)
        net = CHAIN_NETWORK

    if not hashes:
        hashes = _scrape(res.get("stdout", ""))
    ex = _explorer()
    links: Dict[str, str] = {}
    if ex and net != "local":
        if hashes.get("txHash"):
            links["transaction"] = f"{ex[1]}/tx/{hashes['txHash']}"
        links["contract"] = f"{ex[1]}/address/{CONTRACT}"

    info = {
        "network": net,
        "event_path": path,
        "submitted_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "ok": res["ok"],
        "hashes": hashes,
        "links": links,
        "explorer_name": ex[0] if ex else None,
        "log": res["stdout"][-2500:],
        "error": (res["stderr"][-1200:] if not res["ok"] else ""),
    }
    rec["chain"] = info
    _scores[u["email"]] = rec

    # Keep a copy of exactly what was anchored, keyed by transaction hash.
    # ScoreAuditRegistry stores hashes only, never the score, so without this
    # a business holding a name, an email and a claimed score has nothing on
    # chain to compare the score against. This file also outlives a server
    # restart, which the in-memory score store does not.
    if res["ok"] and net != "local" and hashes.get("txHash"):
        try:
            from serve import anchor_store
            with open(path, encoding="utf-8") as fh:
                anchored_event = json.load(fh)
            info["anchor_saved"] = anchor_store.save(
                tx_hash=hashes["txHash"],
                user_hash=hashes.get("userHash") or "",
                event=anchored_event,
                score_event_hash=hashes.get("scoreEventHash") or "",
                merkle_root=hashes.get("merkleRoot") or "",
                model_version_hash=hashes.get("modelVersionHash") or "",
                network=net,
                contract=CONTRACT,
                score=(float(rec["score"])
                       if rec.get("score") is not None else None),
                band=str(rec.get("band") or ""),
            )
        except Exception as exc:
            # Anchoring already succeeded, so never fail the request here.
            print("could not persist the anchor record:", exc)
            info["anchor_saved"] = ""

    # Surface hardhat failures as errors instead of a misleading HTTP 200.
    if not res["ok"]:
        # Pick the line that actually explains the failure. The first line of
        # hardhat's stderr is often a banner or a warning, not the reason.
        needles = ("error", "revert", "not approved", "insufficient",
                   "cooldown", "too soon", "rate limit", "cannot find",
                   "not recognized", "err!", "exception", "failed")
        err_lines = [l.strip() for l in (res["stderr"] or "").splitlines()
                     if l.strip()]
        out_lines = [l.strip() for l in (res["stdout"] or "").splitlines()
                     if l.strip()]
        picked = [l for l in err_lines
                  if any(n in l.lower() for n in needles)]
        reason = (picked[-1] if picked
                  else (err_lines[-1] if err_lines
                        else (out_lines[-1] if out_lines
                              else "no output -- is the chain folder right?")))
        print("chain submit failed.\nstderr:\n" + (res["stderr"] or "")
              + "\nstdout:\n" + (res["stdout"] or ""))
        raise HTTPException(
            502, "on-chain submission failed: " + reason[:300])
    return info


# --------------------------------------------------------------------
# admin cohort panel -- N real applicants from the eval slice
# --------------------------------------------------------------------
_cohort_state: Dict[str, Any] = {
    "status": "idle",    # idle | loading | ready | error
    "message": "",
    "rows": [],
    "started": None,
    "finished": None,
}
_cohort_lock = threading.Lock()


def _num(v, default=None):
    try:
        f = float(v)
        return default if f != f else f
    except Exception:
        return default


def characterise(row: Dict[str, Any]) -> str:
    """
    Plain-language description of an applicant's file. Mirrors the
    wording used by cohort_report.py so the two stay comparable.
    """
    bits: List[str] = []
    n_ob = _num(row.get("num_active_obligations"), 0.0) or 0.0
    if n_ob <= 0:
        bits.append("no-file")
    elif n_ob <= 1:
        bits.append("ultra-thin")
    elif n_ob <= 2:
        bits.append("thin")
    elif n_ob <= 5:
        bits.append("medium")
    else:
        bits.append("thick")

    dpd = _num(row.get("max_dpd_24m"), 0.0) or 0.0
    ser = _num(row.get("num_serious_arrears_24m"), 0.0) or 0.0
    if ser >= 1:
        bits.append("serious arrears")
    elif dpd >= 8:
        bits.append("late payments")
    elif dpd > 0:
        bits.append("minor lateness")
    else:
        bits.append("clean conduct")

    dti = _num(row.get("debt_to_income"))
    if dti is not None:
        if dti > 0.6:
            bits.append("stretched")
        elif dti > 0.3:
            bits.append("moderate leverage")
        else:
            bits.append("comfortable")

    cv = _num(row.get("cv_payment_amount"))
    if cv is not None and cv > 0.5:
        bits.append("volatile income")
    return ", ".join(bits)


def _build_cohort(n: int, seed: int) -> None:
    """Runs on a worker thread. Loads, samples, scores, then frees."""
    try:
        import numpy as np
        import pandas as pd
        import lightgbm as lgb

        if not OB_MATRIX or not os.path.exists(OB_MATRIX):
            raise RuntimeError(
                "IBEX_OB_MATRIX is not set to an existing OB matrix pickle")

        card_path = os.path.join(ARTIFACTS, "scorecard.json")
        model_path = os.path.join(ARTIFACTS, "model_lgbm.txt")
        for p in (card_path, model_path):
            if not os.path.exists(p):
                raise RuntimeError(f"missing {p}")
        with open(card_path, "r", encoding="utf-8") as fh:
            card = json.load(fh)

        _cohort_state["message"] = "loading OB matrix (this takes a minute)"
        m = pd.read_pickle(OB_MATRIX)
        sub = m.dropna(subset=["target", "__week__"])
        sub = sub.sort_values("__week__", kind="mergesort")
        ev = sub.iloc[int(len(sub) * 0.8):]
        take = min(n, len(ev))
        sample = ev.sample(take, random_state=seed).copy()
        del m, sub, ev

        _cohort_state["message"] = f"scoring {take} applicants"
        feats = list(card["features"])
        medians = card.get("medians", {}) or {}
        best = int(card.get("best_iteration") or 0) or None

        X = sample.reindex(columns=feats)
        for c in feats:
            if c in medians and medians[c] is not None:
                X[c] = X[c].fillna(medians[c])
        X = X.fillna(0.0).to_numpy(np.float32)

        booster = lgb.Booster(model_file=model_path)
        raw = booster.predict(X, num_iteration=best)

        import pickle
        with open(os.path.join(ARTIFACTS, "calibrator.pkl"), "rb") as fh:
            cal = pickle.load(fh)
        cx = np.asarray(cal["x"], dtype=float)
        cy = np.asarray(cal["y"], dtype=float)
        floor = float(cal.get("pd_floor") or 0.0003)
        pds = np.clip(np.interp(raw, cx, cy, left=cy[0], right=cy[-1]),
                      floor, 1.0 - floor)
        scores = OFFSET + FACTOR * np.log((1.0 - pds) / pds)

        rows = []
        recs = sample.to_dict(orient="records")
        for i, r in enumerate(recs):
            s = float(scores[i])
            rows.append({
                "n": i + 1,
                "score": round(s),
                "band": _band_of(s),
                "pd": round(float(pds[i]), 5),
                "defaulted": int(_num(r.get("target"), 0) or 0),
                "profile": characterise(r),
                "obligations": _num(r.get("num_active_obligations"), 0),
                "max_dpd": _num(r.get("max_dpd_24m"), 0),
                "dti": (None if _num(r.get("debt_to_income")) is None
                        else round(_num(r.get("debt_to_income")), 3)),
                "income": (None if _num(r.get("monthly_income")) is None
                           else round(_num(r.get("monthly_income")), 2)),
            })
        rows.sort(key=lambda d: d["score"], reverse=True)
        for i, r in enumerate(rows):
            r["n"] = i + 1

        _cohort_state["rows"] = rows
        _cohort_state["status"] = "ready"
        _cohort_state["message"] = f"{len(rows)} applicants scored"
    except Exception as exc:
        _cohort_state["status"] = "error"
        _cohort_state["message"] = str(exc)
    finally:
        _cohort_state["finished"] = time.time()


@router.get("/api/v3/admin/cohort")
def admin_cohort(n: int = 50, seed: int = 42, refresh: bool = False,
                 ibex_session: Optional[str] = Cookie(None)):
    _require(ibex_session, admin=True)
    with _cohort_lock:
        if _cohort_state["status"] == "loading":
            return {"status": "loading", "message": _cohort_state["message"]}
        if _cohort_state["status"] == "ready" and not refresh:
            return {"status": "ready", "rows": _cohort_state["rows"],
                    "message": _cohort_state["message"]}
        _cohort_state.update({"status": "loading", "rows": [],
                              "message": "starting",
                              "started": time.time()})
    t = threading.Thread(target=_build_cohort,
                         args=(max(1, min(int(n), 500)), int(seed)),
                         daemon=True)
    t.start()
    return {"status": "loading", "message": "started"}


@router.get("/api/v3/admin/cohort/status")
def admin_cohort_status(ibex_session: Optional[str] = Cookie(None)):
    _require(ibex_session, admin=True)
    return {"status": _cohort_state["status"],
            "message": _cohort_state["message"],
            "rows": _cohort_state["rows"]
            if _cohort_state["status"] == "ready" else []}


@router.get("/app", response_class=HTMLResponse)
def app_page():
    path = os.path.join(_STATIC, "app_v3.html")
    if not os.path.exists(path):
        return HTMLResponse("<h1>app_v3.html missing from serve/static</h1>",
                            status_code=500)
    with open(path, "r", encoding="utf-8") as fh:
        return HTMLResponse(fh.read())
