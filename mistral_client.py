"""
Mistral API wrapper with rate-limit retry logic.

+ LaTeX repair layer (expanded):
  - control-char restore (backslash-f -> formfeed etc.)
  - dropped-backslash restore inside math
  - backslash-cosec -> backslash-csc  (KaTeX has no backslash-cosec — was rendering "Undefined control sequence")
  - (R)/(r) mangled into ® by the model -> restored to "(R)"
  - "◦ 2" style corruption of minus signs before digits -> "-2"
  - answers containing LaTeX commands but NO $ delimiters get their math
    runs wrapped in $...$ so the frontend actually renders them

+ Tracing: LLM transport only. Observability goes through
  langfuse_client.log_llm_generation() under the single job trace.
"""

import json
import logging
import os
import random
import re
import threading
import time

import requests
from dotenv import load_dotenv

from app.core.config import (
    INTER_CALL_JITTER,
    JSON_PARSE_MAX_ATTEMPTS,
    JSON_RETRY_TEMPERATURE,
    MAX_TOKENS,
    MISTRAL_INPUT_PRICE_PER_M_TOKENS,
    MISTRAL_OUTPUT_PRICE_PER_M_TOKENS,
    MODEL,
    RATE_LIMIT_BASE_DELAY,
    RATE_LIMIT_MAX_DELAY,
    RATE_LIMIT_MAX_RETRIES,
    TEMPERATURE,
)
from app.pipeline.langfuse_client import log_llm_generation

load_dotenv()
logger = logging.getLogger("app.mistral_client")


# ---------------------------------------------------------------------------
# Token usage tracking (unchanged)
# ---------------------------------------------------------------------------

_usage_lock = threading.Lock()
_usage_totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}


def reset_usage_tracker() -> None:
    with _usage_lock:
        _usage_totals["prompt_tokens"] = 0
        _usage_totals["completion_tokens"] = 0
        _usage_totals["total_tokens"] = 0
        _usage_totals["calls"] = 0


def get_usage_summary() -> dict:
    with _usage_lock:
        prompt = _usage_totals["prompt_tokens"]
        completion = _usage_totals["completion_tokens"]
        calls = _usage_totals["calls"]

    input_cost = (prompt / 1_000_000) * MISTRAL_INPUT_PRICE_PER_M_TOKENS
    output_cost = (completion / 1_000_000) * MISTRAL_OUTPUT_PRICE_PER_M_TOKENS

    return {
        "calls": calls,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "estimated_cost_usd": round(input_cost + output_cost, 4),
    }


def _record_usage(usage: dict) -> None:
    if not usage:
        return
    with _usage_lock:
        _usage_totals["prompt_tokens"] += usage.get("prompt_tokens", 0)
        _usage_totals["completion_tokens"] += usage.get("completion_tokens", 0)
        _usage_totals["total_tokens"] += usage.get("total_tokens", 0)
        _usage_totals["calls"] += 1


# ---------------------------------------------------------------------------
# LaTeX repair layer (runs AFTER json.loads)
# ---------------------------------------------------------------------------

# Pass 1 — control-char restore (safe to run on ALL text)
_CTRL_RESTORE = [
    (re.compile(r"\x0c(rac\b|orall\b)"), r"\\f\1"),
    (re.compile(r"\t(o\b|ext[a-z]*\b|imes\b|an\b|heta\b|riangle\b|herefore\b)"), r"\\t\1"),
    (re.compile(r"\x08(egin\b|inom\b|ar\b|eta\b|oxed\b|mod\b)"), r"\\b\1"),
    (re.compile(r"\n(eq\b|abla\b|e\b)"), r"\\n\1"),
    (re.compile(r"\r(ightarrow\b|ight\b|ho\b)"), r"\\r\1"),
]

# Pass 2 — dropped-backslash restore (run ONLY inside $...$ / $$...$$ math)
_DROPPED_CMDS = (
    "quad|qquad|cdots|cdot|ldots|dots|sqrt|dfrac|frac|pm|mp|mu\\b|angle|"
    "left|right|sin|cos|tan|cot|sec|csc|log|ln\\b|lim|sum|prod|int\\b|infty|"
    "alpha|beta|gamma|delta|epsilon|lambda|pi\\b|sigma|omega|phi|theta|rho\\b|"
    "geq|leq|neq|approx|equiv|propto|div\\b|times|mathrm|mathbf|mathit|"
    "text[a-z]*|overline|underline|vec\\b|hat\\b|bar\\b|ce\\b|degree|circ\\b|"
    "rightarrow|leftarrow|to\\b|implies|therefore|because|triangle|forall|sim\\b|parallel"
)
_DROPPED_RE = re.compile(r"(?<![\\a-zA-Z{])(" + _DROPPED_CMDS + r")")

