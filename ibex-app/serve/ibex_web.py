"""Ibex marketing landing page.

Purely additive. Serves three plain files -- HTML, CSS, JS -- with no build
step and no templating, so a front-end person can edit them without touching
any Python, and without any risk to scoring, calibration or the chain code.

Routes
    GET /home            the landing page
    GET /web/styles.css  stylesheet
    GET /web/app.js      behaviour

Edit  serve/web/index.html, serve/web/styles.css, serve/web/app.js
and refresh the browser. No restart needed for those three files.
"""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter()

WEB_DIR = Path(__file__).resolve().parent / "web"

_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}

# no-store keeps designers from chasing ghosts while iterating on CSS
_NO_CACHE = {"Cache-Control": "no-store, max-age=0"}


def _serve(name: str) -> FileResponse:
    target = (WEB_DIR / name).resolve()
    # containment check: never serve outside serve/web
    if not str(target).startswith(str(WEB_DIR.resolve())):
        raise HTTPException(status_code=404, detail="not found")
    if not target.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"{name} not found -- expected at {target}",
        )
    media = _TYPES.get(target.suffix.lower(), "application/octet-stream")
    return FileResponse(target, media_type=media, headers=_NO_CACHE)


@router.get("/home", include_in_schema=False)
def landing() -> FileResponse:
    """Public marketing page. Sign-in button points at /ibex."""
    return _serve("index.html")


@router.get("/web/{asset:path}", include_in_schema=False)
def asset(asset: str) -> FileResponse:
    """Static assets for the landing page.

    Anything dropped into serve/web is served automatically -- images, an
    extra stylesheet, a logo -- with no Python change required.
    """
    return _serve(asset)
