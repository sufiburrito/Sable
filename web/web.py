"""
TradeCentral Web UI — FastAPI server.

Run:  python3 web/web.py
Open: http://localhost:8080
"""
import sys
from pathlib import Path

# Add project root to path so alert_bot imports work
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import uvicorn

from web.api.stocks import router as stocks_router
from web.api.prices import router as prices_router
from web.api.alerts import router as alerts_router
from web.api.mmi import router as mmi_router
from web.api.simulation import router as simulation_router
from web.api.regime import router as regime_router
from web.api.portfolio import router as portfolio_router
from web.api.smartmoney import router as smartmoney_router

app = FastAPI(title="TradeCentral", version="0.1.0")

# Register API routers
app.include_router(stocks_router)
app.include_router(prices_router)
app.include_router(alerts_router)
app.include_router(mmi_router)
app.include_router(simulation_router)
app.include_router(regime_router)
app.include_router(portfolio_router)
app.include_router(smartmoney_router)

# Serve static files (CSS, JS)
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/")
def index():
    """Serve the main HTML page."""
    return FileResponse(static_dir / "index.html")


if __name__ == "__main__":
    uvicorn.run(
        "web.web:app",
        host="0.0.0.0",
        port=8081,
        reload=True,
        reload_dirs=[str(Path(__file__).parent)],
    )