_MATH_SEGMENT_RE = re.compile(r"(\$\$[\s\S]*?\$\$|\$[^$\n]*?\$)")

# Pass 3 — invalid / mangled commands and symbols
#   \cosec is NOT a valid KaTeX/LaTeX command -> \csc
_COSEC_RE = re.compile(r"\\cosec\b")
_COSEC_PLAIN_RE = re.compile(r"(?<![\\a-zA-Z])cosec\b")          # inside math only
#   "(R)" / "(r)" auto-typographed into ® by the model
_REG_MARK_RE = re.compile(r"\s?®")
#   minus sign corrupted into "◦" / "∘" before a number ("◦ 2" was "− 2")
_BAD_MINUS_RE = re.compile(r"[◦∘]\s?(?=\d)")

# Pass 4 — math with commands but no delimiters at all: wrap runs in $...$
_LATEX_TOKEN = r"\\[a-zA-Z]+(?:\{[^{}]*\}|\[[^\]]*\])*"
_MATH_RUN_RE = re.compile(
    _LATEX_TOKEN + r"(?:" + _LATEX_TOKEN + r"|[0-9A-Za-z^_+\-=(){}/]|\{[^{}]*\})*"
)


def _restore_dropped_in_math(match: re.Match) -> str:
    seg = match.group(0)
    seg = _DROPPED_RE.sub(r"\\\1", seg)
    seg = _COSEC_PLAIN_RE.sub(r"\\csc", seg)
    return seg


def _wrap_undelimited_math(s: str) -> str:
    """If a string contains LaTeX commands but zero $ delimiters, wrap each
    contiguous math run in $...$. Conservative: only fires when there is at
    least one backslash-command and no $ anywhere in the string."""
    if "$" in s or "\\" not in s:
        return s
    if not re.search(_LATEX_TOKEN, s):
        return s

    def repl(m: re.Match) -> str:
        run = m.group(0).strip()
        if "\\" not in run:
            return m.group(0)
        trail = ""
        while run and run[-1] in ".,;:":
            trail = run[-1] + trail
            run = run[:-1]
        return f"${run}${trail}"

    return _MATH_RUN_RE.sub(repl, s)



# Pass 5 — bare chemistry/algebra sub/superscripts outside math: C_{4}H_{8},
# BaCl_{2}, Na_{2}SO_{4}, C_xH_y, O^{2-} ... wrap each formula run in $...$.
_CHEM_UNIT = r"[A-Z][a-z]?(?:_\{[^{}]+\}|_[0-9A-Za-z])?(?:\^\{[^{}]+\}|\^[0-9+\-])?"
_CHEM_RUN_RE = re.compile(r"(?<![\\$A-Za-z_])\d{0,3}(?:" + _CHEM_UNIT + r"){1,8}(?![\w$])")


def _wrap_chem_outside_math(s: str) -> str:
    """Wrap bare chemical-formula runs (must contain _ or ^) in $...$,
    only in parts of the string NOT already inside math delimiters."""
    if "_" not in s and "^" not in s:
        return s

    def repl(m):
        run = m.group(0)
        if "_" not in run and "^" not in run:
            return run
        return f"${run}$"

    parts = _MATH_SEGMENT_RE.split(s)
    for i in range(len(parts)):
        part = parts[i] or ""
        if not (part.startswith("$") and part.endswith("$")):
            parts[i] = _CHEM_RUN_RE.sub(repl, part)
    return "".join(parts)


def repair_latex_string(s: str) -> str:
    if not s:
        return s
    # Cheap symbol fixes always run
    if "®" in s:
        s = _REG_MARK_RE.sub(" (R)", s)
    if "◦" in s or "∘" in s:
        s = _BAD_MINUS_RE.sub("-", s)
    if ("\\" not in s and "$" not in s and "_" not in s and "^" not in s
            and "\x0c" not in s and "\x08" not in s
            and "\t" not in s and "\r" not in s):
        return s
    for pat, repl in _CTRL_RESTORE:
        s = pat.sub(repl, s)
    s = _COSEC_RE.sub(r"\\csc", s)
    s = _wrap_undelimited_math(s)
    s = _MATH_SEGMENT_RE.sub(_restore_dropped_in_math, s)
    s = _COSEC_RE.sub(r"\\csc", s)   # \cos + ec produced by dropped-restore
    s = _wrap_chem_outside_math(s)
    return s


