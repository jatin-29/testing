"""
Shared helpers used across all pipeline phases.
Pure utilities only — no Langfuse / LLM / tracing side effects.
"""

import json
import os
import re


def clean_meta_field(value: str) -> str:
    if not isinstance(value, str):
        return value
    value = " ".join(value.split())
    value = re.sub(r"\s*,\s*", " and ", value)
    return value.strip()


def load_template(path: str) -> str:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Prompt file '{path}' not found.")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def fill_template(template: str, values: dict) -> str:
    prompt = template
    for key in sorted(values.keys(), key=len, reverse=True):
        value = values[key]
        if isinstance(value, (list, dict)):
            json_blob = json.dumps(value, ensure_ascii=False)
            prompt = prompt.replace(f'"${key}"', json_blob).replace(f"${key}", json_blob)
        elif value is None:
            prompt = prompt.replace(f'"${key}"', "null").replace(f"${key}", "")
        else:
            prompt = prompt.replace(f"${key}", str(value))
    return prompt


def get_chapter_lookup(config: dict) -> dict:
    lookup = {}
    for entry in config.get("chapter_json", []) or []:
        name = entry.get("chapter_name")
        cid = entry.get("chapter_id")
        if name and cid:
            lookup[name] = cid
    lookup.setdefault("General", "00000000-0000-0000-0000-000000000000")
    return lookup


def resolve_chapter_id(config: dict, chapter_name) -> tuple:
    lookup = get_chapter_lookup(config)
    name = str(chapter_name or "").strip()
    if name and name in lookup:
        return lookup[name], name
    return lookup["General"], "General"
