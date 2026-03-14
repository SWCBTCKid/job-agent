from __future__ import annotations

from job_logging import ensure_process_logging

ensure_process_logging(__file__)

import json
import urllib.parse
import urllib.request
from datetime import datetime
import logging

from models import JobPosting

LOGGER = logging.getLogger(__name__)


def format_digest(postings: list[JobPosting]) -> str:
    stamp = datetime.utcnow().strftime("%Y-%m-%d")
    lines = [f"Job Digest - {stamp} - {len(postings)} matches", ""]
    for i, p in enumerate(postings, start=1):
        salary = "Unknown"
        if p.salary_min and p.salary_max:
            salary = f"${p.salary_min:,}-${p.salary_max:,}"
        elif p.salary_min:
            salary = f"From ${p.salary_min:,}"

        lines.extend(
            [
                f"{i}. {p.company} - {p.title}",
                f"   Tier {p.tier or '?'} | {salary} | {p.age_days} days old | {p.source}",
                f"   Match: {p.match_reason or 'N/A'}",
                f"   Risk: {p.risk or 'N/A'}",
                f"   Competition: {p.competition}",
                f"   Apply: {p.url}",
                "",
            ]
        )
    return "\n".join(lines).strip()


def _split_chunks(text: str, limit: int = 4000) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        if size + len(line) > limit and current:
            chunks.append("".join(current).rstrip())
            current = [line]
            size = len(line)
        else:
            current.append(line)
            size += len(line)
    if current:
        chunks.append("".join(current).rstrip())
    return chunks


def send_telegram(token: str, chat_id: str, text: str) -> None:
    if not token or not chat_id:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chunk in _split_chunks(text, 4000):
        payload = urllib.parse.urlencode({"chat_id": chat_id, "text": chunk}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
        parsed = json.loads(body)
        if not parsed.get("ok"):
            raise RuntimeError(f"Telegram send failed: {body}")


def send_telegram_safe(token: str, chat_id: str, text: str) -> None:
    """Best-effort Telegram send that logs failures instead of failing silently."""
    try:
        send_telegram(token, chat_id, text)
    except Exception as exc:
        LOGGER.exception("Telegram send failed: %s", exc)

