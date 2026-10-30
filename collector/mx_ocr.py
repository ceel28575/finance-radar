# -*- coding: utf-8 -*-
"""Optional local Chinese OCR using an installed Tesseract binary.

OCR runs on the user's computer only. If Tesseract or chi_sim is unavailable,
the original image URL is still retained and the message stays marked pending.
"""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_OCR_CHARS = 5000


def find_tesseract():
    configured = os.environ.get("TESSERACT_CMD", "").strip().strip('"')
    if configured and Path(configured).is_file():
        return configured
    found = shutil.which("tesseract")
    if found:
        return found
    if os.name == "nt":
        for candidate in (
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tesseract-OCR" / "tesseract.exe",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Tesseract-OCR" / "tesseract.exe",
        ):
            if candidate.is_file():
                return str(candidate)
    return None


def ocr_image_bytes(image_bytes, executable=None, timeout=18):
    if not isinstance(image_bytes, (bytes, bytearray)) or not image_bytes:
        return ""
    if len(image_bytes) > MAX_IMAGE_BYTES:
        return ""
    executable = executable or find_tesseract()
    if not executable:
        return ""
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            handle.write(image_bytes)
            temp_path = handle.name
        result = subprocess.run(
            [executable, temp_path, "stdout", "-l", "chi_sim", "--psm", "6"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            return ""
        text = "\n".join(line.strip() for line in result.stdout.splitlines() if line.strip())
        return text[:MAX_OCR_CHARS]
    except (OSError, subprocess.TimeoutExpired):
        return ""
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass
