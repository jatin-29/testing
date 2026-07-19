"""
Post-processing: merging chunks, MCQ option resolution, marks reconciliation,
DETERMINISTIC image URL filling, and the auto-repair pass.
"""

import logging
import re

from app.pipeline.helpers import resolve_chapter_id

logger = logging.getLogger("app.postprocess")

# Matches ![img-3.jpeg] and ![p1_img-3.jpeg] style markers
_IMG_MARKER_RE = re.compile(r"!\[(?:p\d+_)?img-")
# Full marker with captured id: ![img-3.jpeg](img-3.jpeg) / ![p1_img-3.jpeg](...)
_IMG_FULL_RE = re.compile(r"!\[((?:p\d+_)?img-\d+)[^\]]*\]\([^)]*\)")
# Bare id
_IMG_ID_RE = re.compile(r"((?:p\d+_)?img-\d+)")
# "the question actually references a figure" signal
_FIG_REF_RE = re.compile(
    r"\b(fig\.?\s?\d*|figure|given below|shown|diagram|graph|in the given)\b", re.I
)


# ---------------------------------------------------------------------------
# Marks helpers
# ---------------------------------------------------------------------------

def sum_marks_for_section(questions: list) -> int:
    return sum(q.get("marks", 0) for q in questions)


# ---------------------------------------------------------------------------
# DETERMINISTIC image URL filling  (replaces Phase 3 Step 1 entirely)
# ---------------------------------------------------------------------------

def _option_texts(entry: dict) -> list:
    """Collect option_text from a question/sub-question's options array.
    MCQ options can themselves BE images (each choice is a figure), so this
    must be scanned for img-N markers just like question_text."""
    return [str(o.get("option_text", "")) for o in (entry.get("options") or [])]


def _root_scope_texts(q_data: dict) -> list:
    """Root question text + options + SUB_QUESTION texts + their options ->
    'question_image' key scope."""
    texts = [str(q_data.get("question_text", ""))] + _option_texts(q_data)
    for alt in q_data.get("additional_questions", []) or []:
        if alt.get("additional_question_type") == "SUB_QUESTION":
            texts.append(str(alt.get("question_text", "")))
            texts += _option_texts(alt)
    return texts


def _alt_scope_texts(q_data: dict) -> list:
    """ALTERNATIVE texts + options + all child_additional_questions (+ their
    options) -> 'child_additional_questions' key scope."""
    texts = []
    for alt in q_data.get("additional_questions", []) or []:
        if alt.get("additional_question_type") == "ALTERNATIVE":
            texts.append(str(alt.get("question_text", "")))
            texts += _option_texts(alt)
        for child in alt.get("child_additional_questions", []) or []:
            texts.append(str(child.get("question_text", "")))
            texts += _option_texts(child)
    return texts


def _strip_marker_from_question(q_data: dict, img_id: str) -> None:
    """Remove every ![img_id...](...) marker from all text fields of one
    question -- question_text AND every option's option_text (MCQ choices
    can themselves be images)."""
    pat = re.compile(r"\s?!\[" + re.escape(img_id) + r"[^\]]*\]\([^)]*\)")

    def clean(entry: dict):
        if isinstance(entry.get("question_text"), str):
            entry["question_text"] = pat.sub("", entry["question_text"])
        for opt in entry.get("options") or []:
            if isinstance(opt.get("option_text"), str):
                opt["option_text"] = pat.sub("", opt["option_text"])

    clean(q_data)
    for alt in q_data.get("additional_questions", []) or []:
        clean(alt)
        for child in alt.get("child_additional_questions", []) or []:
            clean(child)


def _rewrite_marker_url(q_data: dict, img_id: str, url: str) -> None:
    """
    Replace every ![img_id...](...) marker with a plain placeholder phrase
    (no URL) -- in question_text AND in every option's option_text.

    DESIGN DECISION (confirmed with team): the frontend renders images ONLY
    from the structured `question_image` array, never by markdown-parsing
    question_text. Leaving a resolved ![id](url) marker inside question_text
    was causing the SAME image to render twice -- once from question_image,
    once from an inline markdown parse of question_text. So instead of
    embedding the URL inline, we now strip the marker down to a neutral
    placeholder and rely entirely on question_image for rendering.
    """
    pat = re.compile(r"!\[" + re.escape(img_id) + r"[^\]]*\]\([^)]*\)")
    placeholder = "(see figure)"

    def clean(entry: dict):
        t = entry.get("question_text")
        if isinstance(t, str) and img_id in t:
            entry["question_text"] = pat.sub(placeholder, t)
        for opt in entry.get("options") or []:
            ot = opt.get("option_text")
            if isinstance(ot, str) and img_id in ot:
                opt["option_text"] = pat.sub(placeholder, ot)

    clean(q_data)
    for alt in q_data.get("additional_questions", []) or []:
        clean(alt)
        for child in alt.get("child_additional_questions", []) or []:
            clean(child)


