"""
Image upload service — pushes locally-extracted OCR images to the internal
bucket and returns an img_id → hosted_url mapping.
Logic preserved exactly from upload_images.py.
"""

import logging
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("app.upload_service")

UPLOAD_URL = "https://stageapi.aichanakya.in/v0/internal/upload-file/"


def upload_single_image(image_path: Path, api_token: str) -> str | None:
    """Upload one image file and return its hosted URL (or None on failure)."""
    logger.debug(f"Uploading '{image_path.name}'...")
    headers = {"Authorization": f"Bearer {api_token}"}

    with open(image_path, "rb") as f:
        files = {"file": (image_path.name, f, "image/jpeg")}
        resp = requests.post(UPLOAD_URL, headers=headers, files=files)

    if resp.status_code != 200:
        logger.warning(f"Failed to upload '{image_path.name}': HTTP {resp.status_code} — {resp.text}")
        return None

    data = resp.json()
    url = data.get("url") or data.get("file_url") or data.get("data", {}).get("url")

    if not url:
        logger.warning(f"Upload succeeded for '{image_path.name}' but no URL in response: {data}")
        return None

    logger.debug(f"Uploaded '{image_path.name}' -> {url}")
    return url


def upload_images_dir(images_dir: Path, api_token: str) -> dict:
    """
    Upload every image in images_dir to the internal bucket.
    Returns a dict mapping img_id (filename stem, e.g. 'img-0') → hosted URL.
    """
    mapping = {}

    image_files = (
        sorted(images_dir.glob("*.jpeg"))
        + sorted(images_dir.glob("*.jpg"))
        + sorted(images_dir.glob("*.png"))
    )

    if not image_files:
        logger.info(f"No images found in '{images_dir}' — skipping upload.")
        return mapping

    logger.info(f"Uploading {len(image_files)} image(s) from '{images_dir}'...")
    for img_path in image_files:
        url = upload_single_image(img_path, api_token)
        if url:
            img_id = img_path.stem  # e.g. "img-0" from "img-0.jpeg"
            mapping[img_id] = url

    success = len(mapping)
    failed = len(image_files) - success
    logger.info(f"Upload complete — {success} succeeded, {failed} failed.")
    return mapping
