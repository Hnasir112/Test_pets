"""
GCC Open Banking Underwriting API

Entry point for the FastAPI application.
Run with: uvicorn app.main:app --reload
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes import assessments

app = FastAPI(
    title="GCC Open Banking Underwriting API",
    description=(
        "SME credit scoring middleware for GCC commercial banks. "
        "Submit categorized bank transactions and receive a full underwriting "
        "scorecard — DSCR, revenue volatility, trend analysis, burn rate, "
        "risk score, and anomaly flags — in under 2 seconds."
    ),
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.include_router(assessments.router)


@app.get("/health", tags=["system"])
def health():
    return {"status": "ok", "version": "0.1.0"}


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )
