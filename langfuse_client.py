"""
Langfuse infrastructure layer.

LAYERING (do not call SDK directly from phases / mistral):
  1. Client        — singleton Langfuse SDK
  2. Job context   — one parent trace per request_id (start / end / get)
  3. Observations  — nest generations under the current job trace

Prompt names + fetch live in langfuse_prompts.py (separate module).

SINGLE-TRACE CONTRACT:
  - Exactly ONE langfuse.trace() per /upload-paper/ request
  - id = session_id = payload request_id
  - user_id = payload user_name
  - Every LLM step only nests a generation (never another root trace)
"""

from __future__ import annotations

import logging
import os
import re
import threading
from datetime import datetime
from typing import Any, Optional, Tuple

from dotenv import load_dotenv
from langfuse import Langfuse

from app.core.config import (
    MISTRAL_INPUT_PRICE_PER_M_TOKENS,
    MISTRAL_OUTPUT_PRICE_PER_M_TOKENS,
)

load_dotenv()

logger = logging.getLogger("app.langfuse_client")

# ---------------------------------------------------------------------------
# 1. Client singleton
# ---------------------------------------------------------------------------

_langfuse_client: Optional[Langfuse] = None


def get_langfuse_client() -> Langfuse:
    """Public accessor for SDK singleton (used by langfuse_prompts)."""
    global _langfuse_client
    if _langfuse_client is None:
        _langfuse_client = Langfuse(
            secret_key=os.getenv("CHANAKYA_GURU_LANGFUSE_SECRET_KEY"),
            public_key=os.getenv("CHANAKYA_GURU_LANGFUSE_PUBLIC_KEY"),
            host=os.getenv("CHANAKYA_GURU_LANGFUSE_HOST"),
        )
        logger.info("Langfuse client initialized.")
    return _langfuse_client


# Keep private alias for internal use
_get_langfuse_client = get_langfuse_client


# ---------------------------------------------------------------------------
# 2. Job context — one parent trace keyed by request_id
# ---------------------------------------------------------------------------

_ctx_lock = threading.Lock()
_current = {"trace": None, "request_id": None, "user_name": None}


def get_current_trace():
    with _ctx_lock:
        return _current["trace"]


def get_current_request_context() -> tuple:
    with _ctx_lock:
        return _current["request_id"], _current["user_name"]


# ---------------------------------------------------------------------------
# 3. Tracer facade — create root once; nest generations only
# ---------------------------------------------------------------------------

class LangfuseTracer:
    def __init__(self):
        self.langfuse = self._initialize_langfuse()
        self.tags = os.environ.get("CHANAKYA_GURU_LANGFUSE_TAGS", "stage")

    def _initialize_langfuse(self):
        try:
            return _get_langfuse_client()
        except Exception as e:
            logger.error(f"Error while initializing Langfuse: {e}")
            return None

    def trace(self, name, user_email, request_id=None, **kwargs):
        """THE only place that creates a Langfuse root trace."""
        if not request_id:
            logger.error("Langfuse.trace requires request_id — skipping.")
            return None
        if not self.langfuse:
            logger.warning("Langfuse is not initialized. Skipping trace creation.")
            return None
        try:
            trace = self.langfuse.trace(
                name=name,
                user_id=user_email,
                id=request_id,
                timestamp=datetime.now(),
                session_id=request_id,
                tags=[self.tags],
            )
            for key, value in kwargs.items():
                trace.update(**{key: value})
            logger.info(f"Langfuse parent trace created for request_id={request_id}")
            return trace
        except Exception as e:
            logger.error(f"Error while creating trace: {e}")
            return None

    def generation(
        self,
        trace,
        name: str,
        start_time,
        end_time,
        model: str,
        input_prompt,
        usage: dict = None,
        output=None,
        metadata: dict = None,
        prompt=None,
    ):
        """Nest one LLM step under the existing parent. Never creates a root trace."""
        if not trace:
            logger.warning(
                "No parent Langfuse trace for this request_id — skipping generation."
            )
            return None
        try:
            md = dict(metadata or {})
            request_id, _ = get_current_request_context()
            if request_id:
                md["request_id"] = request_id
            trace.generation(
                name=name,
                start_time=start_time,
                end_time=end_time,
                model=model,
                input=input_prompt,
                prompt=prompt,
                usage=usage,
                output=output,
                metadata=md,
            )
        except Exception as e:
            logger.error(f"Error while logging Langfuse generation: {e}")


langfuse_tracer = LangfuseTracer()


