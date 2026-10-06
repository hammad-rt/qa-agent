"""HTML to PDF with headless Chrome/Chromium (no extra Python dependency). Returns None if no browser is found."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

BROWSERS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")


def find_browser() -> str | None:
    return next((p for b in BROWSERS if (p := shutil.which(b))), None)


def html_to_pdf(html: Path, pdf: Path | None = None, browser: str | None = None, run=subprocess.run) -> Path | None:
    browser = browser or find_browser()
    if not browser:
        return None
    pdf = pdf or html.with_suffix(".pdf")
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-pdf-header-footer",
           f"--print-to-pdf={pdf}", html.resolve().as_uri()]
    try:
        run(cmd, check=True, capture_output=True, timeout=120)
    except (subprocess.SubprocessError, OSError):
        return None
    return pdf if pdf.exists() and pdf.stat().st_size > 0 else None
