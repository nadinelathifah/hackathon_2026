"""Append-only score history. No database.

Every scoring run appends one JSON line to a file. Reading it back gives the
user their previous scores for the credential page.

Why a file and not a database: it needs no service, no schema migration and
no ops. One JSON object per line means a partial write can only ever lose the
last line rather than corrupt the whole store, and the file is trivially
inspectable during a viva.

Path comes from IBEX_SCORE_HISTORY_PATH, defaulting to score_history.jsonl in
the project root. Keep it out of Git: it contains real scores.
"""
from __future__ import annotations
import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

DEFAULT_PATH = os.path.join(_ROOT, "score_history.jsonl")
_LOCK = threading.Lock()


def history_path() -> str:
    """Resolved store location."""
    return os.environ.get("IBEX_SCORE_HISTORY_PATH", "") or DEFAULT_PATH


def record_score(key: str,
                 score: float,
                 band: Optional[str] = None,
                 pd: Optional[float] = None,
                 handle: Optional[str] = None,
                 extra: Optional[Dict[str, Any]] = None) -> Optional[Dict]:
    """Append one scoring event, keyed by the user's email.

    Never raises. A history write failing must not turn a successful score
    into a 500 for the person who just waited for it.
    """
    if not key:
        return None
    try:
        row: Dict[str, Any] = {
            "key": str(key).strip().lower(),
            "score": round(float(score), 1),
            "band": band,
            "pd": (round(float(pd), 6) if pd is not None else None),
            "handle": handle,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception:
        return None
    if isinstance(extra, dict):
        for k, v in extra.items():
            row.setdefault(k, v)
    path = history_path()
    try:
        with _LOCK:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
    except Exception:
        return None
    return row


def read_history(key: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Newest first, with a delta against the next-oldest entry.

    Tolerates truncated or corrupt lines rather than failing the page.
    """
    path = history_path()
    if not key or not os.path.exists(path):
        return []
    want = str(key).strip().lower()
    rows: List[Dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if isinstance(row, dict) and str(row.get("key", "")).lower() == want:
                    rows.append(row)
    except Exception:
        return []
    rows.sort(key=lambda r: str(r.get("timestamp") or ""), reverse=True)
    try:
        n = max(1, int(limit))
    except Exception:
        n = 10
    rows = rows[:n]
    for i, r in enumerate(rows):
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        a, b = r.get("score"), (nxt or {}).get("score")
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            r["delta"] = round(float(a) - float(b), 1)
        else:
            r["delta"] = None
    return rows
