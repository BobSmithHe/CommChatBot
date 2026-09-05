from __future__ import annotations

from io import BytesIO
from pathlib import Path


def extract_text(filename: str, raw: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        import pdfplumber

        with pdfplumber.open(BytesIO(raw)) as pdf:
            return "\n\n".join(page.extract_text() or "" for page in pdf.pages)
    for encoding in ("utf-8", "gbk", "utf-16"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")
