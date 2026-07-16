"""
SQLite database layer for the paper extraction job queue.

Every incoming /upload-paper/ request is persisted here so that:
- Job status can be polled at any time (survives server restarts)
- Duplicate exam submissions are visible
- Pipeline step progress is traceable (status transitions)

All helper functions open and close their own connection so they are
safe to call from FastAPI BackgroundTasks (which run in threads).
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.core.config import DATA_DIR

DB_PATH = DATA_DIR / "jobs.db"


def _get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def create_tables() -> None:
    """
    Create the jobs table if it does not exist.
    Call once at application startup.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = _get_connection()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id          TEXT PRIMARY KEY,
                exam_id         TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'processing',
                request_payload TEXT NOT NULL,
                result          TEXT,
                error           TEXT,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_jobs_exam_id ON jobs (exam_id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status)
        """)
        conn.commit()
    finally:
        conn.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def insert_job(job_id: str, exam_id: str, request_payload: dict) -> None:
    """
    Insert a new job row with status='processing'.
    Stores the entire incoming request payload as a JSON blob.
    """
    now = _now_iso()
    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO jobs (job_id, exam_id, status, request_payload, created_at, updated_at)
            VALUES (?, ?, 'processing', ?, ?, ?)
            """,
            (job_id, exam_id, json.dumps(request_payload, ensure_ascii=False), now, now),
        )
        conn.commit()
    finally:
        conn.close()


def update_job_status(job_id: str, status: str) -> None:
    """
    Update the status of an in-progress job to an intermediate step label.
    Valid intermediate values: 'ocr', 'uploading_images', 'extracting', 'saving'
    """
    conn = _get_connection()
    try:
        conn.execute(
            "UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?",
            (status, _now_iso(), job_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_job_done(job_id: str, result: dict) -> None:
    """
    Mark a job as successfully completed and store the API response payload.
    """
    conn = _get_connection()
    try:
        conn.execute(
            """
            UPDATE jobs
            SET status = 'done',
                result = ?,
                updated_at = ?
            WHERE job_id = ?
            """,
            (json.dumps(result, ensure_ascii=False), _now_iso(), job_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_job_failed(job_id: str, error_msg: str) -> None:
    """
    Mark a job as failed and store the error message.
    """
    conn = _get_connection()
    try:
        conn.execute(
            """
            UPDATE jobs
            SET status = 'failed',
                error  = ?,
                updated_at = ?
            WHERE job_id = ?
            """,
            (error_msg, _now_iso(), job_id),
        )
        conn.commit()
    finally:
        conn.close()

#converting text data back to dict
def get_job(job_id: str) -> Optional[dict]:
    """
    Return a single job row as a plain dict, or None if not found.
    """
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            return None
        job = dict(row)
        if job.get("request_payload"):
            job["request_payload"] = json.loads(job["request_payload"])
        if job.get("result"):
            job["result"] = json.loads(job["result"])
        return job
    finally:
        conn.close()
