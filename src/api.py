"""Thin FastAPI wrapper around opportunity_view.build_scan_result().

No detection or shaping logic lives here -- see opportunity_view.py for
that. This file only wires HTTP routes and serves the static frontend.
Frontend and API share one origin (same process, same port), so no CORS
middleware is needed.
"""

import sys
from pathlib import Path

# This project's modules use bare, flat imports (from market_matcher import
# ...), which only resolve because python src/main.py auto-adds src/ to
# sys.path[0]. Running this file via uvicorn doesn't do that automatically,
# so it's added explicitly here -- same fix tests/test_slippage.py already
# uses for the identical reason.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from categories import CATEGORY_ORDER  # noqa: E402
from opportunity_view import ScanBusyError, build_category_scan_result, build_scan_result  # noqa: E402

_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(title="Arbitrage Engine for Prediction Markets")


@app.get("/api/opportunities")
def get_opportunities() -> JSONResponse:
    return JSONResponse(build_scan_result())


@app.post("/api/scan/{category}")
def scan_category(category: str) -> JSONResponse:
    """Discovery-mode scan of exactly one category -- slower (~15-25s,
    Kalshi/Polymarket calls only, no paid dependency), explicit-trigger
    only, and fully independent of every other category. Never touches
    the fast/free GET /api/opportunities path above.
    """
    if category not in CATEGORY_ORDER:
        return JSONResponse({"error": f"unknown category: {category}"}, status_code=404)
    try:
        return JSONResponse(build_category_scan_result(category))
    except ScanBusyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=429)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_FRONTEND_DIR / "index.html")


@app.get("/robots.txt")
def robots() -> FileResponse:
    return FileResponse(_FRONTEND_DIR / "robots.txt")


app.mount("/static", StaticFiles(directory=str(_FRONTEND_DIR)), name="static")
