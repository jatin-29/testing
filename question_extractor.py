"""
Phase 2 — Question extraction (no answers).
Logic preserved exactly from demo.py.
"""

import logging
import re

from app.pipeline.langfuse_prompts import PHASE_QUESTION_EXTRACTION, resolve_phase_prompt
from app.pipeline.mistral_client import call_mistral_with_retries

logger = logging.getLogger("app.question_extractor")

EXTRACTOR_SYSTEM_PROMPT = (
    "You are an expert educational content extractor. Extract questions without solving them. "
    "Do NOT include any answer_data keys. Map nested sub-parts directly under the "
    "'additional_questions' array with additional_question_type='SUB_QUESTION'."
)


def build_extractor_prompt(chunk: dict, config: dict) -> tuple:
    values = {
        "board_name": config["board_name"],
        "grade": config["grade"],
        "subject": config["subject"],
        "topic": config["topic"],
        "chapter_descriptions": config["chapter_descriptions"],
        "difficulty_level": config["difficulty_level"],
        "section_name": chunk.get("section_name") or "General",
        "section_priority": chunk["chunk_index"],
        "question_number_range": chunk.get("question_number_range", "unknown"),
        "chunk_raw_text": chunk["raw_text"],
        "available_chapters": [
            entry.get("chapter_name")
            for entry in config.get("chapter_json", []) or []
        ],
    }
    return resolve_phase_prompt(PHASE_QUESTION_EXTRACTION, values)


def find_image_count_mismatch(chunk_result: dict, chunk_raw_text: str) -> dict:
    # Matches both "img-3" and "p1_img-3" (multi-PDF prefixed ids)
    _IMG_ID_RE = r"((?:p\d+_)?img-\d+)"
    source_ids = set(re.findall(_IMG_ID_RE, chunk_raw_text))

    def collect_ids(q_data: dict) -> list:
        texts = [str(q_data.get("question_text", ""))]
        for alt in q_data.get("additional_questions", []) or []:
            texts.append(str(alt.get("question_text", "")))
            for child in alt.get("child_additional_questions", []) or []:
                texts.append(str(child.get("question_text", "")))
        return re.findall(_IMG_ID_RE, " ".join(texts))

    id_to_questions = {}
    for q in chunk_result.get("questions", []):
        q_num = str(q.get("question_number", ""))
        for img_id in collect_ids(q.get("question_data", {})):
            id_to_questions.setdefault(img_id, []).append(q_num)

    output_ids = set(id_to_questions.keys())
    missing = sorted(source_ids - output_ids)

    issues = {}
    if missing:
        issues["missing"] = missing

    # Same image attached to more than one DISTINCT question
    duplicates = {i: qs for i, qs in id_to_questions.items() if len(set(qs)) > 1}
    if duplicates:
        issues["duplicates"] = duplicates
    return issues


def run_phase2_extract_one(chunk: dict, config: dict, max_repair_attempts: int = 1) -> dict:
    chunk_index = chunk["chunk_index"]
    section = chunk.get("section_name") or "General"
    q_range = chunk.get("question_number_range", "unknown")
    label = f"Phase 2 - Chunk {chunk_index} - Question Extraction"

    logger.info(f"[{label}] Starting extraction — section='{section}', questions={q_range}")
    prompt, langfuse_prompt = build_extractor_prompt(chunk, config)
    result = call_mistral_with_retries(
        prompt, EXTRACTOR_SYSTEM_PROMPT, label, langfuse_prompt=langfuse_prompt
    )

    q_count = len(result.get("questions", []))
    logger.info(f"[{label}] Extraction done — {q_count} question(s) extracted.")

    flagged = find_image_count_mismatch(result, chunk["raw_text"])
    needs_repair = bool(flagged) and (flagged.get("missing") or flagged.get("duplicates"))

    if needs_repair and max_repair_attempts > 0:
        problems = []
        if flagged.get("missing"):
            problems.append(
                "These image IDs from the source are MISSING from your output questions: "
                + ", ".join(flagged["missing"])
                + ". Locate each one's referencing question and attach the marker verbatim."
            )
        if flagged.get("duplicates"):
            dup_desc = "; ".join(
                f"{img_id} appears in questions {sorted(set(qs))}"
                for img_id, qs in flagged["duplicates"].items()
            )
            problems.append(
                "These image IDs are wrongly attached to MULTIPLE questions: "
                + dup_desc
                + ". Each id must appear in exactly ONE question — keep it only in the "
                "question whose stem actually references the figure, remove it from the rest."
            )

        logger.warning(f"[{label}] Image mismatch — {' | '.join(problems)} Triggering repair call...")
        repair_prompt = (
            prompt + "\n\n---\n\n## CORRECTION REQUIRED\n" + "\n".join(problems)
        )
        result = call_mistral_with_retries(
            repair_prompt, EXTRACTOR_SYSTEM_PROMPT, f"{label}-Repair",
            langfuse_prompt=langfuse_prompt,
        )
        logger.info(f"[{label}] Repair call done — {len(result.get('questions', []))} question(s) after repair.")

    result["_chunk_index"] = chunk_index
    return result