"""
Central configuration — constants, file paths, and environment-variable keys.
All modules import from here; nothing is hardcoded elsewhere.
"""

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT_DIR = Path(__file__).parent.parent.parent  # project root
APP_DIR = Path(__file__).parent.parent  # app/
DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT_DIR / "data")))
PROMPTS_DIR = APP_DIR / "pipeline" / "prompts"

# Local prompt fallbacks (used only when Langfuse fetch fails)
SPLITTER_PROMPT_FILE = str(PROMPTS_DIR / "relevance_and_chunking.md")
EXTRACTOR_PROMPT_FILE = str(PROMPTS_DIR / "question_extractor.md")
ANSWERER_PROMPT_FILE = str(PROMPTS_DIR / "answer_generator.md")

# Langfuse prompt names live in app.pipeline.langfuse_prompts (single source of truth)

# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

MODEL = "mistral-large-latest"
TEMPERATURE = 0.0
MAX_TOKENS = 16000

# ---------------------------------------------------------------------------
# Pipeline batching / parallelism
# ---------------------------------------------------------------------------

MAX_QUESTIONS_PER_CHUNK = 10
MAX_QUESTIONS_PER_ANSWER_CALL = 2  # Phase 3: questions per answer-generation call
MAX_PARALLEL_CHUNK_CALLS = 3

# ---------------------------------------------------------------------------
# Retries / jitter
# ---------------------------------------------------------------------------

RATE_LIMIT_MAX_RETRIES = 6
RATE_LIMIT_BASE_DELAY = 2.0
RATE_LIMIT_MAX_DELAY = 60.0
INTER_CALL_JITTER = (0.3, 1.0)
JSON_PARSE_MAX_ATTEMPTS = 5  # re-call LLM when response is invalid JSON
JSON_RETRY_TEMPERATURE = 0.2  # slight entropy so retries are not identical

# ---------------------------------------------------------------------------
# Domain defaults
# ---------------------------------------------------------------------------

DIFFICULTY_TO_MARKS_FALLBACK = {"Easy": 1, "Medium": 2, "Hard": 3}

MARKS_BREAKDOWN_ENABLED_TEXT = (
    "ENABLED — MANDATORY. For every SUBJECTIVE question or sub-part/alternative "
    "with marks >= 2, you MUST include a marking_scheme field inside its answer_data."
)

MARKS_BREAKDOWN_DISABLED_TEXT = (
    "DISABLED — Do NOT include a marking_scheme field in any answer_data object."
)

# ---------------------------------------------------------------------------
# Downstream API + pricing
# ---------------------------------------------------------------------------

CREATE_PAPER_URL = "https://stageapi.aichanakya.in/v0/edu-multiagent/create-paper/"

MISTRAL_INPUT_PRICE_PER_M_TOKENS = 0.50   # Mistral Large 3 (correct current pricing)
MISTRAL_OUTPUT_PRICE_PER_M_TOKENS = 1.50
