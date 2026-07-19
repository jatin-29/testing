"""
Phase 3 — Answer generation helpers (batching, prompts, LLM calls).

Celery orchestration lives in phase_orchestration.py (avoids circular imports).

COST REWORK:
- Image URL mapping is NO LONGER sent to (or done by) the LLM. It is now a
  deterministic post-merge step in postprocess.fill_image_urls(). This both
  cuts prompt tokens and eliminates wrong/duplicate image assignments.
- Questions sent to Phase 3 are TRIMMED to only the fields needed to answer:
  question_number, marks, question_type, question_text, options (text only),
  additional_questions / child_additional_questions (same trimmed shape).
"""

import json
import logging

from app.core.config import (
    ANSWER_BATCH_SIZE_BY_TYPE,
    MARKS_BREAKDOWN_DISABLED_TEXT,
)
from app.pipeline.langfuse_prompts import PHASE_ANSWER_GENERATION, resolve_phase_prompt
from app.pipeline.mistral_client import call_mistral_with_retries

logger = logging.getLogger("app.answer_generator")

ANSWERER_SYSTEM_PROMPT = (
    "You are an expert K-12 answer generator. Generate answers matching the structured "
    "input schema. Never output 'sub_answers'. Map subpart answers into the 'additional_answers' "
    "array with additional_answer_type='SUB_QUESTION'. All mathematical notation MUST be inside "
    "$...$ or $$...$$ delimiters. Never use \\cosec (use \\csc). Never convert (R) into the "
    "registered-trademark symbol."
)


def _slim_option(opt: dict) -> dict:
    return {
        "option_prefix": opt.get("option_prefix"),
        "option_text": opt.get("option_text"),
    }


def _slim_nested(entry: dict) -> dict:
    slim = {
        "question_prefix": entry.get("question_prefix"),
        "additional_question_type": entry.get("additional_question_type"),
        "question_type": entry.get("question_type"),
        "marks": entry.get("marks"),
        "question_text": entry.get("question_text"),
    }
    if entry.get("options"):
        slim["options"] = [_slim_option(o) for o in entry["options"]]
    children = entry.get("child_additional_questions") or []
    if children:
        slim["child_additional_questions"] = [_slim_nested(c) for c in children]
    return slim


def slim_questions_for_answering(questions: list) -> list:
    """Strip everything Phase 3 does not need (question_image placeholders,
    difficulty, chapter_name, is_correct flags...). ~25-35% prompt-token cut."""
    slimmed = []
    for q in questions:
        q_data = q.get("question_data", {}) or {}
        entry = {
            "question_number": q.get("question_number"),
            "question_type": q.get("question_type"),
            "marks": q.get("marks"),
            "question_text": q_data.get("question_text"),
        }
        if q_data.get("options"):
            entry["options"] = [_slim_option(o) for o in q_data["options"]]
        add_qs = q_data.get("additional_questions") or []
        if add_qs:
            entry["additional_questions"] = [_slim_nested(a) for a in add_qs]
        slimmed.append(entry)
    return slimmed


def build_answerer_prompt(chunk_questions: list, config: dict, chunk_index) -> tuple:
    values = {
        "board_name": config["board_name"],
        "grade": config["grade"],
        "subject": config["subject"],
        "topic": config["topic"],
        "chapter_descriptions": config["chapter_descriptions"],
        # Marks breakdown (marking_scheme) is PERMANENTLY disabled per team
        # decision — always send the disabled instruction regardless of the
        # incoming request's include_marks_breakdown flag, so old API callers
        # that still pass this field don't break; it's just ignored now.
        "marks_breakdown_instructions": MARKS_BREAKDOWN_DISABLED_TEXT,
        "chunk_questions": slim_questions_for_answering(chunk_questions),
        # image_url_mapping intentionally removed — handled in postprocess now.
    }
    return resolve_phase_prompt(PHASE_ANSWER_GENERATION, values)


