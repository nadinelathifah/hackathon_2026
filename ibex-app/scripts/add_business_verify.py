#!/usr/bin/env python3
"""add_business_verify.py -- add POST /api/v4/verify/identity to serve/ibex_v4.py.

Run from the step3 repo root:
    py -3.13 scripts/add_business_verify.py

Why a new endpoint is needed: a business cannot compute the on-chain handle
itself. make_handle() mixes in IBEX_APP_SALT, which is server side, and the
name+email space is small enough to brute force if the salt were published.
So the business sends name + email + the score being claimed, the server
recomputes the handle, reads the contract, and compares.

Idempotent. Backs up to serve/ibex_v4.py.bizbak once.
"""
from __future__ import annotations
import os
import sys

TARGET = os.path.join("serve", "ibex_v4.py")
MARK = "# ==== BEGIN business identity verification ===="

OLD_IMPORT = "from fastapi import APIRouter, Cookie, HTTPException"
NEW_IMPORT = "from fastapi import APIRouter, Cookie, HTTPException, Request"

BLOCK = '''

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

    handle = v3.make_handle(email, name)
    h = handle if str(handle).startswith("0x") else "0x" + str(handle)
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", h):
        raise HTTPException(500, "computed handle is not a 32 byte hash")

    res = _run_node("scripts/readLatestRecord.js", {"USER_HASH": h})
    rec = _scrape_hashes(res["log"])
    found = res["ok"] and any(
        rec.get(k) for k in ("scoreEventHash", "merkleRoot", "score"))

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
        verdict = ("FOUND -- a record exists for this person, but no score "
                   "was supplied to compare it against.")
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
        "limitation": (
            "This proves the score is authentic and unaltered, and that it "
            "is bound to the name this person's bank returned over open "
            "banking. It does NOT prove the person presenting it is its "
            "subject -- that needs a signed wallet challenge."),
        "log": res["log"][-4000:],
    }

# ==== END business identity verification ====
'''


def main():
    if not os.path.exists(TARGET):
        sys.exit("FATAL: run from the repo root (missing " + TARGET + ")")
    src = open(TARGET, encoding="utf-8").read()

    if MARK in src:
        print("[add_business_verify] already patched -- nothing to do")
        return 0

    if OLD_IMPORT not in src and "Request" not in src.split("router =")[0]:
        sys.exit("FATAL: could not find the fastapi import line to extend")

    bak = TARGET + ".bizbak"
    if not os.path.exists(bak):
        open(bak, "w", encoding="utf-8", newline="").write(src)

    out = src.replace(OLD_IMPORT, NEW_IMPORT, 1) if OLD_IMPORT in src else src
    out = out.rstrip() + "\n" + BLOCK

    open(TARGET, "w", encoding="utf-8", newline="\n").write(out)
    print("[add_business_verify] added POST /api/v4/verify/identity to " + TARGET)
    print("[add_business_verify] backup at " + bak)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
