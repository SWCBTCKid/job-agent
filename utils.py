from __future__ import annotations

from job_logging import ensure_process_logging

ensure_process_logging(__file__)

import hashlib
import re
from datetime import datetime, timezone


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def normalize_title(title: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9 ]+", " ", title.lower())
    text = re.sub(r"\b(senior|sr|staff|principal|ii|iii|iv)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def make_job_id(company: str, title: str) -> str:
    key = f"{company.strip().lower()}::{normalize_title(title)}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def make_url_id(url: str) -> str:
    """Stable ID derived from a canonical job URL — unique per posting."""
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()[:24]


def strip_html(text: str) -> str:
    no_tags = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", no_tags).strip()

