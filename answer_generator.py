"""
Phase 3 — Answer generation, plus the orchestrator that runs Phase 2 + Phase 3
concurrently across all chunks.

COST REWORK:
- Image URL mapping is NO LONGER sent to (or done by) the LLM. It is now a
  deterministic post-merge step in postprocess.fill_image_urls(). This both
  cuts prompt tokens and eliminates wrong/duplicate image assignments.
- Questions sent to Phase 3 are TRIMMED to only the fields needed to answer:
  question_number, marks, question_type, question_text, options (text only),
  additional_questions / child_additional_questions (same trimmed shape).
"""

import concurrent.futures
import logging

from app.core.config import (
    MARKS_BREAKDOWN_DISABLED_TEXT,
    MARKS_BREAKDOWN_ENABLED_TEXT,
    MAX_PARALLEL_CHUNK_CALLS,
    MAX_QUESTIONS_PER_ANSWER_CALL,
)
from app.pipeline.langfuse_prompts import PHASE_ANSWER_GENERATION, resolve_phase_prompt
from app.pipeline.mistral_client import call_mistral_with_retries
from app.pipeline.question_extractor import run_phase2_extract_one

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
        "marks_breakdown_instructions": (
            MARKS_BREAKDOWN_ENABLED_TEXT
            if config.get("include_marks_breakdown")
            else MARKS_BREAKDOWN_DISABLED_TEXT
        ),
        "chunk_questions": slim_questions_for_answering(chunk_questions),
        # image_url_mapping intentionally removed — handled in postprocess now.
    }
    return resolve_phase_prompt(PHASE_ANSWER_GENERATION, values)


def run_phase3_answer_one(phase2_result: dict, config: dict) -> list:
    """
    Generate answers for one chunk's questions, batched so each Mistral call
    receives at most MAX_QUESTIONS_PER_ANSWER_CALL questions (default 2).
    """
    chunk_index = phase2_result.get("_chunk_index", "?")
    questions = phase2_result.get("questions", [])
    if not questions:
        logger.info(f"[Phase3-Chunk{chunk_index}] No questions to answer — skipping.")
        return []

    batch_size = int(config.get("max_questions_per_answer_call", MAX_QUESTIONS_PER_ANSWER_CALL))
    if batch_size < 1:
        batch_size = MAX_QUESTIONS_PER_ANSWER_CALL

    all_answers: list = []
    total = len(questions)
    num_batches = (total + batch_size - 1) // batch_size
    logger.info(
        f"[Phase3-Chunk{chunk_index}] Answer generation for {total} question(s) "
        f"in {num_batches} batch(es) of up to {batch_size}."
    )

    for batch_num, start in enumerate(range(0, total, batch_size), start=1):
        batch = questions[start : start + batch_size]
        # Always include Chunk N + Batch N so Langfuse generation names map clearly
        label = (
            f"Phase 3 - Chunk {chunk_index} - Answer Generation "
            f"batch {batch_num}/{num_batches}"
        )
        logger.info(f"[{label}] Sending {len(batch)} question(s).")
        prompt, langfuse_prompt = build_answerer_prompt(batch, config, chunk_index)
        logger.info(
            f"[{label}] Answer prompt ready — chars={len(prompt)}, "
            f"from_langfuse={langfuse_prompt is not None}, "
            f"langfuse_name={PHASE_ANSWER_GENERATION.langfuse_name!r}"
        )
        answer_list = call_mistral_with_retries(
            prompt, ANSWERER_SYSTEM_PROMPT, label, expect_array=True,
            langfuse_prompt=langfuse_prompt,
        )
        logger.info(f"[{label}] Got {len(answer_list)} answer(s).")
        all_answers.extend(answer_list)

    logger.info(
        f"[Phase3-Chunk{chunk_index}] Answer generation done — "
        f"{len(all_answers)} answer(s) total for {total} question(s)."
    )
    return all_answers


def run_p2_then_p3_one_chunk(chunk: dict, config: dict):
    phase2_result = run_phase2_extract_one(chunk, config)
    answer_list = run_phase3_answer_one(phase2_result, config)
    return phase2_result, answer_list


def run_phase2_and_phase3_all(chunks: list, config: dict):
    """
    CRITICAL DESIGN NOTE — answers are CHUNK-SCOPED, never global.

    CBSE papers for Accountancy / Business Studies / Economics reuse the
    same question numbers across optional parts (Part A has Q27-Q31 AND
    Part B has its own Q27-Q31). A single global answers-by-number dict
    made Part B's answers overwrite / cross-contaminate Part A's.

    Fix: each chunk's answers are attached to THAT chunk's phase2_result
    (under "_answers"), and merging zips answers strictly within the same
    chunk. Two sections reusing "Q27" can no longer collide, by construction.
    """
    phase2_results = []

    logger.info(f"Submitting {len(chunks)} chunk(s) to thread pool (max_workers={MAX_PARALLEL_CHUNK_CALLS}).")
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_PARALLEL_CHUNK_CALLS) as executor:
        futures = {
            executor.submit(run_p2_then_p3_one_chunk, chunk, config): chunk["chunk_index"]
            for chunk in chunks
        }
        for future in concurrent.futures.as_completed(futures):
            chunk_index = futures[future]
            try:
                phase2_result, answer_list = future.result()
            except Exception as exc:
                logger.error(f"[Phase2+3-Chunk{chunk_index}] Failed: {exc}")
                raise

            logger.info(
                f"[Phase2+3-Chunk{chunk_index}] Completed — "
                f"{len(phase2_result.get('questions', []))} questions, {len(answer_list)} answers."
            )

            chunk_answers = {}
            for answer_obj in answer_list:
                q_num = str(answer_obj.get("question_number", ""))
                if not q_num:
                    continue
                # NOTE: question_image from the LLM is IGNORED on purpose —
                # image URLs are now filled deterministically in postprocess.
                chunk_answers[q_num] = {
                    "answer_data": answer_obj.get("answer_data", {}),
                }
            phase2_result["_answers"] = chunk_answers
            phase2_results.append(phase2_result)

    phase2_results.sort(key=lambda r: r["_chunk_index"])
    total_answers = sum(len(r.get("_answers", {})) for r in phase2_results)
    logger.info(f"All chunks processed — {len(phase2_results)} result(s), {total_answers} answer(s) indexed (chunk-scoped).")
    return phase2_results, None  # second value kept for call-site compatibility