def fill_image_urls(data: dict, image_url_mapping: dict) -> dict:
    """
    Scan every question's texts for ![img-N] markers and build its
    question_image array from image_url_mapping — pure code, no LLM.

    Guarantees:
      1. question_image is ALWAYS a list of {"key": ..., "url": ...} objects
         (fixes the string-vs-array schema leak permanently).
      2. Every img id maps to EXACTLY ONE question globally. If the same id
         appears in multiple questions, the question whose own text contains
         a figure-reference phrase wins; ties -> first occurrence. Losers get
         the marker stripped from their text too.
      3. Ids with no URL in the mapping are skipped (never invented).
    """
    image_url_mapping = image_url_mapping or {}

    # ── Pass 1: collect every occurrence globally ───────────────────────────
    occurrences = {}  # img_id -> list of (question_ref, has_fig_ref)
    all_questions = []
    for section in data.get("sections", []):
        for q in section.get("questions", []):
            all_questions.append(q)
            q_data = q.get("question_data", {}) or {}
            texts = " ".join(_root_scope_texts(q_data) + _alt_scope_texts(q_data))
            ids_here = set(_IMG_FULL_RE.findall(texts)) or set(_IMG_ID_RE.findall(texts))
            has_ref = bool(_FIG_REF_RE.search(texts))
            for img_id in ids_here:
                occurrences.setdefault(img_id, []).append((q, has_ref))

    # ── Pass 2: resolve duplicates — one owner per id ───────────────────────
    for img_id, holders in occurrences.items():
        if len(holders) <= 1:
            continue
        with_ref = [h for h in holders if h[1]]
        owner = (with_ref[0] if with_ref else holders[0])[0]
        for q, _ in holders:
            if q is not owner:
                _strip_marker_from_question(q.get("question_data", {}) or {}, img_id)
                logger.warning(
                    f"[fill_image_urls] '{img_id}' duplicated — kept on "
                    f"Q{owner.get('question_number')}, stripped from Q{q.get('question_number')}."
                )

    # ── Pass 3: rebuild question_image per question, scope-aware ────────────
    for q in all_questions:
        q_data = q.get("question_data", {}) or {}
        entries = []
        # BUGFIX: "seen" must be shared ACROSS both scopes, not reset per
        # scope. A root question and its own ALTERNATIVE/child often
        # reference the SAME shared diagram (e.g. "the figure below" used by
        # both option A and its OR-alternative) — with a per-scope "seen"
        # set, that one image id passed the dedupe check twice (once in each
        # scope) and got appended to `entries` twice, producing a duplicate
        # image within a single question's own question_image array.
        seen = set()
        for scope_key, texts in (
            ("question_image", _root_scope_texts(q_data)),
            ("child_additional_questions", _alt_scope_texts(q_data)),
        ):
            joined = " ".join(texts)
            found = _IMG_FULL_RE.findall(joined) or _IMG_ID_RE.findall(joined)
            for img_id in found:
                if img_id in seen:
                    continue
                seen.add(img_id)
                url = image_url_mapping.get(img_id)
                if url:
                    entries.append({"key": scope_key, "url": url})
                    # Rewrite the inline marker to point at the hosted URL so
                    # markdown renderers show the actual image, not a broken icon.
                    _rewrite_marker_url(q_data, img_id, url)
                else:
                    # No hosted URL -> a bare ![img-N](img-N.jpeg) marker would
                    # render as a broken image icon. Strip it from the text.
                    _strip_marker_from_question(q_data, img_id)
                    logger.warning(
                        f"[fill_image_urls] Q{q.get('question_number')}: no hosted URL "
                        f"for '{img_id}' — marker stripped from text."
                    )
        q["question_image"] = entries  # ALWAYS an array

    return data


# ---------------------------------------------------------------------------
# MCQ option resolution (unchanged)
# ---------------------------------------------------------------------------

