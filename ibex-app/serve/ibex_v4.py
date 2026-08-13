"""
Ibex v4 -- additive layer on top of ibex_v3.

Nothing in ibex_v3.py is modified. This module adds:

  GET  /ibex                      welcome page + role routing
  POST /api/v4/score/run          scoring with DEEP payload extraction
  GET  /api/v4/score/current
  GET  /api/v4/evidence           standard errors / uncertainty panel
  POST /api/v4/verify/event       business: paste score-event.json
  POST /api/v4/verify/hash        business: paste a userHash
  GET  /api/v4/admin/cohort       admin: cohort via the REAL calibrator
  GET  /api/v4/admin/cohort/status

Two bugs fixed here relative to v3:

  1. Blank income / credit lines / months / factors. v3 guessed at payload
     key names. v4 walks the payload recursively and matches on patterns,
     so it finds the fields whatever they are called.

  2. The 709 pile-up in the admin cohort. v3 reimplemented calibration as
     plain np.interp + clip, which is NOT what production does -- it threw
     away the hybrid tail and clipped everything at the floor. v4 loads the
     real calibrator object and calls it, so admin and production agree by
     construction.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

router = APIRouter()

_HERE = os.path.dirname(os.path.abspath(__file__))
_STATIC = os.path.join(_HERE, "static")

ARTIFACTS = os.environ.get("IBEX_ARTIFACTS", "artifacts")
OB_MATRIX = os.environ.get("IBEX_OB_MATRIX", "")
CHAIN_DIR = os.environ.get("IBEX_CHAIN_DIR", "")
CHAIN_NETWORK = os.environ.get("IBEX_CHAIN_NETWORK", "polygon")


def _user_salt() -> str:
    """
    USER_SALT must be byte-identical to the one in the chain repo's .env.
    If it differs we still produce a well-formed 32 byte hash -- it just
    corresponds to nobody, and every lookup returns a clean-looking
    "not found". Prefer the environment, else read it out of the chain
    repo directly so the two cannot drift apart.
    """
    from serve.chain_hash import configured_salt
    return configured_salt(CHAIN_DIR)
EVENT_PATH = os.environ.get("IBEX_SCORE_EVENT_PATH", "")
CONTRACT = os.environ.get(
    "IBEX_CONTRACT", "0x8621D09F08C2f58803e7239F8D46D444e0eF63e1")

PDO = 40.0
BASE_SCORE = 600.0
BASE_ODDS = 20.0
import math
FACTOR = PDO / math.log(2.0)
OFFSET = BASE_SCORE - FACTOR * math.log(BASE_ODDS)

EXPLORERS = {
    "polygon": ("Polygonscan", "https://polygonscan.com", 137),
    "amoy": ("Amoy Polygonscan", "https://amoy.polygonscan.com", 80002),
}

# ---- auth helpers borrowed from v3 (never redefined, so no drift) ----
from serve import ibex_v3 as v3
from serve.history import realised_history, count_credit_accounts  # BUILD 19 history fix

_require = v3._require
_current_user = v3._current_user
_band_of = v3._band_of
_issues_from_factors = v3._issues_from_factors


# ======================================================================
# 1. DEEP PAYLOAD EXTRACTION -- the fix for the "--" fields
# ======================================================================

def _walk(obj: Any, path: str = ""):
    """Yield (dotted_path, key, value) for every key in a nested payload."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            yield p, str(k), v
            yield from _walk(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            p = f"{path}[{i}]"
            yield from _walk(v, p)


def _find_scalar(payload: Dict[str, Any], patterns: List[str],
                 exclude: Optional[List[str]] = None) -> Tuple[Any, str]:
    """
    First numeric/string value whose key matches any pattern.
    Shallower matches win, so a top-level 'score' beats 'debug.raw.score'.
    """
    exclude = exclude or []
    hits = []
    for path, key, val in _walk(payload):
        if not isinstance(val, (int, float, str)) or isinstance(val, bool):
            continue
        kl = key.lower()
        if any(re.search(x, kl) for x in exclude):
            continue
        for rank, pat in enumerate(patterns):
            if re.search(pat, kl):
                hits.append((rank, path.count("."), path, val))
                break
    if not hits:
        return None, ""
    hits.sort(key=lambda t: (t[0], t[1]))
    return hits[0][3], hits[0][2]


def _find_list(payload: Dict[str, Any], patterns: List[str]) -> Tuple[List, str]:
    """First list-of-strings / list-of-dicts whose key matches."""
    hits = []
    for path, key, val in _walk(payload):
        if not isinstance(val, list) or not val:
            continue
        kl = key.lower()
        for rank, pat in enumerate(patterns):
            if re.search(pat, kl):
                hits.append((rank, path.count("."), path, val))
                break
    if not hits:
        return [], ""
    hits.sort(key=lambda t: (t[0], t[1]))
    return hits[0][3], hits[0][2]


def _as_float(v) -> Optional[float]:
    try:
        if v is None:
            return None
        f = float(v)
        return None if f != f else f
    except Exception:
        return None


def _factor_labels(raw: List[Any]) -> List[str]:
    """Reason codes arrive as strings, or as dicts with a name + value."""
    out: List[str] = []
    for item in raw:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            for k in ("feature", "name", "label", "reason", "code", "factor"):
                if isinstance(item.get(k), str):
                    out.append(item[k])
                    break
        elif isinstance(item, (list, tuple)) and item:
            if isinstance(item[0], str):
                out.append(item[0])
    return out


def extract(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Pull every display field out of an unknown payload shape."""
    found: Dict[str, str] = {}

    score, p = _find_scalar(payload, [r"^score$", r"credit_?score", r"score"],
                            exclude=[r"z_?score", r"raw", r"hash"])
    found["score"] = p
    pd_v, p = _find_scalar(payload, [r"^pd$", r"^prob", r"probability",
                                     r"default_?rate", r"\bpd\b"],
                           exclude=[r"floor", r"min", r"max"])
    found["pd"] = p
    income, p = _find_scalar(
        payload, [r"monthly_?income", r"detected_?income", r"^income$",
                  r"income"], exclude=[r"declared", r"gap", r"type", r"code"])
    found["income"] = p
    lines, p = _find_scalar(
        payload, [r"credit_?lines", r"credit_?accounts", r"n_?credit",
                  r"num_active_obligations", r"obligations"])
    found["credit_lines"] = p
    months, p = _find_scalar(
        payload, [r"months_?of_?history", r"history_?months", r"^months$",
                  r"months"], exclude=[r"request"])
    found["months"] = p
    nfeat, p = _find_scalar(payload, [r"n_?features", r"num_?features",
                                      r"feature_?count"])
    found["n_features"] = p

    neg_raw, p = _find_list(payload, [r"negative_?factors", r"negative",
                                      r"adverse", r"reason_?codes?",
                                      r"top_?reasons?", r"detract"])
    found["negative"] = p
    pos_raw, p = _find_list(payload, [r"positive_?factors", r"positive",
                                      r"favour", r"favor", r"strength"])
    found["positive"] = p

    sc = _as_float(score)
    pdv = _as_float(pd_v)
    if sc is None and pdv is not None and 0.0 < pdv < 1.0:
        sc = OFFSET + FACTOR * math.log((1.0 - pdv) / pdv)
    if pdv is None and sc is not None:
        pdv = 1.0 / (1.0 + math.exp((sc - OFFSET) / FACTOR))

    return {
        "score": sc,
        "pd": pdv,
        "income": _as_float(income),
        "credit_lines": _as_float(lines),
        "months": _as_float(months),
        "n_features": _as_float(nfeat),
        "negative_factors": _factor_labels(neg_raw),
        "positive_factors": _factor_labels(pos_raw),
        "_found_at": found,
    }


# ======================================================================
# 2. SCORING
# ======================================================================

_LAST: Dict[str, Dict[str, Any]] = {}


class RunBody(BaseModel):
    token: str
    months: int = 24


@router.post("/api/v4/score/run")
def score_run(body: RunBody, ibex_session: Optional[str] = Cookie(None)):
    user = _require(ibex_session)

    from serve.app import score_live, LiveScoreRequest

    holder = ""
    try:
        holder = v3._holder_name_for(body.token) or ""
    except Exception:
        holder = ""

    raw = score_live(LiveScoreRequest(token=body.token))

    # score_live returns a starlette JSONResponse, not a dict.
    if isinstance(raw, dict):
        payload = raw
    elif hasattr(raw, "body"):
        payload = json.loads(bytes(raw.body).decode("utf-8"))
    else:
        payload = getattr(raw, "__dict__", {}) or {}

    print("SCORE PAYLOAD KEYS:", sorted(payload.keys()))

    f = extract(payload)
    print("V4 RESOLVED FROM:", json.dumps(f["_found_at"]))

    if f["score"] is None:
        raise HTTPException(
            502, "no score field found in payload; keys were "
                 + ", ".join(sorted(payload.keys())))

    prev = (_LAST.get(user["email"]) or {}).get("score")
    handle = None
    verified = bool(holder)
    try:
        # Must equal the on-chain userHash: keccak256(email + ":" + USER_SALT),
        # derived from the email alone exactly as the chain repo does it, so
        # the handle shown to the user is the one a business can look up.
        # The bank name is NOT part of it -- it only drives `verified`.
        from serve.chain_hash import chain_user_hash, configured_salt
        _salt = configured_salt(CHAIN_DIR)
        handle = (chain_user_hash(user["email"], holder or "", _salt)
                  if _salt else None)
    except Exception:
        handle = None

    rec: Dict[str, Any] = {
        "user_id": user["email"],
        "name": user.get("name") or "",
        "bank_name": holder,
        "bank_name_verified": verified,
        "chain_handle": handle,
        "score": round(float(f["score"]), 1),
        "band": _band_of(float(f["score"])),
        "pd": f["pd"],
        "previous_score": prev,
        "income": f["income"],
        "credit_lines": f["credit_lines"],
        "months": f["months"],
        "n_features": f["n_features"],
        "positive_factors": f["positive_factors"],
        "negative_factors": f["negative_factors"],
        "resolved_from": f["_found_at"],
        "payload_keys": sorted(payload.keys()),
        "scored_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    rec["issues"] = _issues_from_factors(rec)
    try:
        from serve.score_advice import build_positives
        rec["positives"] = build_positives(rec)
    except Exception as exc:
        print("positive advice failed:", exc)
        rec["positives"] = []
    _LAST[user["email"]] = rec
    # autoAnchor() on the live page posts to /api/v3/chain/submit, which reads
    # v3._scores -- a different dict from this module's _LAST. Without this the
    # anchor step fails with "no score yet -- connect a bank first" even though
    # a score is on screen. Both stores must see the same record.
    try:
        v3._scores[user["email"]] = rec
    except Exception as exc:
        print("could not mirror score into v3 store:", exc)
    try:
        import serve.score_history as _hist
        _hist.record_score(
            key=user["email"], score=rec["score"], band=rec["band"],
            pd=rec.get("pd"), handle=rec.get("chain_handle"),
            extra={"bank_name_verified": rec.get("bank_name_verified")})
    except Exception as exc:
        print("score history write failed:", exc)
    return rec


@router.get("/api/v4/score/current")
def score_current(ibex_session: Optional[str] = Cookie(None)):
    user = _require(ibex_session)
    return {"score": _LAST.get(user["email"])}


@router.get("/api/v4/tl/connect-url")
def tl_connect_url(ibex_session: Optional[str] = Cookie(None)):
    _require(ibex_session)
    return {"url": "/connect?months=24", "months": 24}


# ======================================================================
# 3. EVIDENCE PANEL -- standard errors surfaced in the UI
# ======================================================================

def _ceiling_now():
    # Highest score the LIVE pd floor permits. Recomputed on every call,
    # so it follows the calibrator instead of going stale.
    f = _floor_now()
    if not f or not (0.0 < f < 1.0):
        return None
    return round(OFFSET + FACTOR * math.log((1.0 - f) / f), 1)


def _at_ceiling(rows):
    # Rows sitting at the POLICY CEILING -- not merely at this sample's
    # maximum score, which is what at_top counts.
    c = _ceiling_now()
    if c is None:
        return 0
    out = 0
    for r in rows:
        try:
            v = float(r.get('score'))
        except (AttributeError, TypeError, ValueError):
            continue
        if v >= c - 0.05:
            out += 1
    return out


def _floor_now() -> Optional[float]:
    try:
        import pickle
        with open(os.path.join(ARTIFACTS, "calibrator.pkl"), "rb") as fh:
            d = pickle.load(fh)
        if isinstance(d, dict):
            return float(d.get("pd_floor") or 0.0)
        return float(getattr(d, "pd_floor", 0.0) or 0.0)
    except Exception:
        return None


@router.get("/api/v4/evidence")
def evidence(ibex_session: Optional[str] = Cookie(None)):
    """
    # [patched by fix_evidence.py]
    Serves the measured uncertainty panel.

    Figures come from artifacts/evidence_se.json, written by
    scripts/evidence_se.py --json-out. If that file is missing we return
    has_run=False and NO figures, so the panel states that uncertainty
    has not been measured for the artifacts currently being served
    instead of showing a previous build's numbers.
    """
    _require(ibex_session)
    path = os.path.join(ARTIFACTS, "evidence_se.json")
    live = None
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                live = json.load(fh)
        except Exception:
            live = None

    floor = _floor_now()
    ceiling = None
    if floor and 0 < floor < 1:
        ceiling = round(OFFSET + FACTOR * math.log((1 - floor) / floor), 1)

    caveats = [
        "Percentile intervals. Never converted via +/- 1.96 SE -- the "
        "replicate distributions are materially asymmetric.",
        "The booster was held fixed across replicates, so intervals "
        "reflect calibration and evaluation sampling variability, not "
        "model-fitting variability.",
        "The upper bound at the ceiling score is censored by the PD floor "
        "and should be read as at or below the floor.",
        "Calibration and evaluation slices cover different origination "
        "weeks; the base-rate difference is right-censoring, not model "
        "error.",
    ]

    if live is None:
        return {
            "has_run": False,
            "stale": True,
            "floor": floor,
            "ceiling": ceiling,
            "replicates": 0,
            "method": "block bootstrap over origination weeks",
            "gini": None,
            "tail": None,
            "points": [],
            "caveats": caveats,
            "message": (
                "Uncertainty has not been measured for the artifacts being "
                "served (" + str(ARTIFACTS) + "). Run scripts/evidence_se.py "
                "with --json-out " + path + " to populate this panel."
            ),
        }

    def _pair(v):
        try:
            return [float(v[0]), float(v[1])]
        except Exception:
            return None

    def _num(v, default=None):
        try:
            return float(v)
        except Exception:
            return default

    scores = live.get("ref_scores") or []
    pds = live.get("ref_pd_point") or []
    pd_cis = live.get("ref_pd_ci") or []
    sc_cis = live.get("ref_score_ci") or []
    points = []
    for i, sc in enumerate(scores):
        if i >= len(pds):
            break
        pd_ci = _pair(pd_cis[i]) if i < len(pd_cis) else None
        sc_ci = _pair(sc_cis[i]) if i < len(sc_cis) else None
        if pd_ci is None or sc_ci is None:
            continue
        points.append({
            "score": round(float(sc), 1),
            "pd": float(pds[i]),
            "pd_ci": pd_ci,
            "score_ci": sc_ci,
        })

    gini_point = _num(live.get("gini_point"))
    if gini_point is None:
        return {
            "has_run": False,
            "stale": True,
            "floor": floor,
            "ceiling": ceiling,
            "replicates": 0,
            "method": "block bootstrap over origination weeks",
            "gini": None,
            "tail": None,
            "points": [],
            "caveats": caveats,
            "message": "evidence_se.json is present but unreadable; re-run "
                       "scripts/evidence_se.py --json-out " + path,
        }

    tail_ci = _pair(live.get("tail_n_ci")) or [0.0, 0.0]
    return {
        "has_run": True,
        "stale": False,
        "floor": floor,
        "ceiling": ceiling,
        "replicates": int(live.get("n_boot") or 0),
        "method": "block bootstrap over origination weeks",
        "n_eval": live.get("n_eval"),
        "n_weeks_eval": live.get("n_weeks_eval"),
        "gini": {
            "point": gini_point,
            "ci": _pair(live.get("gini_ci_block")) or [gini_point, gini_point],
            "se_block": _num(live.get("se_gini_block"), 0.0),
            "se_delong": _num(live.get("se_gini_delong"), 0.0),
            "design_effect": round(_num(live.get("design_effect"), 0.0), 2),
        },
        "tail": {
            "obs": int(_num(live.get("tail_n_point"), 0)),
            "defaults": int(_num(live.get("tail_k_point"), 0)),
            "ci": [int(round(tail_ci[0])), int(round(tail_ci[1]))],
            "rule_of_three": live.get("rule_of_three"),
            "floor_ci": _pair(live.get("floor_ci")),
        },
        "floor_measured": live.get("floor_point"),
        "mean_pd_ci": _pair(live.get("mean_pd_ci")),
        "caveats": caveats,
        "points": points,
    }


# ======================================================================
# 4. BUSINESS VERIFICATION -- pull-based, consent-first
# ======================================================================

def _run_node(script: str, env_extra: Dict[str, str],
              timeout: int = 240) -> Dict[str, Any]:
    if not CHAIN_DIR or not os.path.isdir(CHAIN_DIR):
        raise HTTPException(500, "IBEX_CHAIN_DIR is not set to the chain repo")
    env = dict(os.environ)
    env.update(env_extra)
    cmd = ["npx", "hardhat", "run", script, "--network", CHAIN_NETWORK]
    try:
        p = subprocess.run(cmd, cwd=CHAIN_DIR, env=env, timeout=timeout,
                           capture_output=True, text=True, shell=(os.name == "nt"))
    except subprocess.TimeoutExpired:
        raise HTTPException(504, f"{script} timed out after {timeout}s")
    out = (p.stdout or "") + ("\n" + p.stderr if p.stderr else "")
    return {"ok": p.returncode == 0, "code": p.returncode, "log": out}


_ZERO32 = "0x" + "0" * 64


def _scrape_hashes(log: str) -> Dict[str, Optional[str]]:
    got: Dict[str, Optional[str]] = {
        "userHash": None, "scoreEventHash": None,
        "modelVersionHash": None, "merkleRoot": None, "txHash": None,
        "score": None, "band": None, "timestamp": None,
        "timestampEpoch": None,
    }
    for line in log.splitlines():
        low = line.lower()
        m = re.search(r"(0x[0-9a-fA-F]{64})", line)
        # An absent record reads back as an all-zero struct, and those zeros
        # still match the 64 hex digit pattern. Keeping them made a
        # nonexistent record report as FOUND, so discard them here.
        if m and m.group(1).lower() == _ZERO32:
            m = None
        if m:
            if "userhash" in low.replace(" ", ""):
                got["userHash"] = m.group(1)
            elif "scoreeventhash" in low.replace(" ", ""):
                got["scoreEventHash"] = m.group(1)
            elif "modelversionhash" in low.replace(" ", ""):
                got["modelVersionHash"] = m.group(1)
            elif "merkle" in low:
                got["merkleRoot"] = m.group(1)
            elif "tx" in low or "transaction" in low:
                got["txHash"] = m.group(1)
        # Never read a score off a hash line. "scoreEventHash: 0x0000..."
        # matched score\D{0,12}(\d{3}) and produced a score of "000". The
        # registry stores no score at all, so this can only ever pick up
        # submit-script output, never a chain read.
        ms = None if re.search(r"0x[0-9a-fA-F]{64}", line) else re.search(
            r"score\D{0,12}(\d{3})\b", low)
        if ms and got["score"] is None:
            got["score"] = ms.group(1)
        mb = re.search(r"band\W+([A-E])\b", line, re.I)
        if mb and got["band"] is None:
            got["band"] = mb.group(1).upper()
        mt = re.search(r"(\d{4}-\d{2}-\d{2}[T ][\d:]{5,8})", line)
        if mt and got["timestamp"] is None:
            got["timestamp"] = mt.group(1)
        # readLatestRecord.js prints "timestamp: 0" for an empty record and
        # "timestamp: <epoch> (<iso>)" for a real one. The epoch is the
        # authoritative existence check.
        me = re.search(r"timestamp:\s*(\d+)", low)
        if me and got["timestampEpoch"] is None:
            got["timestampEpoch"] = me.group(1)
    return got


def _read_record(user_hash: str) -> Dict[str, Any]:
    """Latest on-chain record for a userHash, without a node runtime.

    Prefers the direct JSON-RPC reader (works on the hosted python-only
    service) and falls back to the hardhat read script when the chain
    project is checked out locally. Returns {"ok", "log", "rec"} with rec
    shaped exactly like _scrape_hashes output.
    """
    try:
        from serve import chain_rpc
    except Exception:
        chain_rpc = None
    if chain_rpc is not None and chain_rpc.read_available():
        try:
            return chain_rpc.read_record(user_hash)
        except chain_rpc.ChainRevert as exc:
            raise HTTPException(exc.status, str(exc))
    res = _run_node("scripts/readLatestRecord.js", {"USER_HASH": user_hash})
    return {"ok": res["ok"], "log": res["log"],
            "rec": _scrape_hashes(res["log"])}


def _explorer() -> Tuple[str, str, int]:
    return EXPLORERS.get(CHAIN_NETWORK, (CHAIN_NETWORK, "", 0))


def _verify_event_rpc(ev: Dict[str, Any]) -> Tuple[bool, Dict[str, Any], str]:
    """verifyScoreEvent.js without node: recompute locally with chain_hash
    (byte-identical to the chain repo's utils), read the registry over
    JSON-RPC, and compare every field the script compares."""
    from serve import chain_rpc
    from serve.chain_hash import (user_hash, score_event_hash,
                                  model_version_hash)
    salt = _user_salt()
    if len(salt) < 16:
        raise HTTPException(
            500, "USER_SALT is not configured (16 chars minimum). It must "
                 "be byte-identical to the value used when the score was "
                 "anchored.")
    uh = user_hash(str(ev.get("userId", "")), salt)
    seh = score_event_hash(ev)
    mvh = model_version_hash(str(ev.get("modelVersion", "")))
    period = chain_rpc.score_period_of(ev)
    try:
        rec = chain_rpc.read_record(uh)["rec"]
    except chain_rpc.ChainRevert as exc:
        raise HTTPException(exc.status, str(exc))
    on_seh = str(rec.get("scoreEventHash") or "").lower()
    on_root = str(rec.get("merkleRoot") or "").lower()
    on_mvh = str(rec.get("modelVersionHash") or "").lower()
    on_period = int(rec.get("scorePeriod") or 0)
    # Single-leaf tree: the anchored merkle root IS the score event hash,
    # so an empty proof verifies iff root == leaf.
    valid = bool(
        on_seh
        and on_seh == seh.lower()
        and on_root == seh.lower()
        and on_mvh == mvh.lower()
        and on_period == period)
    log = ("(direct JSON-RPC verification -- no node runtime)\n"
           f"userHash: {uh}\n"
           f"scoreEventHash local: {seh}\n"
           f"scoreEventHash chain: {on_seh or '(no record)'}\n"
           f"modelVersionHash chain: {on_mvh or '(no record)'}\n"
           f"scorePeriod chain: {on_period or '(no record)'}\n"
           f"Verification result: {'VALID' if valid else 'INVALID'}\n")
    hashes = {
        "userHash": uh,
        "scoreEventHash": on_seh or None,
        "merkleRoot": on_root or None,
        "modelVersionHash": on_mvh or None,
        "txHash": None,
        "score": None,
        "band": None,
        "timestamp": rec.get("timestamp"),
        "timestampEpoch": rec.get("timestampEpoch"),
        "scorePeriod": on_period or None,
    }
    return valid, hashes, log


class VerifyEventBody(BaseModel):
    event_json: str


@router.post("/api/v4/verify/event")
def verify_event(body: VerifyEventBody):
    """
    Route 1: the person hands over score-event.json.
    We hash it, read the contract record, and compare.
    Deliberately unauthenticated -- a business is not an Ibex user.
    """
    try:
        ev = json.loads(body.event_json)
    except Exception as exc:
        raise HTTPException(400, f"that is not valid JSON: {exc}")
    if not isinstance(ev, dict):
        raise HTTPException(400, "expected a JSON object")

    for k in ("userId", "newScore"):
        if k not in ev:
            raise HTTPException(
                400, f"missing '{k}' -- this does not look like a "
                     f"score-event.json produced by Ibex")

    canonical = json.dumps(ev, sort_keys=True, separators=(",", ":"))
    local_sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    valid: Optional[bool] = None
    hashes: Dict[str, Any] = {}
    res: Dict[str, Any] = {"ok": False, "log": ""}
    try:
        from serve import chain_rpc as _crpc
        if _crpc.read_available():
            valid, hashes, rpc_log = _verify_event_rpc(ev)
            res = {"ok": True, "log": rpc_log}
    except HTTPException:
        raise
    except Exception as exc:
        print("rpc verify/event fell back to hardhat:", exc)
        valid = None

    if valid is None:
        tmp = os.path.join(CHAIN_DIR or ".", "_verify_input.json")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(ev, fh, indent=2)

        res = _run_node("scripts/verifyScoreEvent.js",
                        {"SCORE_EVENT_FILE": tmp})
        hashes = _scrape_hashes(res["log"])
        low = res["log"].lower()
        valid = res["ok"] and ("valid" in low) and ("invalid" not in low)

    name, base, chain_id = _explorer()
    return {
        "ok": res["ok"],
        "match": bool(valid),
        "verdict": ("VALID -- this score was issued by Ibex and has not "
                    "been altered since it was anchored")
        if valid else
        ("NO MATCH -- the contract holds no record equal to this file, "
         "so it was either never anchored or has been edited"),
        "local_sha256": local_sha,
        "claimed": {
            "userId": ev.get("userId"),
            "score": ev.get("newScore"),
            "band": ev.get("scoreBand"),
            "timestamp": ev.get("timestamp"),
            "modelVersion": ev.get("modelVersion"),
        },
        "onchain": hashes,
        "network": CHAIN_NETWORK,
        "chain_id": chain_id,
        "contract": CONTRACT,
        "explorer": f"{base}/address/{CONTRACT}" if base else None,
        "limitation": ("This proves the score is authentic and unaltered. "
                       "It does NOT prove the person presenting it is its "
                       "subject -- that needs a signed wallet challenge."),
        "log": res["log"][-4000:],
    }


class VerifyHashBody(BaseModel):
    user_hash: str


@router.post("/api/v4/verify/hash")
def verify_hash(body: VerifyHashBody):
    """Route 2: the person gives a userHash; read the latest record."""
    h = (body.user_hash or "").strip()
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", h):
        raise HTTPException(
            400, "a userHash is 0x followed by 64 hex characters")
    rr = _read_record(h)
    res = {"ok": rr["ok"], "log": rr["log"]}
    rec = rr["rec"]
    name, base, chain_id = _explorer()
    found = res["ok"] and any(
        rec[k] for k in ("scoreEventHash", "merkleRoot", "score"))
    return {
        "ok": res["ok"],
        "found": bool(found),
        "user_hash": h,
        "record": rec,
        "network": CHAIN_NETWORK,
        "chain_id": chain_id,
        "contract": CONTRACT,
        "explorer": f"{base}/address/{CONTRACT}" if base else None,
        "log": res["log"][-4000:],
    }


# ======================================================================
# 5. ADMIN COHORT -- the 709 fix
# ======================================================================
#
# v3 did:  pd = clip(interp(raw, x, y), floor, 1-floor)
#
# That is NOT production. It throws away the hybrid log-linear tail, so
# every applicant in the bottom isotonic block collapses onto the floor
# and scores exactly 709.0. Production extrapolates through the backbone
# and only then applies the floor, which preserves ordering inside the
# tail. v4 loads the real calibrator and calls it.

_cal_cache: Dict[str, Any] = {}


def load_real_calibrator():
    """
    Returns (predict_fn, how) where predict_fn maps raw margins -> PD.
    Tries, in order:
      1. the calibrator class from obcredit.modeling.calibration
      2. an object pickled with a .predict method
      3. plain interpolation (LAST RESORT -- flagged loudly)
    """
    if _cal_cache.get("fn") is not None:
        return _cal_cache["fn"], _cal_cache["how"]

    import numpy as np
    import pickle
    path = os.path.join(ARTIFACTS, "calibrator.pkl")
    if not os.path.exists(path):
        raise RuntimeError(f"missing {path}")

    def _norm(res):
        if isinstance(res, tuple):
            res = res[0]
        return np.asarray(res, dtype=float)

    # 1. the production class
    try:
        from obcredit.modeling import calibration as calmod
        for nm, obj in vars(calmod).items():
            if not inspect.isclass(obj):
                continue
            if not (hasattr(obj, "load") and hasattr(obj, "predict")):
                continue
            try:
                inst = obj.load(path)
            except Exception:
                continue
            fn = lambda raw, _i=inst: _norm(_i.predict(np.asarray(raw, float)))
            fn(np.zeros(3))  # smoke test
            _cal_cache.update(fn=fn, how=f"production class {nm}.load()")
            return fn, _cal_cache["how"]
    except Exception:
        pass

    # 2. a pickled object that can predict for itself
    try:
        with open(path, "rb") as fh:
            d = pickle.load(fh)
        if hasattr(d, "predict"):
            fn = lambda raw, _o=d: _norm(_o.predict(np.asarray(raw, float)))
            fn(np.zeros(3))
            _cal_cache.update(fn=fn, how="pickled object .predict()")
            return fn, _cal_cache["how"]
    except Exception:
        d = None

    # 3. last resort -- reconstruct the hybrid tail by hand from the dict
    with open(path, "rb") as fh:
        d = pickle.load(fh)
    if not isinstance(d, dict):
        raise RuntimeError("calibrator.pkl is neither a class nor a dict")

    x = np.asarray(d["x"], float)
    y = np.asarray(d["y"], float)
    floor = float(d.get("pd_floor") or 0.0003)
    bb = d.get("backbone")

    def fn(raw, _x=x, _y=y, _f=floor, _bb=bb):
        raw = np.asarray(raw, float)
        out = np.interp(raw, _x, _y, left=_y[0], right=_y[-1])
        # log-linear extrapolation through the fitted backbone in the
        # flat lower plateau, which is exactly what production does
        if _bb is not None:
            try:
                a, b = float(_bb[0]), float(_bb[1])
                lo = _y.min()
                flat = out <= lo + 1e-12
                if flat.any():
                    z = a + b * raw[flat]
                    out[flat] = 1.0 / (1.0 + np.exp(-np.clip(z, -700, 700)))
            except Exception:
                pass
        return np.clip(out, _f, 1.0 - _f)

    _cal_cache.update(fn=fn, how="reconstructed hybrid tail (fallback)")
    return fn, _cal_cache["how"]


_cohort: Dict[str, Any] = {"status": "idle", "message": "", "rows": [],
                           "how": "", "summary": {}}
_cohort_lock = threading.Lock()


def _num(v, default=None):
    try:
        f = float(v)
        return default if f != f else f
    except Exception:
        return default


def characterise(row: Dict[str, Any]) -> str:
    bits: List[str] = []
    n_ob = _num(row.get("num_active_obligations"), 0.0) or 0.0
    bits.append("no-file" if n_ob <= 0 else
                "ultra-thin" if n_ob <= 1 else
                "thin" if n_ob <= 2 else
                "medium" if n_ob <= 5 else "thick")
    dpd = _num(row.get("max_dpd_24m"), 0.0) or 0.0
    ser = _num(row.get("num_serious_arrears_24m"), 0.0) or 0.0
    bits.append("serious arrears" if ser >= 1 else
                "late payments" if dpd >= 8 else
                "minor lateness" if dpd > 0 else "clean conduct")
    dti = _num(row.get("debt_to_income"))
    if dti is not None:
        bits.append("stretched" if dti > 0.6 else
                    "moderate leverage" if dti > 0.3 else "comfortable")
    cv = _num(row.get("cv_payment_amount"))
    if cv is not None and cv > 0.5:
        bits.append("volatile income")
    return ", ".join(bits)


ADMIN_SAMPLE = os.path.normpath(
    os.path.join(_HERE, "..", "fixtures", "ob_matrix_admin100.pkl"))


def _resolve_admin_matrix() -> Tuple[str, bool]:
    """Which matrix the admin cohort scores.

    The full OB matrix is gigabytes and only exists on the research machine;
    on Render (512 MB) it is absent, so fall back to the bundled 100-row
    evaluation-slice sample. Returns (path, using_sample).
    """
    if OB_MATRIX and os.path.exists(OB_MATRIX):
        return OB_MATRIX, False
    if os.path.exists(ADMIN_SAMPLE):
        return ADMIN_SAMPLE, True
    raise RuntimeError(
        "no OB matrix available: set IBEX_OB_MATRIX to the full pickle, or "
        "bundle fixtures/ob_matrix_admin100.pkl (scripts/make_admin_sample.py)")


def _build_cohort(n: int, seed: int) -> None:
    try:
        import numpy as np
        import pandas as pd
        import lightgbm as lgb

        matrix_path, using_sample = _resolve_admin_matrix()
        card_path = os.path.join(ARTIFACTS, "scorecard.json")
        model_path = os.path.join(ARTIFACTS, "model_lgbm.txt")
        for p in (card_path, model_path):
            if not os.path.exists(p):
                raise RuntimeError(f"missing {p}")
        with open(card_path, "r", encoding="utf-8") as fh:
            card = json.load(fh)

        predict_pd, how = load_real_calibrator()
        _cohort["how"] = how

        _cohort["message"] = ("loading bundled 100-row admin sample"
                              if using_sample else
                              "loading OB matrix (about a minute)")
        m = pd.read_pickle(matrix_path)
        sub = m.dropna(subset=["target", "__week__"]).sort_values(
            "__week__", kind="mergesort")
        if using_sample:
            # The bundled sample was already drawn from the evaluation slice;
            # taking the 80/20 cut of a 100-row file would leave 20 rows.
            pool = sub
        else:
            pool = sub.iloc[int(len(sub) * 0.8):]
        take = min(n, len(pool))
        sample = pool.sample(take, random_state=seed).copy()
        del m, sub, pool

        _cohort["message"] = f"scoring {take} applicants"
        feats = list(card["features"])
        medians = card.get("medians", {}) or {}
        best = int(card.get("best_iteration") or 0) or None

        X = sample.reindex(columns=feats)
        for c in feats:
            if medians.get(c) is not None:
                X[c] = X[c].fillna(medians[c])
        X = X.fillna(0.0).to_numpy(np.float32)

        booster = lgb.Booster(model_file=model_path)
        raw = booster.predict(X, num_iteration=best)

        pds = np.asarray(predict_pd(raw), dtype=float)
        scores = OFFSET + FACTOR * np.log((1.0 - pds) / pds)

        rows = []
        for i, r in enumerate(sample.to_dict(orient="records")):
            s = float(scores[i])
            rows.append({
                "n": i + 1,
                "score": round(s),
                "band": _band_of(s),
                "pd": round(float(pds[i]), 5),
                "defaulted": int(_num(r.get("target"), 0) or 0),
                "profile": characterise(r),
                "oblig": _num(r.get("num_active_obligations"), 0),
                "max_dpd": _num(r.get("max_dpd_24m"), 0),
                "dti": (None if _num(r.get("debt_to_income")) is None
                        else round(_num(r.get("debt_to_income")), 3)),
                "income": (None if _num(r.get("monthly_income")) is None
                           else round(_num(r.get("monthly_income")), 2)),
            })
        rows.sort(key=lambda d: d["score"], reverse=True)
        for i, r in enumerate(rows):
            r["n"] = i + 1

        uniq = len({r["score"] for r in rows})
        top = max((r["score"] for r in rows), default=0)
        at_top = sum(1 for r in rows if r["score"] == top)
        bands: Dict[str, int] = {}
        for r in rows:
            bands[r["band"]] = bands.get(r["band"], 0) + 1
        _cohort["summary"] = {
            "n": len(rows),
            "distinct_scores": uniq,
            "top_score": top,
            "at_top_score": at_top,
            "ceiling_score": _ceiling_now(),
            "at_ceiling": _at_ceiling(rows),
            "pct_at_top_score": round(100.0 * at_top / max(1, len(rows)), 1),
            "pct_at_ceiling": round(
                100.0 * _at_ceiling(rows) / max(1, len(rows)), 1),
            "bands": bands,
            "defaults": sum(r["defaulted"] for r in rows),
            "calibrator": how,
        }
        _cohort["rows"] = rows
        _cohort["status"] = "ready"
        _cohort["message"] = f"{len(rows)} scored, {uniq} distinct values"
    except Exception as exc:
        _cohort["status"] = "error"
        _cohort["message"] = str(exc)


@router.get("/api/v4/admin/cohort")
def admin_cohort(n: int = 50, seed: int = 42, refresh: bool = False,
                 ibex_session: Optional[str] = Cookie(None)):
    _require(ibex_session, admin=True)
    with _cohort_lock:
        if _cohort["status"] == "loading":
            return {"status": "loading", "message": _cohort["message"]}
        if _cohort["status"] == "ready" and not refresh:
            return {"status": "ready", "rows": _cohort["rows"],
                    "summary": _cohort["summary"], "message": _cohort["message"]}
        _cohort.update(status="loading", rows=[], summary={},
                       message="starting")
    threading.Thread(target=_build_cohort,
                     args=(max(1, min(int(n), 500)), int(seed)),
                     daemon=True).start()
    return {"status": "loading", "message": "started"}


@router.get("/api/v4/admin/cohort/status")
def admin_cohort_status(ibex_session: Optional[str] = Cookie(None)):
    _require(ibex_session, admin=True)
    return {"status": _cohort["status"], "message": _cohort["message"],
            "summary": _cohort.get("summary", {}),
            "rows": _cohort["rows"] if _cohort["status"] == "ready" else []}


@router.get("/api/v4/diag/calibrator")
def diag_calibrator(ibex_session: Optional[str] = Cookie(None)):
    """Which calibration path the cohort will use, and the current floor."""
    _require(ibex_session, admin=True)
    try:
        _, how = load_real_calibrator()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    floor = _floor_now()
    return {
        "ok": True,
        "path": how,
        "is_production": how.startswith("production"),
        "pd_floor": floor,
        "ceiling_score": (round(OFFSET + FACTOR * math.log((1 - floor) / floor), 1)
                          if floor else None),
    }


@router.get("/ibex", response_class=HTMLResponse)
def ibex_page():
    path = os.path.join(_STATIC, "ibex.html")
    if not os.path.exists(path):
        return HTMLResponse("<h1>ibex.html missing from serve/static</h1>",
                            status_code=500)
    with open(path, "r", encoding="utf-8") as fh:
        return HTMLResponse(fh.read())


# ==== BEGIN business identity verification ====
# Added by scripts/add_business_verify.py -- powers serve/static/business.html

_IDENT_HITS = {}
_IDENT_LOCK = threading.Lock()
_IDENT_MAX = int(os.environ.get("IBEX_VERIFY_RATE_MAX", "20"))
_IDENT_WINDOW = float(os.environ.get("IBEX_VERIFY_RATE_WINDOW", "3600"))


def _ident_rate_ok(key: str) -> bool:
    # Application level throttle. This is NOT the same thing as the on-chain
    # rate limit -- it only protects this server and the RPC quota. A caller
    # who talks to the contract directly is unaffected, which is exactly why
    # the limit also belongs in the contract.
    now = time.time()
    with _IDENT_LOCK:
        hits = [t for t in _IDENT_HITS.get(key, []) if now - t < _IDENT_WINDOW]
        if len(hits) >= _IDENT_MAX:
            _IDENT_HITS[key] = hits
            return False
        hits.append(now)
        _IDENT_HITS[key] = hits
        return True


class VerifyIdentityBody(BaseModel):
    name: str
    email: str
    claimed_score: Optional[float] = None
    claimed_band: Optional[str] = None


@router.post("/api/v4/verify/identity")
def verify_identity(body: VerifyIdentityBody, request: Request):
    # Route 3: business holds a name, an email and a claimed score.
    client = request.client.host if request.client else "unknown"
    if not _ident_rate_ok(client):
        raise HTTPException(
            429, "too many verification attempts from this address, "
                 "try again later")

    name = (body.name or "").strip()
    email = (body.email or "").strip().lower()
    if not name or not email:
        raise HTTPException(400, "name and email are both required")

    # The chain repo derives identity as keccak256(utf8(userId + ":" + salt))
    # with userId = the email (utils/hashScoreEvent.js:51). This previously
    # called v3.make_handle(), which is sha256 over a salt|email|bank-name
    # triple -- a hash the contract has never seen, so every lookup missed.
    from serve.chain_hash import chain_user_hash, normalise_name

    salt = _user_salt()
    if len(salt) < 16:
        raise HTTPException(
            500, "USER_SALT is not configured (16 chars minimum). It must "
                 "match the chain repo .env exactly or no record will ever "
                 "be found.")

    # Identity is keccak256("email|NORMALISED NAME" + ":" + salt). A business
    # will rarely type the name exactly as the bank holds it, and a near miss
    # is indistinguishable from "never scored", so try the plausible spellings
    # before reporting no match. Middle names are the usual culprit.
    parts = normalise_name(name).split()
    variants = [name]
    if len(parts) > 2:
        variants.append(parts[0] + " " + parts[-1])
    if len(parts) >= 2:
        variants.append(parts[0][0] + " " + parts[-1])

    seen: set = set()
    tried: List[Dict[str, str]] = []
    h = ""
    rec: Dict[str, Any] = {}
    res: Dict[str, Any] = {"ok": False, "log": ""}
    found = False

    for variant in variants:
        cand = chain_user_hash(email, variant, salt)
        if cand in seen:
            continue
        seen.add(cand)
        if not re.fullmatch(r"0x[0-9a-fA-F]{64}", cand):
            raise HTTPException(500, "computed handle is not a 32 byte hash")
        tried.append({"name": normalise_name(variant), "user_hash": cand})
        h = cand
        rr = _read_record(cand)
        res = {"ok": rr["ok"], "log": rr["log"]}
        rec = rr["rec"]
        # A record exists only when the registry returns a non-zero
        # scoreEventHash AND a non-zero timestamp. Anything else is the empty
        # struct latestRecordByUserHash returns for an unknown userHash.
        found = bool(
            res["ok"]
            and rec.get("scoreEventHash")
            and (rec.get("timestampEpoch") or "0") != "0")
        if found:
            break

    score_match = None
    if found and body.claimed_score is not None and rec.get("score") is not None:
        try:
            score_match = abs(
                float(rec["score"]) - float(body.claimed_score)) < 0.5
        except (TypeError, ValueError):
            score_match = None

    band_match = None
    if found and body.claimed_band and rec.get("band"):
        band_match = (str(rec["band"]).strip().upper()
                      == body.claimed_band.strip().upper())

    match = bool(found) and score_match is not False and band_match is not False

    if not found:
        verdict = ("NO MATCH -- the contract holds no record for this name "
                   "and email. Either this person has never been scored by "
                   "Ibex, or the name given does not match the one their "
                   "bank returned.")
    elif score_match is False:
        verdict = ("MISMATCH -- Ibex has a record for this person, but the "
                   "anchored score is not the one being claimed.")
    elif band_match is False:
        verdict = ("MISMATCH -- the anchored band is not the one being "
                   "claimed.")
    elif score_match is None:
        verdict = ("FOUND -- Ibex anchored a record for this name and email, "
                   "but the registry stores only hashes, never the score "
                   "itself, so the claimed score cannot be checked from the "
                   "chain alone. Use the score event file tab to confirm it.")
    else:
        verdict = ("VERIFIED -- Ibex issued this score to this person and it "
                   "has not been altered since it was anchored.")

    _net, base, chain_id = _explorer()
    return {
        "ok": res["ok"],
        "found": bool(found),
        "match": match,
        "verdict": verdict,
        "score_match": score_match,
        "band_match": band_match,
        "user_hash": h,
        "claimed": {
            "name": name,
            "score": body.claimed_score,
            "band": body.claimed_band,
        },
        "record": rec,
        "network": CHAIN_NETWORK,
        "chain_id": chain_id,
        "contract": CONTRACT,
        "explorer": f"{base}/address/{CONTRACT}" if base else None,
        "name_checked": True,
        "tried": tried,
        "limitation": (
            "This proves Ibex issued this score to this name and email and "
            "that it has not been altered since it was anchored. The name is "
            "the one this person's bank returned over open banking, so it is "
            "bank-verified rather than self-declared. It does NOT prove the "
            "person presenting the score is its subject; that needs a signed "
            "wallet challenge."),
        "log": res["log"][-4000:],
    }


class VerifyTxBody(BaseModel):
    tx_hash: str
    name: str = ""
    email: str = ""
    claimed_score: Optional[float] = None


@router.post("/api/v4/verify/tx")
def verify_tx(body: VerifyTxBody, request: Request):
    """
    Route 4: name, email, claimed score and the transaction id for that one
    score. This is the only route that can check all three at once.

    The registry stores hashes, never the score, so a claimed figure can never
    be checked against the chain by itself. This route closes that gap with
    the score event kept at anchoring time, and requires all four to hold:

      1. the stored event re-hashes to the hash that was anchored
      2. that hash is what the contract actually holds for this identity
      3. the anchored identity hash matches this name and email
      4. the anchored score equals the claimed score
    """
    client = request.client.host if request.client else "unknown"
    if not _ident_rate_ok(client):
        raise HTTPException(
            429, "too many verification attempts from this address, "
                 "try again later")

    from serve import anchor_store
    from serve.chain_hash import (chain_user_hash, normalise_name,
                                  score_event_hash)

    tx = (body.tx_hash or "").strip()
    if not anchor_store.is_tx_hash(tx):
        raise HTTPException(
            400, "a transaction id is 0x followed by 64 hex characters")

    _nm, base, chain_id = _explorer()
    out: Dict[str, Any] = {
        "ok": True,
        "found": False,
        "match": False,
        "tx_hash": tx,
        "network": CHAIN_NETWORK,
        "chain_id": chain_id,
        "contract": CONTRACT,
        "explorer": f"{base}/tx/{tx}" if base else None,
        "claimed": {"name": body.name, "email": body.email,
                    "score": body.claimed_score},
    }

    stored = anchor_store.load_by_tx(tx)
    if not stored:
        out["verdict"] = (
            "NO MATCH -- this Ibex deployment has no record of anchoring that "
            "transaction. Either the id is wrong, or the score was anchored "
            "by a different deployment. Ask for the score file instead.")
        out["limitation"] = (
            "Only transactions anchored by this deployment can be resolved "
            "to a score, because the score lives in the score event rather "
            "than in the contract.")
        return out

    out["found"] = True
    event = stored.get("event") or {}
    anchored_hash = str(stored.get("score_event_hash") or "").lower()

    # 1. integrity: the stored event must re-hash to what was anchored.
    recomputed = ""
    try:
        recomputed = score_event_hash(event).lower()
    except Exception as exc:
        out["hash_error"] = str(exc)
    integrity_ok = bool(anchored_hash) and recomputed == anchored_hash

    # 2. the contract must currently hold that hash for this identity.
    user_hash = str(stored.get("user_hash") or "")
    rr = _read_record(user_hash)
    res = {"ok": rr["ok"], "log": rr["log"]}
    chain_rec = rr["rec"]
    on_chain = str(chain_rec.get("scoreEventHash") or "").lower()
    chain_ok = bool(on_chain) and on_chain == anchored_hash

    # 3. identity. Middle names are the usual near miss, so try the same
    #    spellings the details route tries.
    identity_match = None
    tried: List[Dict[str, str]] = []
    if body.name.strip() and body.email.strip():
        salt = _user_salt()
        if len(salt) < 16:
            raise HTTPException(
                500, "USER_SALT is not configured (16 chars minimum). It "
                     "must match the chain repo .env exactly.")
        email = body.email.strip().lower()
        parts = normalise_name(body.name).split()
        variants = [body.name]
        if len(parts) > 2:
            variants.append(parts[0] + " " + parts[-1])
        if len(parts) >= 2:
            variants.append(parts[0][0] + " " + parts[-1])
        identity_match = False
        seen: set = set()
        for variant in variants:
            cand = chain_user_hash(email, variant, salt).lower()
            if cand in seen:
                continue
            seen.add(cand)
            tried.append({"name": normalise_name(variant), "user_hash": cand})
            if cand == user_hash.lower():
                identity_match = True
                break

    # 4. the score, read from the anchored event rather than the chain.
    score_match = None
    event_score = event.get("newScore", stored.get("score"))
    if body.claimed_score is not None and event_score is not None:
        try:
            score_match = abs(
                float(event_score) - float(body.claimed_score)) < 0.5
        except (TypeError, ValueError):
            score_match = None

    out.update({
        "integrity_ok": integrity_ok,
        "chain_ok": chain_ok,
        "identity_match": identity_match,
        "score_match": score_match,
        "user_hash": user_hash,
        "anchored_score": event_score,
        "anchored_band": stored.get("band") or event.get("scoreBand"),
        "anchored_at": stored.get("anchored_at"),
        "score_event_hash": anchored_hash,
        "on_chain_score_event_hash": on_chain,
        "model_version_hash": stored.get("model_version_hash"),
        "tried": tried,
        "log": res["log"][-4000:],
        # Shaped for the shared result renderer on business.html.
        "record": {
            "score": event_score,
            "band": stored.get("band") or event.get("scoreBand"),
            "scoreEventHash": on_chain or anchored_hash,
            "txHash": tx,
            "timestamp": event.get("timestamp") or stored.get("anchored_at"),
        },
    })

    if not integrity_ok:
        out["verdict"] = (
            "TAMPERED -- the score event held for this transaction does not "
            "re-hash to the value that was anchored. Treat this credential "
            "as unreliable.")
    elif not chain_ok:
        out["verdict"] = (
            "NOT CONFIRMED ON CHAIN -- the stored event is internally "
            "consistent, but the contract does not currently hold that hash "
            "for this identity. It may have been superseded by a newer "
            "score, or the transaction may not have been mined.")
    elif identity_match is False:
        out["verdict"] = (
            "MISMATCH -- that transaction is a genuine Ibex anchor, but it "
            "was not issued to this name and email. The name has to match "
            "the one their bank returned.")
    elif score_match is False:
        out["verdict"] = (
            "MISMATCH -- a genuine Ibex anchor for this person, but the "
            f"anchored score is {event_score}, not {body.claimed_score}.")
    elif identity_match is None or score_match is None:
        out["verdict"] = (
            "FOUND -- a genuine Ibex anchor, confirmed against the contract. "
            "Supply the name, email and claimed score as well to check that "
            "it belongs to this person and matches what they told you.")
    else:
        out["verdict"] = (
            "VERIFIED -- Ibex issued this score to this person, it is the "
            "score they claimed, and it has not been altered since it was "
            "anchored.")

    out["match"] = bool(
        integrity_ok and chain_ok
        and identity_match is True and score_match is True)
    out["limitation"] = (
        "This proves Ibex issued this exact score to this bank-verified name "
        "and email and that nothing has changed since it was anchored. It "
        "does not prove the person presenting it is its subject; that needs "
        "photo ID or a signed wallet challenge.")
    return out


# ==== END business identity verification ====


# ==== BEGIN score history ====
@router.get("/api/v4/score/history")
def score_history_route(limit: int = 10,
                        ibex_session: Optional[str] = Cookie(None)):
    # Previous scores for the signed-in user. File-backed, no database.
    user = _require(ibex_session)
    import serve.score_history as _hist
    rows = _hist.read_history(user["email"], limit=limit)
    return {
        "ok": True,
        "count": len(rows),
        "history": rows,
        "current": _LAST.get(user["email"]),
        "store": _hist.history_path(),
    }


# ==== BEGIN profile dashboard ====
@router.get("/api/v4/profile/dashboard")
def profile_dashboard(ibex_session: Optional[str] = Cookie(None)):
    """One call with everything the dashboard page needs: who is signed in,
    their current and previous scores, and their latest on-chain anchor."""
    user = _require(ibex_session)
    email = user["email"]

    rows: List[Dict[str, Any]] = []
    try:
        import serve.score_history as _hist
        rows = _hist.read_history(email, limit=10)
    except Exception as exc:
        print("dashboard history read failed:", exc)

    current = _LAST.get(email)
    if current is None and rows:
        latest = rows[0]
        current = {"score": latest.get("score"), "band": latest.get("band"),
                   "pd": latest.get("pd"), "timestamp": latest.get("timestamp")}

    anchor: Optional[Dict[str, Any]] = None
    try:
        from serve import anchor_store
        prefix = email + "|"
        mine = []
        for rec in anchor_store._all_records():
            uid = str((rec.get("event") or {}).get("userId", ""))
            if uid == email or uid.startswith(prefix):
                mine.append(rec)
        if mine:
            mine.sort(key=lambda r: str(r.get("anchored_at", "")),
                      reverse=True)
            r0 = mine[0]
            _nm, base, _cid = _explorer()
            tx = r0.get("tx_hash", "")
            anchor = {
                "txHash": tx,
                "explorer": (base + "/tx/" + tx) if tx else "",
                "network": r0.get("network", ""),
                "score": r0.get("score"),
                "band": r0.get("band", ""),
                "anchored_at": r0.get("anchored_at", ""),
                "scoreEventHash": r0.get("score_event_hash", ""),
                "count": len(mine),
            }
    except Exception as exc:
        print("dashboard anchor read failed:", exc)

    return {
        "ok": True,
        "email": email,
        "name": user.get("name") or email.split("@")[0],
        "created": user.get("created", ""),
        "current": current,
        "history": rows,
        "anchor": anchor,
    }
# ==== END profile dashboard ====


@router.get("/api/v4/chain/status")
def chain_status_route(ibex_session: Optional[str] = Cookie(None)):
    # What the blockchain panel needs to describe the current wiring.
    # Deliberately honest: when the chain is off it says so, rather than
    # letting the page imply an anchor exists when none does.
    _require(ibex_session)
    try:
        name, base, chain_id = _explorer()
    except Exception:
        name, base, chain_id = "", "", None
    enabled = bool(CHAIN_NETWORK) and str(CHAIN_NETWORK).lower() != "none"
    return {
        "ok": True,
        "enabled": enabled,
        "network": CHAIN_NETWORK,
        "chain_id": chain_id,
        "contract": CONTRACT,
        "explorer": name,
        "explorer_base": base,
        "chain_dir": CHAIN_DIR or "",
        "reason": (None if enabled else
                   "IBEX_CHAIN_NETWORK is none, so scores are not anchored "
                   "on chain yet."),
    }
# ==== END score history ====
