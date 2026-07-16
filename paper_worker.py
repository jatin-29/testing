"""
Background worker — orchestrates the full end-to-end paper processing pipeline
for a single job.  Called from the API layer via FastAPI BackgroundTasks.
"""
import json
import logging
import os
import re
import shutil
import tempfile
import traceback
from pathlib import Path

import requests
from dotenv import load_dotenv

from app.core.config import CREATE_PAPER_URL, MODEL
from app.db import database as db
from app.models.schemas import PaperFile, UploadPaperRequest
from app.pipeline.relevance_and_chunking import run_phase1_split
from app.pipeline.answer_generator import run_phase2_and_phase3_all
from app.pipeline.postprocess import auto_repair, fill_image_urls, merge_chunks
from app.pipeline.mistral_client import get_usage_summary, reset_usage_tracker
from app.pipeline.langfuse_client import end_job_trace, start_job_trace
from app.services.ocr_service import (
    build_combined_markdown,
    flatten_markdown_tables,
    run_ocr,
    save_images,
)
from app.services.upload_service import upload_images_dir

load_dotenv()
logger = logging.getLogger("app.paper_worker")

# Matches "img-3" and "p1_img-3" (multi-PDF prefixed ids)
_IMG_ID_RE = re.compile(r"((?:p\d+_)?img-\d+)")


# ---------------------------------------------------------------------------
# Main entry point called by the API route
# ---------------------------------------------------------------------------

def process_paper(job_id: str, req: UploadPaperRequest) -> None:
    work_dir = Path(tempfile.mkdtemp(prefix=f"paper_{job_id}_"))
    reset_usage_tracker()

    # Orchestration: open ONE Langfuse parent trace keyed by payload request_id.
    # Phases never create traces — they resolve prompts + call LLM;
    # mistral_client emits generations via log_llm_generation().
    start_job_trace(
        trace_name=f"{req.subject} Paper Extraction",
        request_id=req.request_id,
        user_name=req.user_name,
        metadata={
            "exam_id": req.exam_id,
            "board": req.board_name,
            "grade": req.grade,
            "subject": req.subject,
            "job_id": job_id,
        },
    )

    try:
        # ── Step 1 & 2: Download PDFs + run OCR ────────────────────────────
        logger.info(f"[job:{job_id}] STEP 1/7 — OCR: downloading PDF(s) and running Mistral OCR...")
        db.update_job_status(job_id, "ocr")
        combined_md, images_dir = _ocr_all_pdfs(req.question_paper_url, work_dir)
        logger.info(f"[job:{job_id}] STEP 1/7 done — {len(combined_md)} chars of markdown extracted.")

        # ── Step 3: Upload extracted images to the bucket ───────────────────
        logger.info(f"[job:{job_id}] STEP 2/7 — Uploading extracted images to bucket...")
        db.update_job_status(job_id, "uploading_images")
        image_url_mapping = _upload_images(images_dir)
        logger.info(f"[job:{job_id}] STEP 2/7 done — {len(image_url_mapping)} image(s) uploaded.")

        # ── Step 4: Build pipeline config from request ──────────────────────
        config = _build_config(req, image_url_mapping)

        # ── Step 5: Run 3-phase Mistral extraction pipeline ─────────────────
        logger.info(f"[job:{job_id}] STEP 3/7 — Phase 1: relevance check + chunking...")
        db.update_job_status(job_id, "extracting")

        split_result = run_phase1_split(combined_md, config)
        if not split_result.get("is_relevant", True):
            raise RuntimeError(
                f"Paper rejected by Phase 1: "
                f"{split_result.get('rejection_reason', 'no reason provided')}"
            )

        chunks = split_result.get("chunks", [])
        has_sections = split_result.get("split_mode") == "by_section"
        logger.info(
            f"[job:{job_id}] STEP 3/7 done — {len(chunks)} chunk(s), has_sections={has_sections}."
        )

        logger.info(f"[job:{job_id}] STEP 4-5/7 — Phase 2 (extract questions) + Phase 3 (generate answers)...")
        phase2_results, answers_by_number = run_phase2_and_phase3_all(chunks, config)
        logger.info(f"[job:{job_id}] STEP 4-5/7 done — {len(phase2_results)} chunk(s) processed.")

        logger.info(f"[job:{job_id}] STEP 6/7 — Merge + deterministic image mapping + auto-repair...")
        parsed_data = merge_chunks(
            phase2_results, answers_by_number, config, has_sections=has_sections
        )

        # ── Deterministic, globally-deduped image URL filling (was Phase 3) ─
        parsed_data = fill_image_urls(parsed_data, image_url_mapping)

        parsed_data, fixes = auto_repair(parsed_data, config)
        logger.info(f"[job:{job_id}] STEP 6/7 done — auto-repaired {len(fixes)} issue(s).")

        # ── Global cross-chunk image audit (read-only, logs verdict) ────────
        _audit_images_globally(job_id, combined_md, parsed_data)

        _enrich_all_answer_data(parsed_data)

        # ── Attach token usage directly onto the output JSON ─────────────────
        usage_summary = get_usage_summary()
        logger.info(
            f"[job:{job_id}] TOKEN USAGE — {usage_summary['calls']} call(s), "
            f"{usage_summary['total_tokens']} total tokens, "
            f"estimated cost=${usage_summary['estimated_cost_usd']}"
        )
        parsed_data["token_details"] = {
            "prompt_tokens": usage_summary["prompt_tokens"],
            "completion_tokens": usage_summary["completion_tokens"],
            "total_tokens": usage_summary["total_tokens"],
            "estimated_cost_usd": usage_summary["estimated_cost_usd"],
            "metadata": {
                "calls": usage_summary["calls"],
                "model": MODEL,
            },
        }

        # ── Step 7: POST to create-paper/ API ───────────────────────────────
        logger.info(f"[job:{job_id}] STEP 7/7 — Posting to create-paper/ API...")
        db.update_job_status(job_id, "saving")

        api_response = _call_create_paper(req.exam_id, req.exam_type, parsed_data)

        db.update_job_done(job_id, api_response)
        logger.info(f"[job:{job_id}] STEP 7/7 done — create-paper/ responded OK.")
        end_job_trace("done")

    except Exception as exc:
        error_detail = f"{type(exc).__name__}: {exc}"
        logger.error(f"[job:{job_id}] FAILED — {error_detail}")
        logger.error(traceback.format_exc())
        db.update_job_failed(job_id, str(exc))
        end_job_trace("failed")

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Step helpers (unchanged below except imports)
# ---------------------------------------------------------------------------