def _clean_option_letter(raw_prefix: str) -> str:
    return raw_prefix.strip().strip("()").rstrip(").").strip().upper() if raw_prefix else ""


def resolve_mcq_options(q_type: str, q_data: dict, a_data: dict):
    if not q_data or not a_data:
        return

    if q_type == "MCQ":
        correct_letter = str(a_data.get("answer_text", "")).strip().upper()
        options = q_data.get("options") or []
        for opt in options:
            prefix = _clean_option_letter(opt.get("option_prefix", ""))
            opt["is_correct"] = correct_letter == prefix

    add_qs = q_data.get("additional_questions") or []
    add_as = a_data.get("additional_answers") or []
    ans_by_prefix = {str(ans.get("answer_prefix", "")): ans for ans in add_as}
    for sub_q in add_qs:
        prefix = str(sub_q.get("question_prefix", ""))
        sub_a = ans_by_prefix.get(prefix)
        if sub_a:
            resolve_mcq_options(sub_q.get("question_type"), sub_q, sub_a)

    child_qs = q_data.get("child_additional_questions") or []
    child_as = a_data.get("child_additional_answers") or []
    child_ans_by_prefix = {str(ans.get("answer_prefix", "")): ans for ans in child_as}
    for child_q in child_qs:
        prefix = str(child_q.get("question_prefix", ""))
        child_a = child_ans_by_prefix.get(prefix)
        if child_a:
            resolve_mcq_options(child_q.get("question_type"), child_q, child_a)


# ---------------------------------------------------------------------------
# Answer zipping — no longer touches question_image (postprocess owns it)
# ---------------------------------------------------------------------------

def zip_answers_into_questions(questions: list, answers_by_number: dict) -> list:
    for q in questions:
        q_num = str(q.get("question_number", ""))
        entry = answers_by_number.get(q_num)
        if entry is None:
            entry = {"answer_data": {"answer_text": ""}}

        answer_data = entry.get("answer_data", {})
        resolve_mcq_options(q.get("question_type"), q.get("question_data", {}), answer_data)
        q["answer_data"] = answer_data
    return questions


# ---------------------------------------------------------------------------
# Chunk merge (unchanged)
# ---------------------------------------------------------------------------

def merge_chunks(
    phase2_results: list, answers_by_number: dict = None, config: dict = None, has_sections: bool = True
) -> dict:
    """
    answers_by_number is DEPRECATED and ignored — answers now live inside
    each phase2_result under "_answers" (chunk-scoped), so papers that reuse
    question numbers across optional parts (Accountancy Part A / Part B)
    can never cross-contaminate. The parameter stays for call-site
    compatibility only.
    """
    logger.info(f"Merging {len(phase2_results)} chunk(s) — has_sections={has_sections}.")
    sections_by_name, order = {}, []

    for result in sorted(phase2_results, key=lambda r: r["_chunk_index"]):
        raw_name = result.get("section_name")
        name = (raw_name or "General") if has_sections else (raw_name or "")
        chunk_answers = result.get("_answers", {}) or {}
        questions = zip_answers_into_questions(result.get("questions", []), chunk_answers)

        for q in questions:
            chapter_id, resolved_name = resolve_chapter_id(config, q.get("chapter_name"))
            q["chapter_id"] = chapter_id
            q["chapter_name"] = resolved_name

        if name not in sections_by_name:
            sections_by_name[name] = []
            order.append(name)
        sections_by_name[name].extend(questions)

    sections = [
        {
            "section_name": name,
            "total_question": len(sections_by_name[name]),
            "total_marks": sum_marks_for_section(sections_by_name[name]),
            "attempts_required": len(sections_by_name[name]),
            "priority": i,
            "questions": sections_by_name[name],
        }
        for i, name in enumerate(order, start=1)
    ]
    total_q = sum(len(sections_by_name[n]) for n in order)
    logger.info(f"Merge complete — {len(sections)} section(s), {total_q} question(s) total.")
    return {
        "exam_name": config["exam_name"],
        "difficulty_level": config["difficulty_level"],
        "start_date": config["start_date"],
        "end_date": config["end_date"],
        "exam_type": config["exam_type"],
        "sections": sections,
    }


# ---------------------------------------------------------------------------
# Validation (unchanged)
# ---------------------------------------------------------------------------