def repair_latex_deep(obj):
    if isinstance(obj, str):
        return repair_latex_string(obj)
    if isinstance(obj, list):
        return [repair_latex_deep(x) for x in obj]
    if isinstance(obj, dict):
        return {k: repair_latex_deep(v) for k, v in obj.items()}
    return obj


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

_JSON_RETRY_HINT = (
    "\n\n---\nIMPORTANT CORRECTION: Your previous reply was NOT valid JSON "
    "(parser error). Respond again with ONLY valid JSON for the same request. "
    "Rules: escape every backslash in LaTeX as \\\\ (example: \\\\frac{1}{2}, not \\frac); "
    "escape every double-quote inside strings as \\\"; "
    "do not use markdown fences; do not add commentary outside the JSON."
)


def strip_json_fences(raw: str) -> str:
    raw = (raw or "").strip()
    match = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", raw)
    return match.group(1).strip() if match else raw


def sanitize_raw_response_text(text: str) -> str:
    if not text:
        return text
    return text.replace("\r\n", "\n")


def _extract_json_blob(text: str) -> str:
    """Best-effort extract of the outermost JSON object/array from model output."""
    text = strip_json_fences(text)
    if not text:
        return text
    starts = [(text.find("["), "["), (text.find("{"), "{")]
    starts = [(i, ch) for i, ch in starts if i != -1]
    if not starts:
        return text
    start, opener = min(starts, key=lambda x: x[0])
    closer = "]" if opener == "[" else "}"
    end = text.rfind(closer)
    if end != -1 and end > start:
        return text[start : end + 1]
    return text[start:]