def _ocr_all_pdfs(paper_files: list[PaperFile], work_dir: Path) -> tuple[str, Path]:
    images_dir = work_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    multi = len(paper_files) > 1
    all_markdown_parts: list[str] = []

    for idx, paper_file in enumerate(paper_files):
        pdf_path = work_dir / f"paper_{idx}_{paper_file.file_name}"
        _download_file(paper_file.url, pdf_path)

        ocr_response = run_ocr(pdf_path)
        pages = ocr_response.pages

        raw_md = build_combined_markdown(pages)
        cleaned_md = flatten_markdown_tables(raw_md)

        if multi:
            cleaned_md = re.sub(r"\bimg-(\d+)", rf"p{idx}_img-\1", cleaned_md)

        all_markdown_parts.append(cleaned_md)

        pdf_images_dir = work_dir / f"images_{idx}"
        pdf_images_dir.mkdir(parents=True, exist_ok=True)
        save_images(pages, pdf_images_dir)

        for img_file in pdf_images_dir.iterdir():
            new_name = f"p{idx}_{img_file.name}" if multi else img_file.name
            dest = images_dir / new_name
            shutil.move(str(img_file), str(dest))

    combined_md = "\n\n".join(all_markdown_parts)
    return combined_md, images_dir


def _audit_images_globally(job_id: str, combined_md: str, parsed_data: dict) -> None:
    source_ids = set(_IMG_ID_RE.findall(combined_md))
    if not source_ids:
        return

    def _opt_texts(entry: dict) -> list:
        return [str(o.get("option_text", "")) for o in (entry.get("options") or [])]

    seen: dict[str, list] = {}
    for section in parsed_data.get("sections", []):
        for q in section.get("questions", []):
            q_num = str(q.get("question_number", "?"))
            q_data = q.get("question_data", {})
            # NOTE: MCQ options can themselves be images — must scan
            # option_text too, matching fill_image_urls()'s scope, or this
            # audit falsely reports options-only images as "missing".
            texts = [str(q_data.get("question_text", ""))] + _opt_texts(q_data)
            for alt in q_data.get("additional_questions", []) or []:
                texts.append(str(alt.get("question_text", "")))
                texts += _opt_texts(alt)
                for child in alt.get("child_additional_questions", []) or []:
                    texts.append(str(child.get("question_text", "")))
                    texts += _opt_texts(child)
            for img_id in _IMG_ID_RE.findall(" ".join(texts)):
                seen.setdefault(img_id, []).append(q_num)

    missing = sorted(source_ids - set(seen.keys()))
    duplicates = {i: sorted(set(qs)) for i, qs in seen.items() if len(set(qs)) > 1}

    if missing:
        logger.warning(f"[job:{job_id}] GLOBAL IMAGE AUDIT — missing from final output: {', '.join(missing)}")
    if duplicates:
        logger.warning(f"[job:{job_id}] GLOBAL IMAGE AUDIT — attached to multiple questions: {duplicates}")
    if not missing and not duplicates:
        logger.info(f"[job:{job_id}] GLOBAL IMAGE AUDIT — all {len(source_ids)} image id(s) mapped exactly once. OK")


