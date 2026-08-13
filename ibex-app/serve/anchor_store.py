"""
File-backed record of what Ibex has anchored on chain.

Why this exists
---------------
ScoreAuditRegistry stores only hashes: scoreEventHash, merkleRoot,
modelVersionHash, timestamp, issuer. It does NOT store the score. So a
business holding a name, an email and a claimed score cannot verify that
score from the chain alone -- there is nothing there to compare against.

The missing piece is the score event itself. Keeping a copy of every event
we anchor, keyed by transaction hash, closes the loop:

  1. the stored event's userId hashes to the identity being claimed
  2. the stored event's newScore equals the score being claimed
  3. the stored event re-hashes to the scoreEventHash we anchored
  4. that scoreEventHash is what the contract actually holds

Steps 1-3 are pure Python via chain_hash. Only step 4 touches the chain.

No database: one JSON file per transaction, which is also why the anchor
record survives a server restart when the in-memory score store does not.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_TX_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


def anchor_dir() -> str:
    """Where anchor records live. Override with IBEX_ANCHOR_DIR."""
    d = os.environ.get("IBEX_ANCHOR_DIR", "").strip()
    if not d:
        here = os.path.dirname(os.path.abspath(__file__))
        d = os.path.join(here, "_anchors")
    os.makedirs(d, exist_ok=True)
    return d


def is_tx_hash(value: str) -> bool:
    return bool(_TX_RE.match((value or "").strip()))


def _path_for(tx_hash: str) -> str:
    return os.path.join(anchor_dir(), tx_hash.strip().lower() + ".json")


def save(
    tx_hash: str,
    user_hash: str,
    event: Dict[str, Any],
    score_event_hash: str = "",
    merkle_root: str = "",
    model_version_hash: str = "",
    network: str = "",
    contract: str = "",
    score: Optional[float] = None,
    band: str = "",
) -> str:
    """
    Persist one anchored score event. Returns the file path written.

    Written atomically: a half-written anchor record would make a genuine
    credential unverifiable, which is worse than having no record at all.
    """
    if not is_tx_hash(tx_hash):
        raise ValueError(f"not a transaction hash: {tx_hash!r}")

    rec: Dict[str, Any] = {
        "tx_hash": tx_hash.strip().lower(),
        "user_hash": (user_hash or "").strip().lower(),
        "score_event_hash": (score_event_hash or "").strip().lower(),
        "merkle_root": (merkle_root or "").strip().lower(),
        "model_version_hash": (model_version_hash or "").strip().lower(),
        "network": network,
        "contract": contract,
        "score": score,
        "band": band,
        "anchored_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "event": event,
    }

    out = _path_for(tx_hash)
    d = os.path.dirname(out)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".anchor-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, indent=2, sort_keys=True)
        os.replace(tmp, out)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return out


def load_by_tx(tx_hash: str) -> Optional[Dict[str, Any]]:
    """Return the anchor record for a transaction hash, or None."""
    if not is_tx_hash(tx_hash):
        return None
    try:
        with open(_path_for(tx_hash), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _all_records() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        names = os.listdir(anchor_dir())
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(anchor_dir(), name), encoding="utf-8") as fh:
                rec = json.load(fh)
            if isinstance(rec, dict):
                out.append(rec)
        except (OSError, ValueError):
            continue
    return out


def load_by_user(user_hash: str) -> Optional[Dict[str, Any]]:
    """Most recent anchor record for an identity hash, or None."""
    key = (user_hash or "").strip().lower()
    if not key:
        return None
    hits = [r for r in _all_records()
            if str(r.get("user_hash", "")).lower() == key]
    if not hits:
        return None
    hits.sort(key=lambda r: str(r.get("anchored_at", "")), reverse=True)
    return hits[0]


def history_for_user(user_hash: str) -> List[Dict[str, Any]]:
    """All anchors for an identity, newest first."""
    key = (user_hash or "").strip().lower()
    hits = [r for r in _all_records()
            if str(r.get("user_hash", "")).lower() == key]
    hits.sort(key=lambda r: str(r.get("anchored_at", "")), reverse=True)
    return hits


def count() -> int:
    return len(_all_records())
