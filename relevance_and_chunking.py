"""
Phase 1 — Relevance check and boundary-based document splitting.

The LLM now returns only START MARKERS (short literal substrings), and this
module slices the actual chunk text locally with str.find(). This removes
the old "copy the whole paper verbatim into raw_text" behaviour, which was
doubling output-token cost and risking silent verbatim-copy corruption.
"""

import logging

from app.core.config import MAX_QUESTIONS_PER_CHUNK
from app.pipeline.langfuse_prompts import PHASE_RELEVANCE, resolve_phase_prompt
from app.pipeline.mistral_client import call_mistral_with_retries

logger = logging.getLogger("app.relevance_chunking")

SPLITTER_SYSTEM_PROMPT = (
    "You are a precise document boundary-detection assistant. Return only literal "
    "marker substrings copied exactly from the document. Never copy chunk contents."
)


def build_splitter_prompt(doc_content: str, config: dict) -> tuple:
    values = {
        "board_name": config["board_name"],
        "grade": config["grade"],
        "subject": config["subject"],
        "topic": config["topic"],
        "chapter_descriptions": config["chapter_descriptions"],
        "pdf_content": doc_content,
        "max_questions_per_chunk": config.get("max_questions_per_chunk", MAX_QUESTIONS_PER_CHUNK),
    }
    return resolve_phase_prompt(PHASE_RELEVANCE, values)


def _find_marker(doc: str, marker: str, search_from: int) -> int:
    """
    Locate a marker in the document at/after search_from.
    Tries exact match first, then a whitespace-normalized fallback
    (OCR text often has inconsistent spacing).
    Returns -1 if not found.
    """
    if not marker:
        return -1
    pos = doc.find(marker, search_from)
    if pos != -1:
        return pos
    # Fallback: collapse runs of whitespace on both sides and retry
    import re
    norm_marker = re.sub(r"\s+", " ", marker.strip())
    # Build a regex that treats any whitespace run as \s+
    pattern = re.escape(norm_marker).replace(r"\ ", r"\s+")
    m = re.compile(pattern).search(doc, search_from)
    return m.start() if m else -1


def slice_chunks_locally(doc_text: str, result: dict) -> dict:
    """
    Convert the marker-only LLM response into full chunks by slicing
    doc_text locally. Adds 'raw_text' to every chunk (so Phase 2 is
    completely unchanged). Also cuts off any trailing marking-scheme
    block and stores it verbatim in 'marking_scheme_reference'.
    """
    work_text = doc_text
    result.setdefault("marking_scheme_reference", None)

    # 1. Cut off the answer-key/marking-scheme tail, keep it as reference
    ms_marker = result.get("marking_scheme_start_marker")
    if ms_marker:
        ms_pos = _find_marker(work_text, ms_marker, 0)
        if ms_pos != -1:
            result["marking_scheme_reference"] = work_text[ms_pos:].strip()
            work_text = work_text[:ms_pos]
            logger.info(f"[Phase1-Split] Marking-scheme block detected and cut at pos {ms_pos}.")
        else:
            logger.warning("[Phase1-Split] marking_scheme_start_marker not found in doc — ignoring.")

    chunks = result.get("chunks", [])
    if not chunks:
        raise RuntimeError("Zero chunks returned by Phase 1.")

    # 2. Resolve every start_marker to a document position (in order)
    positions = []
    cursor = 0
    for chunk in chunks:
        marker = chunk.get("start_marker", "")
        pos = _find_marker(work_text, marker, cursor)
        if pos == -1:
            logger.warning(
                f"[Phase1-Split] Marker not found for chunk {chunk.get('chunk_index')}: "
                f"{marker[:80]!r} — will merge into previous chunk."
            )
        positions.append(pos)
        if pos != -1:
            cursor = pos + 1  # next search starts after this one → enforces order

    # 3. First resolvable chunk should start at 0 so no leading text is lost
    #    (general instructions / paper header stay with chunk 1)
    first_found = next((i for i, p in enumerate(positions) if p != -1), None)
    if first_found is None:
        logger.warning("[Phase1-Split] NO markers found — falling back to single chunk with full text.")
        result["chunks"] = [{
            "chunk_index": 1,
            "section_name": chunks[0].get("section_name"),
            "question_number_range": "unknown",
            "raw_text": work_text,
        }]
        return result
    positions[first_found] = 0

    # 4. Slice: each chunk runs from its position to the next found position
    resolved = []
    found_indices = [i for i, p in enumerate(positions) if p != -1]
    for order, i in enumerate(found_indices):
        start = positions[i]
        end = positions[found_indices[order + 1]] if order + 1 < len(found_indices) else len(work_text)
        raw = work_text[start:end].strip()
        if not raw:
            continue
        chunk = chunks[i]
        resolved.append({
            "chunk_index": len(resolved) + 1,
            "section_name": chunk.get("section_name"),
            "question_number_range": chunk.get("question_number_range", "unknown"),
            "raw_text": raw,
        })

    # 5. Safety check: total sliced length should cover ~the whole doc
    covered = sum(len(c["raw_text"]) for c in resolved)
    if covered < 0.9 * len(work_text.strip()):
        logger.warning(
            f"[Phase1-Split] Sliced chunks cover only {covered}/{len(work_text)} chars — "
            "possible marker misses. Check logs above."
        )

    result["chunks"] = resolved
    return result


def run_phase1_split(doc_text: str, config: dict) -> dict:
    logger.info(
        f"[Phase1-Split] Starting — board={config['board_name']}, grade={config['grade']}, "
        f"subject={config['subject']}, doc_length={len(doc_text)} chars."
    )
    prompt, langfuse_prompt = build_splitter_prompt(doc_text, config)
    result = call_mistral_with_retries(
        prompt, SPLITTER_SYSTEM_PROMPT, "Phase 1 - Relevance and Chunking",
        langfuse_prompt=langfuse_prompt,
    )

    if not result.get("is_relevant", True):
        reason = result.get("rejection_reason", "No reason provided")
        logger.warning(f"[Phase1-Split] Document REJECTED — {reason}")
        return result

    result = slice_chunks_locally(doc_text, result)

    split_mode = result.get("split_mode", "unknown")
    logger.info(f"[Phase1-Split] Done — {len(result['chunks'])} chunk(s), split_mode={split_mode}.")
    return result