def validate_output(data: dict) -> list:
    warnings = []
    for section in data.get("sections", []):
        sec_name = section.get("section_name", "?")
        for q in section.get("questions", []):
            q_num = q.get("question_number", "?")
            q_data = q.get("question_data", {})
            prefix = f"[{sec_name} Q{q_num}]"
            if "sub_questions" in q_data:
                warnings.append(f"{prefix} Contains forbidden 'sub_questions' key in question_data.")
            if "sub_answers" in q.get("answer_data", {}):
                warnings.append(f"{prefix} Contains forbidden 'sub_answers' key in answer_data.")
    return warnings


# ---------------------------------------------------------------------------
# Auto-repair helpers (unchanged except strip_orphan_images removed —
# fill_image_urls now guarantees marker<->entry consistency by construction)
# ---------------------------------------------------------------------------

def _reconcile_one_level(entries: list, parent_marks: float) -> bool:
    """Scale a list of SUB_QUESTION siblings so their marks sum exactly to
    parent_marks. Returns True if anything changed."""
    sub_qs = [item for item in entries if item.get("additional_question_type") == "SUB_QUESTION"]
    if not sub_qs or not parent_marks:
        return False

    sub_marks_sum = sum(s.get("marks", 0) for s in sub_qs)
    if sub_marks_sum == parent_marks:
        return False

    if sub_marks_sum <= 0:
        n = len(sub_qs)
        for s in sub_qs:
            s["marks"] = round(parent_marks / n, 1)
    else:
        for s in sub_qs:
            s["marks"] = round(s.get("marks", 0) * (parent_marks / sub_marks_sum), 1)

    drift = round(parent_marks - sum(s.get("marks", 0) for s in sub_qs), 1)
    if drift and sub_qs:
        sub_qs[-1]["marks"] = round(sub_qs[-1].get("marks", 0) + drift, 1)
    return True


def reconcile_sub_questions_marks(q: dict) -> bool:
    """
    Recursively reconciles SUB_QUESTION mark-splits at EVERY nesting level —
    not just the root's direct additional_questions. This fixes the bug where
    a nested ALTERNATIVE (an OR-option or a "visually impaired" variant) that
    itself splits into SUB_QUESTION children left those children's marks
    untouched (often stuck at 0) even though the ALTERNATIVE's own marks
    value was correct.
    """
    q_data = q.get("question_data", {})
    root_marks = q.get("marks", 0)
    add_qs = q_data.get("additional_questions", []) or []

    changed = _reconcile_one_level(add_qs, root_marks)

    # Recurse: for every entry (SUB_QUESTION or ALTERNATIVE) that itself has
    # child_additional_questions, reconcile THOSE children against that
    # entry's own (now-correct) marks value.
    def recurse(entries: list):
        nonlocal changed
        for entry in entries:
            children = entry.get("child_additional_questions") or []
            if children:
                if _reconcile_one_level(children, entry.get("marks", 0)):
                    changed = True
                recurse(children)
            nested = entry.get("additional_questions") or []
            if nested:
                if _reconcile_one_level(nested, entry.get("marks", 0)):
                    changed = True
                recurse(nested)

    recurse(add_qs)
    return changed


def strip_hallucinated_top_level_keys(q: dict) -> bool:
    """
    Phase 2 occasionally invents a top-level key that isn't part of the
    schema -- observed case: "chapter_image" (a malformed duplicate of
    "question_image" with an unresolved placeholder URL, sitting beside a
    correct "question_image" array). Strip any such stray key. This is
    defensive cleanup, not a fix for WHY the LLM invented it (that needs a
    prompt-level constraint too).
    """
    KNOWN_STRAY_KEYS = ("chapter_image",)
    changed = False
    for key in KNOWN_STRAY_KEYS:
        if key in q:
            logger.warning(
                f"[strip_hallucinated_top_level_keys] Q{q.get('question_number')}: "
                f"removed hallucinated top-level key '{key}' (value: {q[key]!r})."
            )
            del q[key]
            changed = True
    return changed


