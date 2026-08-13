"""Ibex unified dashboard router.

Drop this in as  serve/ibex_ui.py  and mount it from serve/app.py.

It adds three things and changes nothing that already works:

    GET  /dashboard         the single-page dashboard
    GET  /api/ui/session    the current TrueLayer token, so nobody copy-pastes it
    GET  /api/ui/diag       what this module can see inside serve.app, for debugging

Why /api/ui/session exists
--------------------------
After the TrueLayer redirect, serve/app.py sends the browser to  /?tl=<token>.
That token lives in a module-level dict inside serve.app. Rather than making the
user copy it out of the address bar, this endpoint reaches into serve.app at call
time and hands the token to the dashboard directly.

It does NOT assume the dict is called _CONNECTED or that entries have any
particular shape. It scans serve.app's module globals for dictionaries, then
looks for the most plausible token string inside them. If it finds nothing it
says so plainly instead of failing silently, and /api/ui/diag will show you
exactly what it did see.

The import of serve.app is deliberately done inside the function. Importing at
module scope would be circular, because serve/app.py imports this file.
"""

import os
import time

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

router = APIRouter()

_HERE = os.path.dirname(os.path.abspath(__file__))
_DASHBOARD = os.path.join(_HERE, "static", "dashboard.html")

# Names we hope to find. Checked first, but not required.
_LIKELY_DICTS = ("_CONNECTED", "_SESSIONS", "_TOKENS", "_PENDING")

# A TrueLayer session handle is a longish opaque string. Anything shorter than
# this is far more likely to be a case id, a profile name or a status flag.
_MIN_TOKEN_LEN = 12


def _looks_like_token(value):
    if not isinstance(value, str):
        return False
    if len(value) < _MIN_TOKEN_LEN:
        return False
    # Reject obvious non-tokens.
    lowered = value.lower()
    if lowered.startswith(("http://", "https://", "/")):
        return False
    if " " in value:
        return False
    return True


def _candidate_dicts(app_module):
    """Yield (name, dict) for every module-level dict, likely names first."""
    seen = set()
    for name in _LIKELY_DICTS:
        obj = getattr(app_module, name, None)
        if isinstance(obj, dict):
            seen.add(name)
            yield name, obj
    for name in dir(app_module):
        if name in seen or name.startswith("__"):
            continue
        obj = getattr(app_module, name, None)
        if isinstance(obj, dict) and obj:
            yield name, obj


def _extract_token(container):
    """Pull the most plausible token out of a dict entry.

    Handles three shapes:
        {"<token>": {...}}          token is the key
        {"case": "<token>"}         token is a plain value
        {"case": {"token": "..."}}  token is nested one level down
    """
    for key, value in reversed(list(container.items())):
        if isinstance(value, dict):
            for inner_key in ("token", "tl", "session", "access_token", "id"):
                inner = value.get(inner_key)
                if _looks_like_token(inner):
                    return inner
        if _looks_like_token(value):
            return value
        if _looks_like_token(key):
            return key
    return None


@router.get("/api/ui/session")
def ui_session():
    """Return the current TrueLayer token if the app is holding one."""
    try:
        import serve.app as app_module
    except Exception as exc:  # pragma: no cover - only fires on odd layouts
        return JSONResponse(
            {"connected": False, "reason": "cannot import serve.app: %s" % exc},
            status_code=200,
        )

    for name, container in _candidate_dicts(app_module):
        token = _extract_token(container)
        if token:
            return {
                "connected": True,
                "token": token,
                "source": name,
                "entries": len(container),
                "checked_at": time.time(),
            }

    return {
        "connected": False,
        "reason": "no TrueLayer session found - connect a bank first",
    }


@router.get("/api/ui/diag")
def ui_diag():
    """Show what this module can see. Use when /api/ui/session finds nothing."""
    try:
        import serve.app as app_module
    except Exception as exc:
        return {"error": "cannot import serve.app: %s" % exc}

    found = []
    for name, container in _candidate_dicts(app_module):
        found.append(
            {
                "name": name,
                "entries": len(container),
                "keys": [str(k)[:40] for k in list(container.keys())[:5]],
                "value_types": sorted(
                    {type(v).__name__ for v in list(container.values())[:5]}
                ),
            }
        )
    return {
        "dashboard_file": _DASHBOARD,
        "dashboard_exists": os.path.exists(_DASHBOARD),
        "score_event_path": os.environ.get("IBEX_SCORE_EVENT_PATH", "(not set)"),
        "dicts": found,
    }


@router.get("/dashboard")
def dashboard_page():
    if not os.path.exists(_DASHBOARD):
        return JSONResponse(
            {"error": "dashboard.html not found", "expected_at": _DASHBOARD},
            status_code=404,
        )
    return FileResponse(_DASHBOARD)
