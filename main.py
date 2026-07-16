"""
FastAPI Paper Extraction Service — entry point.

Run with:
    uvicorn main:app --host 0.0.0.0 --port 8000
"""

import logging
from fastapi import FastAPI

from app.api.routes import router
from app.db.database import create_tables

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)

app = FastAPI(
    title="Paper Extraction Service",
    description="Converts question-paper PDFs into structured exam JSON via Mistral.",
    version="1.0.0",
)


@app.get("/health/")
def health_check():
    return {"status": "ok"}


@app.on_event("startup")
def _startup():
    create_tables()
    logging.getLogger("app").info("Startup complete — database ready.")


app.include_router(router)