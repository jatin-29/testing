"""
Langfuse prompt registry + fetch helpers.

Single place for:
  1. Exact Langfuse prompt names (must match the Langfuse UI)
  2. Local .md fallback paths
  3. All methods that fetch / fill prompts

Pipeline phases MUST use this module — do not hardcode prompt names elsewhere.
"""

import logging
import os
from string import Template
from typing import Any, Dict, NamedTuple, Optional, Tuple

from app.core.config import (
    ANSWERER_PROMPT_FILE,
    EXTRACTOR_PROMPT_FILE,
    SPLITTER_PROMPT_FILE,
)
from app.pipeline.helpers import fill_template, load_template

logger = logging.getLogger("app.langfuse_prompts")

LANGFUSE_LABEL = os.getenv("CHANAKYA_GURU_LANGFUSE_LABEL", "stage")

# ---------------------------------------------------------------------------
# 1. Exact Langfuse prompt names (match Langfuse registry)
# ---------------------------------------------------------------------------

PROMPT_QRC = "QRC - Relevance and Chunking"
PROMPT_QPE = "QPE - Question Paper Extraction"
PROMPT_QAG = "QAG - Answer Generation"

# Phase key → Langfuse name (source of truth for the pipeline)
PROMPT_NAME_BY_PHASE: Dict[str, str] = {
    "relevance_and_chunking": PROMPT_QRC,
    "question_extraction": PROMPT_QPE,
    "answer_generation": PROMPT_QAG,
}


class PhasePromptSpec(NamedTuple):
    """One pipeline phase prompt: Langfuse name + local fallback file."""

    phase_key: str
    langfuse_name: str
    fallback_path: str


PHASE_RELEVANCE = PhasePromptSpec(
    phase_key="relevance_and_chunking",
    langfuse_name=PROMPT_QRC,
    fallback_path=SPLITTER_PROMPT_FILE,
)
PHASE_QUESTION_EXTRACTION = PhasePromptSpec(
    phase_key="question_extraction",
    langfuse_name=PROMPT_QPE,
    fallback_path=EXTRACTOR_PROMPT_FILE,
)
PHASE_ANSWER_GENERATION = PhasePromptSpec(
    phase_key="answer_generation",
    langfuse_name=PROMPT_QAG,
    fallback_path=ANSWERER_PROMPT_FILE,
)

PHASE_PROMPTS: Dict[str, PhasePromptSpec] = {
    PHASE_RELEVANCE.phase_key: PHASE_RELEVANCE,
    PHASE_QUESTION_EXTRACTION.phase_key: PHASE_QUESTION_EXTRACTION,
    PHASE_ANSWER_GENERATION.phase_key: PHASE_ANSWER_GENERATION,
}


# ---------------------------------------------------------------------------
# 2. Fetch methods
# ---------------------------------------------------------------------------

def get_langfuse_for_substitute(
    prompt_name: str, label: str = LANGFUSE_LABEL
) -> Tuple[Optional[Template], Any]:
    """
    Low-level fetch: Langfuse get_prompt → get_langchain_prompt → Template.
    Returns (Template, langfuse_prompt_obj) or (None, None).
    """
    from app.pipeline.langfuse_client import get_langfuse_client

    try:
        langfuse = get_langfuse_client()
        langfuse_prompt_obj = langfuse.get_prompt(name=str(prompt_name), label=label)
        prompt_with_data = langfuse_prompt_obj.get_langchain_prompt()
        if isinstance(prompt_with_data, list):
            parts = []
            for msg in prompt_with_data:
                if isinstance(msg, tuple) and len(msg) >= 2:
                    parts.append(str(msg[1]))
                else:
                    parts.append(str(msg))
            prompt_with_data = "\n".join(parts)
        return Template(str(prompt_with_data)), langfuse_prompt_obj
    except Exception as e:
        logger.warning(
            f"Could not fetch prompt '{prompt_name}' (label={label}) from Langfuse: {e}"
        )
        return None, None


def get_base_prompt_from_langfuse(
    *, prompt_name: str, label: Optional[str] = LANGFUSE_LABEL, **kwargs
) -> Tuple[str, Any]:
    """
    Chanakya Guru base helper: Langfuse prompt + Template.safe_substitute.
    Raises if the prompt cannot be fetched. Prefer resolve_prompt for phases.
    """
    prompt_template, langfuse_prompt_obj = get_langfuse_for_substitute(
        prompt_name, label=label
    )
    if prompt_template is None:
        prompt_template, langfuse_prompt_obj = get_langfuse_for_substitute(
            prompt_name, label=LANGFUSE_LABEL
        )
        if prompt_template is None:
            raise Exception(
                f"Prompt template not found in Langfuse for prompt: {prompt_name} "
                f"and label: {label}"
            )
    prompt = prompt_template.safe_substitute(**kwargs)
    try:
        return prompt.replace("Human:", "").strip(), langfuse_prompt_obj
    except Exception:
        return prompt, langfuse_prompt_obj


def resolve_prompt(
    prompt_name: str, values: dict, fallback_path: str
) -> Tuple[str, Any]:
    """
    Fetch by exact Langfuse name, fill with fill_template (supports list/dict JSON).
    Returns (filled_prompt, langfuse_prompt_obj).
    Falls back to local .md if Langfuse fetch fails (obj=None).
    """
    prompt_template, langfuse_prompt_obj = get_langfuse_for_substitute(prompt_name)
    if prompt_template is not None:
        filled = fill_template(prompt_template.template, values)
        try:
            filled = filled.replace("Human:", "").strip()
        except Exception:
            pass
        logger.info(
            f"Loaded Langfuse prompt '{prompt_name}' "
            f"(label={LANGFUSE_LABEL}, filled_chars={len(filled)})"
        )
        return filled, langfuse_prompt_obj

    logger.warning(
        f"Langfuse prompt '{prompt_name}' unavailable — falling back to {fallback_path}"
    )
    return fill_template(load_template(fallback_path), values), None


def resolve_phase_prompt(spec: PhasePromptSpec, values: dict) -> Tuple[str, Any]:
    """Pipeline entry: resolve by PhasePromptSpec (correct Langfuse name + fallback)."""
    return resolve_prompt(spec.langfuse_name, values, spec.fallback_path)