def start_job_trace(
    trace_name: str,
    request_id: str,
    user_name: str,
    metadata: dict = None,
) -> None:
    """Orchestrator entry: create the single parent trace for this upload."""
    if not request_id:
        raise ValueError("request_id from payload is required for Langfuse tracing.")

    with _ctx_lock:
        if _current["trace"] is not None:
            logger.warning(
                f"Parent Langfuse trace already exists for "
                f"request_id={_current['request_id']} — not creating another."
            )
            return

    md = dict(metadata or {})
    md["request_id"] = request_id
    trace = langfuse_tracer.trace(
        name=trace_name,
        user_email=user_name,
        request_id=request_id,
        metadata=md,
    )
    with _ctx_lock:
        _current["trace"] = trace
        _current["request_id"] = request_id
        _current["user_name"] = user_name


def end_job_trace(status: str = "done") -> None:
    """Orchestrator exit: update status on the same parent and flush."""
    with _ctx_lock:
        trace = _current["trace"]
        request_id = _current["request_id"]
        _current["trace"] = None
        _current["request_id"] = None
        _current["user_name"] = None
    try:
        if trace:
            trace.update(metadata={"job_status": status, "request_id": request_id})
        if langfuse_tracer.langfuse:
            langfuse_tracer.langfuse.flush()
        logger.info(
            f"Langfuse parent trace finalized request_id={request_id} status={status}"
        )
    except Exception as e:
        logger.error(f"Error finalizing Langfuse trace: {e}")


# ---------------------------------------------------------------------------
# 4. Observation naming + LLM generation logging (used by mistral_client only)
# ---------------------------------------------------------------------------

_LABEL_RE = re.compile(
    r"^Phase\s*\d+\s*-\s*(?:Chunk\s*(\d+)\s*-\s*)?"
    r"(.+?)(?:-Repair)?(?:\s+batch\s+(\d+)/\d+)?$",
    re.I,
)

_STEP_RENAMES = {
    "Relevance and Chunking": "Relevance & Chunking",
    "Question Extraction": "Question Extraction",
    "Answer Generation": "Answer Generation",
    "Question Extraction Repair": "Question Extraction Repair",
}


def observation_name_from_label(label: str) -> Tuple[str, dict]:
    """
    Map a pipeline call label to a Langfuse generation name + step metadata.
      Question Extraction - Chunk 1
      Answer Generation - Chunk 2 - Batch 1
    """
    raw = label.strip()
    m = _LABEL_RE.match(raw)
    if not m:
        return label, {}
    chunk, name, batch = m.group(1), m.group(2).strip(), m.group(3)
    md: dict = {}
    if chunk:
        md["chunk"] = int(chunk)
    if batch:
        md["batch"] = int(batch)
    if re.search(r"-Repair", raw, re.I):
        md["repair_call"] = True
        if not name.endswith(" Repair"):
            name = f"{name} Repair"
    step = _STEP_RENAMES.get(name, name)
    parts = [step]
    if chunk:
        parts.append(f"Chunk {chunk}")
    if batch:
        parts.append(f"Batch {batch}")
    return " - ".join(parts), md


def _usage_metadata(usage: dict) -> Tuple[dict, dict]:
    """Build Langfuse usage dict + cost metadata from raw provider usage."""
    prompt_tokens = usage.get("prompt_tokens") or 0
    completion_tokens = usage.get("completion_tokens") or 0
    total_tokens = usage.get("total_tokens") or (prompt_tokens + completion_tokens)
    estimated_cost_usd = round(
        (prompt_tokens / 1_000_000) * MISTRAL_INPUT_PRICE_PER_M_TOKENS
        + (completion_tokens / 1_000_000) * MISTRAL_OUTPUT_PRICE_PER_M_TOKENS,
        6,
    )
    langfuse_usage = {
        "promptTokens": prompt_tokens,
        "completionTokens": completion_tokens,
        "totalTokens": total_tokens,
    }
    cost_md = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "estimated_cost_usd": estimated_cost_usd,
    }
    return langfuse_usage, cost_md


def log_llm_generation(
    *,
    label: str,
    start_time,
    end_time,
    model: str,
    input_messages,
    output,
    usage: dict,
    langfuse_prompt=None,
) -> dict:
    """
    Single entry for LLM observability.
    Nested under the current job trace; links Langfuse prompt; records tokens + price.
    Returns cost metadata (for logging in the caller).
    """
    step_name, step_md = observation_name_from_label(label)
    langfuse_usage, cost_md = _usage_metadata(usage or {})
    langfuse_tracer.generation(
        get_current_trace(),
        name=step_name,
        start_time=start_time,
        end_time=end_time,
        model=model,
        input_prompt=input_messages,
        usage=langfuse_usage,
        output=output,
        prompt=langfuse_prompt,
        metadata={
            "service": "question-paper-extraction",
            **cost_md,
            **step_md,
        },
    )
    return cost_md