def flag_unanswered_root_with_alternative(q: dict) -> bool:
    """
    Detects the confirmed bug where Phase 3 answers a root question's
    ALTERNATIVE sibling but leaves the root's own answer_text empty (and
    every option.is_correct false for MCQs). This is NOT silently
    auto-fixed (we cannot invent a correct answer) — it is logged loudly
    so it surfaces in review instead of shipping a blank answer.
    Returns True if the issue was detected (for fix-count reporting).
    """
    q_data = q.get("question_data", {}) or {}
    a_data = q.get("answer_data", {}) or {}
    add_qs = q_data.get("additional_questions") or []
    has_alternative_sibling = any(
        a.get("additional_question_type") == "ALTERNATIVE" for a in add_qs
    )
    if not has_alternative_sibling:
        return False

    root_text_empty = not str(a_data.get("answer_text", "")).strip()
    options = q_data.get("options") or []
    any_option_correct = any(o.get("is_correct") for o in options)

    is_unanswered = root_text_empty and (not options or not any_option_correct)
    if is_unanswered:
        logger.error(
            f"[flag_unanswered_root_with_alternative] Q{q.get('question_number')}: "
            "root question has an ALTERNATIVE sibling but its OWN answer_text is empty "
            "and no option is marked correct — Phase 3 likely answered only the "
            "alternative. THIS QUESTION IS SHIPPING WITHOUT A ROOT ANSWER — flag for review."
        )
    return is_unanswered


def flag_misnested_alternative_marks(q: dict) -> list:
    """
    Detects the confirmed Physics-paper bug: when a source PDF has two full
    alternative question blocks like "32(I) ... OR ... 32(II) ..." (each a
    complete, independently-worth-full-marks question), Phase 2 sometimes
    nests the second block's ALTERNATIVE deep inside a SMALL sub-part of the
    first block instead of making it a root-level sibling. Signature: a
    nested ALTERNATIVE's own marks are LARGER than its immediate parent
    SUB_QUESTION's marks (a sub-part worth 1-2 marks "containing" a 5-mark
    alternative makes no sense structurally).

    This is NOT silently restructured (moving it to root level would need
    re-deriving intro text and marks splits) -- it's logged loudly with the
    exact path so it surfaces for review. Returns a list of description
    strings for fix-count reporting.

    Also checks: does that misnested ALTERNATIVE have an actual answer?
    (Extends the bug-6 "unanswered alternative" check beyond MCQ to any
    ALTERNATIVE with empty answer_text and no marking_scheme.)
    """
    findings = []
    q_num = q.get("question_number")
    q_data = q.get("question_data", {}) or {}
    a_data = q.get("answer_data", {}) or {}

    add_qs = q_data.get("additional_questions", []) or []
    add_as = a_data.get("additional_answers", []) or []
    ans_by_prefix = {str(a.get("answer_prefix", "")): a for a in add_as}

    for sub_q in add_qs:
        if sub_q.get("additional_question_type") != "SUB_QUESTION":
            continue
        parent_marks = sub_q.get("marks", 0) or 0
        children = sub_q.get("child_additional_questions") or []
        sub_ans = ans_by_prefix.get(str(sub_q.get("question_prefix", "")), {})
        child_ans_by_prefix = {
            str(a.get("answer_prefix", "")): a
            for a in (sub_ans.get("child_additional_answers") or [])
        }
        for child in children:
            if child.get("additional_question_type") != "ALTERNATIVE":
                continue
            child_marks = child.get("marks", 0) or 0
            if child_marks > parent_marks:
                msg = (
                    f"Q{q_num}: ALTERNATIVE '{child.get('question_prefix')}' "
                    f"(marks={child_marks}) is nested inside SUB_QUESTION "
                    f"'{sub_q.get('question_prefix')}' (marks={parent_marks}) -- "
                    f"the alternative's marks exceed its parent's, meaning it should "
                    f"almost certainly be a ROOT-level alternative instead. NOT auto-"
                    f"restructured -- needs manual review or a re-run."
                )
                logger.error(f"[flag_misnested_alternative_marks] {msg}")
                findings.append(msg)

            # Also check: does this ALTERNATIVE actually have an answer?
            child_a = child_ans_by_prefix.get(str(child.get("question_prefix", "")), {})
            text_empty = not str(child_a.get("answer_text", "")).strip()
            no_scheme = not (child_a.get("marking_scheme") or child_a.get("child_additional_answers"))
            if text_empty and no_scheme:
                msg = (
                    f"Q{q_num}: ALTERNATIVE '{child.get('question_prefix')}' under "
                    f"SUB_QUESTION '{sub_q.get('question_prefix')}' has NO answer "
                    f"(empty answer_text, no marking_scheme) -- Phase 3 skipped it."
                )
                logger.error(f"[flag_misnested_alternative_marks] {msg}")
                findings.append(msg)

    return findings