def loads_json_lenient(raw: str):
    """
    Parse model JSON with light recovery:
      1) strip fences
      2) direct json.loads
      3) extract outermost [..] / {..} and retry
    """
    cleaned = strip_json_fences(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        blob = _extract_json_blob(raw)
        if blob != cleaned:
            return json.loads(blob)
        raise


def call_mistral(
    prompt: str,
    system_prompt: str,
    label: str,
    langfuse_prompt=None,
    temperature: float = None,
    user_suffix: str = "",
) -> str:
    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        raise ValueError("MISTRAL_API_KEY not found in environment.")

    call_start_time = time.time()
    call_temperature = TEMPERATURE if temperature is None else temperature
    user_content = prompt + (user_suffix or "")

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "response_format": {"type": "json_object"},
        "temperature": call_temperature,
        "max_tokens": MAX_TOKENS,
    }

    time.sleep(random.uniform(*INTER_CALL_JITTER))

    for rl_attempt in range(1, RATE_LIMIT_MAX_RETRIES + 1):
        logger.info(
            f"[{label}] Mistral API call — attempt {rl_attempt}/{RATE_LIMIT_MAX_RETRIES}, "
            f"model={MODEL}, temperature={call_temperature}"
        )
        try:
            response = requests.post(
                "https://api.mistral.ai/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=300,
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            if rl_attempt < RATE_LIMIT_MAX_RETRIES:
                wait = (
                    min(RATE_LIMIT_BASE_DELAY * (2 ** (rl_attempt - 1)), RATE_LIMIT_MAX_DELAY)
                    + random.uniform(0, 1.0)
                )
                logger.warning(f"[{label}] Network error ({type(exc).__name__}), retrying in {wait:.1f}s...")
                time.sleep(wait)
                continue
            logger.error(f"[{label}] Network error on final attempt — giving up.")
            raise

        if not response.ok:
            logger.error(f"[{label}] HTTP {response.status_code} response body: {response.text[:2000]}")

        is_retryable = response.status_code == 429 or 500 <= response.status_code < 600
        if is_retryable and rl_attempt < RATE_LIMIT_MAX_RETRIES:
            retry_after = response.headers.get("Retry-After")
            wait = (
                float(retry_after)
                if retry_after
                else RATE_LIMIT_BASE_DELAY * (2 ** (rl_attempt - 1))
            )
            wait = min(wait, RATE_LIMIT_MAX_DELAY) + random.uniform(0, 1.0)
            logger.warning(f"[{label}] HTTP {response.status_code} — retrying in {wait:.1f}s...")
            time.sleep(wait)
            continue

        response.raise_for_status()
        response_json = response.json()
        raw_content = response_json["choices"][0]["message"]["content"]

        usage = response_json.get("usage", {})
        _record_usage(usage)
        cost_md = log_llm_generation(
            label=label,
            start_time=call_start_time,
            end_time=time.time(),
            model=MODEL,
            input_messages=payload["messages"],
            output=raw_content,
            usage=usage,
            langfuse_prompt=langfuse_prompt,
        )
        logger.info(
            f"[{label}] Response received — {len(raw_content)} chars. "
            f"Tokens: prompt={cost_md.get('prompt_tokens')}, "
            f"completion={cost_md.get('completion_tokens')}, "
            f"total={cost_md.get('total_tokens')}, "
            f"cost=${cost_md.get('estimated_cost_usd')}."
        )

        return sanitize_raw_response_text(raw_content)


def call_mistral_with_retries(
    prompt: str,
    system_prompt: str,
    label: str,
    max_attempts: int = None,
    expect_array: bool = False,
    langfuse_prompt=None,
) -> object:
    """
    Call Mistral and parse JSON. On JSON / shape failure, re-call the SAME request
    with a repair hint and slightly higher temperature so the model does not
    repeat the identical broken payload.
    """
    attempts = max_attempts if max_attempts is not None else JSON_PARSE_MAX_ATTEMPTS
    last_error = None

    for attempt in range(1, attempts + 1):
        is_retry = attempt > 1
        raw = call_mistral(
            prompt,
            system_prompt,
            label,
            langfuse_prompt=langfuse_prompt,
            temperature=JSON_RETRY_TEMPERATURE if is_retry else TEMPERATURE,
            user_suffix=_JSON_RETRY_HINT if is_retry else "",
        )
        try:
            parsed = loads_json_lenient(raw)
            parsed = repair_latex_deep(parsed)
        except json.JSONDecodeError as e:
            last_error = e
            snippet = (raw or "")[:400].replace("\n", "\\n")
            if attempt == attempts:
                logger.error(
                    f"[{label}] JSON parse failed on all {attempts} attempts: {e}. "
                    f"Snippet: {snippet!r}"
                )
                raise RuntimeError(
                    f"[{label}] Failed to parse JSON after {attempts} attempts: {e}"
                )
            wait = min(RATE_LIMIT_BASE_DELAY * (2 ** (attempt - 1)), 8.0) + random.uniform(0, 0.5)
            logger.warning(
                f"[{label}] JSON parse failed (attempt {attempt}/{attempts}): {e}. "
                f"Re-calling same prompt in {wait:.1f}s. Snippet: {snippet!r}"
            )
            time.sleep(wait)
            continue

        if not expect_array:
            return parsed

        if isinstance(parsed, list):
            logger.info(f"[{label}] JSON array parsed — {len(parsed)} item(s).")
            return parsed
        if isinstance(parsed, dict):
            for key in ("answers", "data", "questions", "results", "items"):
                if isinstance(parsed.get(key), list):
                    logger.info(
                        f"[{label}] JSON array extracted from key '{key}' — "
                        f"{len(parsed[key])} item(s)."
                    )
                    return parsed[key]
            for v in parsed.values():
                if isinstance(v, list):
                    logger.info(
                        f"[{label}] JSON array found in response values — {len(v)} item(s)."
                    )
                    return v

            last_error = RuntimeError(f"Expected JSON array, got keys: {list(parsed.keys())}")
            if attempt == attempts:
                logger.error(f"[{label}] {last_error}")
                raise RuntimeError(f"[{label}] {last_error}")
            wait = min(RATE_LIMIT_BASE_DELAY * (2 ** (attempt - 1)), 8.0) + random.uniform(0, 0.5)
            logger.warning(
                f"[{label}] Expected array, got object (attempt {attempt}/{attempts}), "
                f"re-calling same prompt in {wait:.1f}s..."
            )
            time.sleep(wait)
            continue

        last_error = RuntimeError(f"Expected JSON array, got {type(parsed).__name__}")
        if attempt == attempts:
            raise RuntimeError(f"[{label}] {last_error}")
        logger.warning(f"[{label}] {last_error} (attempt {attempt}/{attempts}), retrying...")
        time.sleep(1.0)

    raise RuntimeError(f"[{label}] Failed after {attempts} attempts: {last_error}")