def _download_file(url: str, dest_path: Path) -> None:
    response = requests.get(url, timeout=120, stream=True)
    response.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)


def _upload_images(images_dir: Path) -> dict:
    api_token = os.environ.get("UPLOAD_API_TOKEN", "")
    if not api_token:
        logger.warning("UPLOAD_API_TOKEN not set — skipping image upload.")
        return {}
    if not any(images_dir.iterdir()):
        return {}
    return upload_images_dir(images_dir, api_token=api_token)


def _build_config(req: UploadPaperRequest, image_url_mapping: dict) -> dict:
    chapter_names = [c.chapter_name for c in req.chapter_json]
    chapter_descriptions = ", ".join(chapter_names)

    chapter_json_list = [
        {"chapter_name": c.chapter_name, "chapter_id": c.chapter_id}
        for c in req.chapter_json
    ]
    if not any(c["chapter_name"] == "General" for c in chapter_json_list):
        chapter_json_list.append(
            {"chapter_name": "General", "chapter_id": "00000000-0000-0000-0000-000000000000"}
        )

    return {
        "board_name": req.board_name,
        "grade": req.grade,
        "subject": req.subject,
        "topic": req.topic,
        "chapter_descriptions": chapter_descriptions,
        "chapter_json": chapter_json_list,
        "exam_name": req.exam_id,
        "difficulty_level": req.difficulty_level,
        "start_date": req.start_date,
        "end_date": req.end_date,
        "exam_type": req.exam_type,
        "include_marks_breakdown": req.include_marks_breakdown,
        "max_questions_per_chunk": 10,
        "max_questions_per_answer_call": 2,
        "image_url_mapping": image_url_mapping,
    }


def _enrich_answer_data(answer_data: dict) -> None:
    if not isinstance(answer_data, dict):
        return
    answer_data.setdefault("answer_image", [])
    answer_data.setdefault("answer_url", "")

    for sub in answer_data.get("additional_answers") or []:
        _enrich_answer_data(sub)
    for child in answer_data.get("child_additional_answers") or []:
        _enrich_answer_data(child)


def _enrich_all_answer_data(parsed_data: dict) -> None:
    for section in parsed_data.get("sections", []):
        for question in section.get("questions", []):
            if "answer_data" in question:
                _enrich_answer_data(question["answer_data"])


def _call_create_paper(exam_id: str, exam_type: str, parsed_data: dict) -> dict:
    internal_token = os.environ.get("INTERNAL_API_TOKEN", "")
    if not internal_token:
        raise ValueError("INTERNAL_API_TOKEN is not set in the environment.")

    if exam_type == "WEEKLY_TEST":
        payload = {
            "exam_id": exam_id,
            "sections": parsed_data.get("sections", []),
        }
    else:
        all_questions = [
            q
            for section in parsed_data.get("sections", [])
            for q in section.get("questions", [])
        ]
        payload = {
            "exam_id": exam_id,
            "questions": all_questions,
        }

    payload["token_details"] = parsed_data.get("token_details", {})

    logger.info(
        f"[create-paper] Payload to be sent:\n{json.dumps(payload, indent=2, ensure_ascii=False)}"
    )

    headers = {
        "x-access-token": internal_token,
        "Content-Type": "application/json",
    }
    response = requests.post(CREATE_PAPER_URL, headers=headers, json=payload, timeout=60)

    try:
        response_json = response.json()
    except Exception:
        response_json = {"raw": response.text}

    if not response.ok:
        raise RuntimeError(
            f"create-paper/ API returned {response.status_code}: {response.text}"
        )

    return response_json