def flag_root_alternative_marks_mismatch(q: dict) -> list:
    """
    Symmetric check to flag_misnested_alternative_marks(): a root-level
    ALTERNATIVE (a direct entry in the root's own additional_questions,
    not nested under a SUB_QUESTION) must carry the SAME marks as the
    root question it replaces (rule 10 in the extractor prompt). If it
    doesn't, something went wrong in extraction -- most commonly Phase 2
    printed the wrong number, or a bracket-numbered block ("X(I)"/"X(II)")
    lost its marks value during mapping. Logged, not auto-fixed.
    """
    findings = []
    q_num = q.get("question_number")
    root_marks = q.get("marks", 0) or 0
    q_data = q.get("question_data", {}) or {}

    for entry in q_data.get("additional_questions", []) or []:
        if entry.get("additional_question_type") != "ALTERNATIVE":
            continue
        alt_marks = entry.get("marks", 0) or 0
        if alt_marks != root_marks:
            msg = (
                f"Q{q_num}: root-level ALTERNATIVE '{entry.get('question_prefix')}' "
                f"has marks={alt_marks} but the root question itself has marks="
                f"{root_marks} -- an alternative must always carry the SAME marks "
                f"as what it replaces. NOT auto-fixed -- needs manual review."
            )
            logger.error(f"[flag_root_alternative_marks_mismatch] {msg}")
            findings.append(msg)

    return findings


def ensure_non_empty_question_text(q: dict) -> bool:
    q_data = q.get("question_data", {})
    text = str(q_data.get("question_text", "")).strip()
    if text:
        return False

    add_qs = q_data.get("additional_questions") or []
    if not add_qs:
        return False

    q_data["question_text"] = "Answer the following questions."
    return True


def auto_repair(data: dict, config: dict = None) -> tuple:
    fixes = []
    total_questions = sum(len(s.get("questions", [])) for s in data.get("sections", []))
    logger.info(f"Auto-repair starting — {len(data.get('sections', []))} section(s), {total_questions} question(s).")
    for section in data.get("sections", []):
        sec_name = section.get("section_name", "?")
        for q in section.get("questions", []):
            q_num = str(q.get("question_number", ""))

            q_data = q.get("question_data", {})
            if "sub_questions" in q_data:
                q_data.pop("sub_questions", None)
                fixes.append(f"[{sec_name} Q{q_num}] Removed stray sub_questions array.")

            a_data = q.get("answer_data", {})
            if "sub_answers" in a_data:
                a_data.pop("sub_answers", None)
                fixes.append(f"[{sec_name} Q{q_num}] Removed stray sub_answers array.")

            if reconcile_sub_questions_marks(q):
                fixes.append(f"[{sec_name} Q{q_num}] Scaled sub_question marks directly under additional_questions.")

            if ensure_non_empty_question_text(q):
                fixes.append(f"[{sec_name} Q{q_num}] Filled empty root question_text with a fallback summary.")

            if flag_unanswered_root_with_alternative(q):
                fixes.append(f"[{sec_name} Q{q_num}] WARNING: root question left unanswered while its ALTERNATIVE was answered — needs manual review.")

            if strip_hallucinated_top_level_keys(q):
                fixes.append(f"[{sec_name} Q{q_num}] Removed hallucinated top-level key (e.g. 'chapter_image').")

            misnested_findings = flag_misnested_alternative_marks(q)
            for finding in misnested_findings:
                fixes.append(f"[{sec_name} Q{q_num}] WARNING: {finding}")

            root_alt_findings = flag_root_alternative_marks_mismatch(q)
            for finding in root_alt_findings:
                fixes.append(f"[{sec_name} Q{q_num}] WARNING: {finding}")

            a_data = q.get("answer_data", {})
            if isinstance(a_data, dict):
                for alt_ans in a_data.get("additional_answers", []) or []:
                    if "additional_answers" in alt_ans:
                        alt_ans["child_additional_answers"] = alt_ans.pop("additional_answers")
                        fixes.append(f"[{sec_name} Q{q_num}] Repaired nested additional_answers → child_additional_answers.")

    if fixes:
        for fix in fixes:
            logger.info(f"Auto-repair fix: {fix}")
    logger.info(f"Auto-repair complete — {len(fixes)} fix(es) applied.")
    return data, fixes
