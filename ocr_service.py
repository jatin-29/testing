"""
OCR service — converts a PDF file to cleaned Markdown and extracts embedded images.

TABLE FIX: flattening is now PER-ROW, not per-block. CBSE papers wrap the
entire paper in one big |Q.No|Question|Marks| table, so the old block-level
decision linearized genuine data tables (frequency distributions etc.) that
sit inside that same contiguous block. Now: rows containing question markers
are linearized; pure data rows are re-emitted verbatim as pipe-table rows so
they survive into question_text as real Markdown tables.
"""

import base64
import logging
import re
from pathlib import Path

from dotenv import load_dotenv
from mistralai.client import Mistral
import os

load_dotenv()
logger = logging.getLogger("app.ocr_service")

OCR_MODEL = "mistral-ocr-latest"


def encode_pdf_base64(pdf_path: Path) -> str:
    with open(pdf_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def run_ocr(pdf_path: Path):
    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        raise ValueError("MISTRAL_API_KEY not found in environment / .env file.")

    logger.info(f"Running OCR on '{pdf_path.name}' using model={OCR_MODEL}...")
    client = Mistral(api_key=api_key)
    b64_pdf = encode_pdf_base64(pdf_path)

    ocr_response = client.ocr.process(
        model=OCR_MODEL,
        document={
            "type": "document_url",
            "document_url": f"data:application/pdf;base64,{b64_pdf}",
        },
        include_image_base64=True,
    )
    page_count = len(ocr_response.pages) if ocr_response.pages else 0
    logger.info(f"OCR complete for '{pdf_path.name}' — {page_count} page(s) returned.")
    return ocr_response


def save_images(pages, images_dir: Path) -> int:
    images_dir.mkdir(parents=True, exist_ok=True)
    count = 0

    for page in pages:
        page_images = getattr(page, "images", None) or []
        for img in page_images:
            img_id = getattr(img, "id", None) or f"img-{count}.jpeg"
            img_b64 = getattr(img, "image_base64", None)

            if not img_b64:
                logger.warning(f"Skipping image '{img_id}' — no base64 data returned.")
                continue

            if img_b64.startswith("data:"):
                img_b64 = img_b64.split(",", 1)[-1]

            filename = img_id if "." in img_id else f"{img_id}.jpeg"
            out_path = images_dir / filename

            try:
                with open(out_path, "wb") as f:
                    f.write(base64.b64decode(img_b64))
                count += 1
                logger.debug(f"Saved image '{filename}'.")
            except Exception as e:
                logger.warning(f"Failed to decode/save image '{img_id}': {e}")

    logger.info(f"Image extraction complete — {count} image(s) saved to '{images_dir}'.")
    return count


# Row contains a question number / sub-part marker -> it's a wrapper row
_QUESTION_CELL_RE = re.compile(
    r"(?:^|\s)(?:Q\s*\.?\s*\d+|\d+\s*[\.\)]|\([ivx]+\)|\([a-d]\))", re.IGNORECASE
)
# Row that looks like pure data: numbers / ranges / short labels only
_DIVIDER_ROW_RE = re.compile(r"^\|[\s\-:|]+\|$")


def _linearize_row(line: str) -> str:
    cells = [c.strip() for c in line.strip().split("|")[1:-1]]
    row_text = " ".join(c for c in cells if c)
    return row_text.replace("![img", " ![img")


def _row_is_data(line: str) -> bool:
    """A row is 'data' if none of its cells contain a question marker."""
    joined = " ".join(line.strip().split("|")[1:-1])
    return not _QUESTION_CELL_RE.search(joined)


def flatten_markdown_tables(md_text: str) -> str:
    """
    PER-ROW selective table flattening.

    - Wrapper rows (containing question numbers / sub-part markers) are
      linearized so questions and image refs inside cells survive.
    - Contiguous runs of pure DATA rows within the same block are kept as a
      proper Markdown pipe table (with a synthesized divider if the original
      one was consumed) so frequency/observation tables reach question_text
      intact.
    - Blocks with no question markers at all are kept verbatim (old behaviour).
    """
    lines = md_text.split("\n")
    out = []
    i = 0
    n = len(lines)

    while i < n:
        stripped = lines[i].strip()

        if not (stripped.startswith("|") and stripped.endswith("|")):
            out.append(lines[i].replace("![img", " ![img"))
            i += 1
            continue

        # Collect the whole contiguous table block
        block = []
        while i < n:
            s = lines[i].strip()
            if s.startswith("|") and s.endswith("|"):
                block.append(lines[i])
                i += 1
            else:
                break

        block_text = "\n".join(block)
        has_questions = bool(_QUESTION_CELL_RE.search(block_text.replace("|", " ")))

        if not has_questions:
            # Genuine standalone data table — keep verbatim
            out.append(block_text.replace("![img", " ![img"))
            continue

        # Mixed wrapper block: per-row handling
        j = 0
        while j < len(block):
            line = block[j]
            s = line.strip()
            if _DIVIDER_ROW_RE.match(s):
                j += 1
                continue

            if _row_is_data(s):
                # Start of an embedded data table — collect the run
                run = []
                while j < len(block):
                    s2 = block[j].strip()
                    if _DIVIDER_ROW_RE.match(s2):
                        j += 1
                        continue
                    if _row_is_data(s2):
                        run.append(block[j].replace("![img", " ![img"))
                        j += 1
                    else:
                        break
                if len(run) >= 2:
                    # Re-emit as a proper table: header + divider + body
                    n_cols = run[0].strip().count("|") - 1
                    divider = "|" + "|".join([" --- "] * max(n_cols, 1)) + "|"
                    out.append(run[0])
                    out.append(divider)
                    out.extend(run[1:])
                else:
                    # Single stray data row — safer to linearize
                    out.extend(_linearize_row(r) for r in run)
            else:
                out.append(_linearize_row(line))
                j += 1

    return "\n".join(out)


def build_combined_markdown(pages) -> str:
    parts = []
    for page in pages:
        idx = getattr(page, "index", "?")
        md = getattr(page, "markdown", "") or ""
        parts.append(f"<!-- page {idx} -->\n{md}")
    return "\n\n".join(parts)