def _group_questions_by_type(questions: list) -> dict:
    """
    Groups root questions by their question_type, preserving each group's
    original relative order. Downstream merging matches answers back to
    questions by question_number (see postprocess.zip_answers_into_questions),
    NOT by list position, so re-ordering into type-groups here is safe.
    """
    groups: dict = {}
    for q in questions:
        qtype = q.get("question_type", "SA")  # SA is a reasonable fallback
        groups.setdefault(qtype, []).append(q)
    return groups


def build_answer_batches(questions: list) -> list[dict]:
    """
    Split questions into type-based batches using ANSWER_BATCH_SIZE_BY_TYPE.
    Each item: {qtype, batch_num, num_batches, questions}.
    """
    type_groups = _group_questions_by_type(questions)
    batches: list[dict] = []
    for qtype, type_questions in type_groups.items():
        batch_size = ANSWER_BATCH_SIZE_BY_TYPE.get(
            qtype, ANSWER_BATCH_SIZE_BY_TYPE["_default"]
        )
        total = len(type_questions)
        num_batches = (total + batch_size - 1) // batch_size
        for batch_num, start in enumerate(range(0, total, batch_size), start=1):
            batches.append(
                {
                    "qtype": qtype,
                    "batch_num": batch_num,
                    "num_batches": num_batches,
                    "questions": type_questions[start : start + batch_size],
                }
            )
    return batches


def generate_answers_for_batch(
    questions_batch: list,
    config: dict,
    chunk_index,
    qtype: str,
    batch_num: int,
    num_batches: int,
) -> list:
    """Run one Mistral answer-generation call for a single type-batch."""
    label = (
        f"Phase 3 - Chunk {chunk_index} - Answer Generation "
        f"[{qtype}] batch {batch_num}/{num_batches}"
    )
    logger.info(
        f"[{label}] Sending {len(questions_batch)} question(s) "
        f"(batch_size={len(questions_batch)})."
    )
    prompt, langfuse_prompt = build_answerer_prompt(
        questions_batch, config, chunk_index
    )
    logger.info(
        f"[{label}] Answer prompt ready — chars={len(prompt)}, "
        f"from_langfuse={langfuse_prompt is not None}, "
        f"langfuse_name={PHASE_ANSWER_GENERATION.langfuse_name!r}"
    )
    answer_list = call_mistral_with_retries(
        prompt,
        ANSWERER_SYSTEM_PROMPT,
        label,
        expect_array=True,
        langfuse_prompt=langfuse_prompt,
    )
    answer_list = normalize_answer_list(answer_list)
    logger.info(f"[{label}] Got {len(answer_list)} answer(s).")
    return answer_list


def normalize_answer_list(raw) -> list[dict]:
    """Coerce LLM / Celery payloads into a list of answer dicts only."""
    if raw is None:
        return []

    if isinstance(raw, dict):
        if isinstance(raw.get("answers"), list):
            return normalize_answer_list(raw["answers"])
        if "question_number" in raw or "answer_data" in raw:
            return [raw]
        return []

    if isinstance(raw, str):
        # Rare: JSON returned as a single string — try to parse once.
        try:
            return normalize_answer_list(json.loads(raw))
        except Exception:
            logger.warning("Skipping non-JSON string answer payload.")
            return []

    if not isinstance(raw, list):
        logger.warning(f"Skipping unexpected answer payload type: {type(raw).__name__}")
        return []

    out: list[dict] = []
    for item in raw:
        if isinstance(item, dict) and (
            "question_number" in item or "answer_data" in item
        ):
            out.append(item)
        elif isinstance(item, str):
            try:
                parsed = json.loads(item)
            except Exception:
                logger.warning("Skipping non-dict answer list item (string).")
                continue
            out.extend(normalize_answer_list(parsed if isinstance(parsed, list) else [parsed]))
        else:
            logger.warning(
                f"Skipping non-dict answer list item ({type(item).__name__})."
            )
    return out
