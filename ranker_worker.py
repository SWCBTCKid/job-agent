from __future__ import annotations

from job_logging import ensure_process_logging

ensure_process_logging(__file__)

"""
ranker_worker.py — Background Claude Sonnet final ranker.

Reads all postings above Stage 1 threshold for a given resume_id,
calls Claude Sonnet for each one, writes claude_score + metadata back
to the resume postings table, emits output/results_{resume_id}_{ts}.json,
and sends a Telegram notification when done.

Usage (launched by main.py as subprocess):
    python ranker_worker.py --resume-id <id> --profile-id <id>
"""

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from config import DATA_DIR, SETTINGS, STATE_DIR
from db import JobDB
from models import JobPosting
from notifier import send_telegram_safe

LOGGER = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"

SONNET_MODEL = SETTINGS.claude_model
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
VALID_LEVELS = {"senior", "mid", "staff", "too_junior", "too_senior"}

_SYSTEM = (
    "You are a senior technical recruiter and engineer. "
    "Score job postings against the provided resume. "
    "Output JSON only. No prose outside the JSON object."
)

_USER_TEMPLATE = """\
RESUME:
{resume_text}

JOB POSTING:
Company: {company}
Title: {title}
Location: {location}
Salary: {salary}
Description:
{description}

Output exactly this JSON object (no markdown, no extra keys):
{{
  "claude_score": <float 0.0-10.0>,
  "match_reason": "<2–3 sentences: what aligns, what gaps exist, overall verdict>",
  "level_fit": "<senior|mid|staff|too_junior|too_senior>",
  "risk": "<low|medium|high>",
  "tier": <1, 2, or 3>
}}

Scoring guide (claude_score):
8–10  Direct match — skills, domain, seniority all align. Prioritise immediately.
6–7   Strong match — worth applying. 1–2 manageable gaps.
4–5   Partial match — apply only if volume is low. Notable mismatch.
0–3   Poor match — wrong domain, wrong seniority, or missing critical skills.

level_fit:
  senior       = senior IC role, right fit for candidate
  mid          = mid-level, candidate slightly overqualified
  staff        = staff/principal — likely too senior for candidate
  too_junior   = junior/intern/entry-level
  too_senior   = director/VP/C-level/manager

risk:
  low    = straightforward fit, no red flags
  medium = some uncertainty (new domain, partial skill overlap, location)
  high   = significant mismatch or unknown factor (equity-heavy comp, very early stage, niche domain)

tier:
  1 = security / observability / SRE / safety-critical / autonomous systems / production engineering
  2 = platform / infrastructure / backend / distributed systems / fleet / embedded
  3 = ML infrastructure, data engineering, or unclear domain
"""


def _format_salary(posting: JobPosting) -> str:
    if posting.salary_min and posting.salary_max:
        return f"${posting.salary_min:,} – ${posting.salary_max:,}"
    if posting.salary_min:
        return f"${posting.salary_min:,}+"
    if posting.salary_max:
        return f"up to ${posting.salary_max:,}"
    return "not specified"


def _build_prompt(resume_text: str, posting: JobPosting) -> str:
    return _USER_TEMPLATE.format(
        resume_text=resume_text[:6000],
        company=posting.company,
        title=posting.title,
        location=posting.location or "not specified",
        salary=_format_salary(posting),
        description=(posting.description or "")[:4000],
    )


def _parse_response(text: str) -> dict:
    text = re.sub(r"```(?:json)?\s*", "", text).strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError(f"No JSON in Claude response: {text[:200]}")
    data = json.loads(m.group(0))

    try:
        score = max(0.0, min(10.0, float(data.get("claude_score", 0))))
    except (TypeError, ValueError):
        score = 0.0

    level = str(data.get("level_fit", "mid")).strip().lower()
    if level not in VALID_LEVELS:
        level = "mid"

    try:
        tier = int(data.get("tier", 2))
    except (TypeError, ValueError):
        tier = 2
    if tier not in {1, 2, 3}:
        tier = 2

    risk = str(data.get("risk", "medium")).strip().lower()
    if risk not in {"low", "medium", "high"}:
        risk = "medium"

    reason = str(data.get("match_reason", "")).strip() or "No reasoning provided."

    return {
        "claude_score": score,
        "match_reason": reason,
        "level_fit": level,
        "risk": risk,
        "tier": tier,
    }


async def _call_claude(
    client: httpx.AsyncClient,
    api_key: str,
    resume_text: str,
    posting: JobPosting,
    max_retries: int = 4,
) -> dict | None:
    body = {
        "model": SONNET_MODEL,
        "max_tokens": 400,
        "temperature": 0,
        "system": _SYSTEM,
        "messages": [{"role": "user", "content": _build_prompt(resume_text, posting)}],
    }
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    for attempt in range(max_retries):
        try:
            resp = await client.post(ANTHROPIC_URL, headers=headers, json=body, timeout=60.0)
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("retry-after", 2 ** (attempt + 1)))
                LOGGER.debug("429 on %s/%s, sleeping %.1fs", posting.company, posting.title, retry_after)
                await asyncio.sleep(retry_after)
                continue
            if resp.status_code >= 400:
                LOGGER.warning(
                    "Claude HTTP %d for %s — %s (skipping)",
                    resp.status_code, posting.company, posting.title,
                )
                return None
            raw = resp.json()
            content = "\n".join(
                part.get("text", "")
                for part in raw.get("content", [])
                if part.get("type") == "text"
            )
            return _parse_response(content)
        except Exception as exc:
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            LOGGER.warning("Claude call failed for %s — %s: %s", posting.company, posting.title, exc)
            return None
    LOGGER.warning("Claude exhausted retries for %s — %s", posting.company, posting.title)
    return None


def _load_resume_text(db: JobDB, resume_id: str) -> str:
    """Load resume text from stored path, or fall back to data/resume.txt."""
    record = db.get_resume(resume_id)
    if record and record.path:
        p = Path(record.path)
        if p.exists():
            return p.read_text(encoding="utf-8")
    fallback = DATA_DIR / "resume.txt"
    if fallback.exists():
        return fallback.read_text(encoding="utf-8")
    return ""


def _write_output(resume_id: str, results: list[dict]) -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = OUTPUT_DIR / f"results_{resume_id}_{ts}.json"
    out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    return out_path


async def run_ranker(resume_id: str, profile_id: str, rpm: int = 20, limit: int = 0) -> None:
    api_key = SETTINGS.anthropic_api_key
    if not api_key:
        LOGGER.error("ANTHROPIC_API_KEY not set — ranker_worker cannot run")
        sys.exit(1)

    db = JobDB(STATE_DIR / "jobs.db")
    resume_text = _load_resume_text(db, resume_id)
    if not resume_text:
        LOGGER.error("No resume text found for resume_id=%s", resume_id)
        send_telegram_safe(
            SETTINGS.telegram_bot_token,
            SETTINGS.telegram_chat_id,
            f"Ranker ERROR: no resume text for {resume_id}",
        )
        db.close()
        sys.exit(1)

    try:
        candidates = db.get_above_threshold(resume_id, SETTINGS.stage1_threshold)
        total = len(candidates)
        if limit > 0:
            candidates = candidates[:limit]
            total = len(candidates)
            LOGGER.info("ranker_worker: --limit %d applied", limit)

        LOGGER.info("ranker_worker: %d candidates for resume=%s at %d RPM", total, resume_id, rpm)

        if total == 0:
            LOGGER.warning("No candidates above threshold — nothing to rank")
            send_telegram_safe(
                SETTINGS.telegram_bot_token,
                SETTINGS.telegram_chat_id,
                f"Job ranker: 0 candidates above threshold for resume {resume_id}. Nothing to rank.",
            )
            db.close()
            return

        scored = 0
        failed = 0
        batch: list[dict] = []
        delay = 60.0 / rpm

        async with httpx.AsyncClient() as client:
            for i, posting in enumerate(candidates):
                t0 = time.monotonic()

                result = await _call_claude(client, api_key, resume_text, posting)

                if result is None:
                    failed += 1
                else:
                    batch.append({
                        "id": posting.id,
                        "claude_score": result["claude_score"],
                        "claude_reason": result["match_reason"],
                        "match_reason": result["match_reason"],
                        "level_fit": result["level_fit"],
                        "tier": result["tier"],
                        # risk stored in claude_reason until schema adds a risk column
                    })
                    scored += 1

                done = i + 1
                if done % 25 == 0 or done == total:
                    pct = done / total * 100
                    LOGGER.info(
                        "[%d/%d %.0f%%] scored=%d failed=%d",
                        done, total, pct, scored, failed,
                    )
                    # Flush batch to DB
                    if batch:
                        db.write_claude_scores(resume_id, batch)
                        batch = []

                elapsed = time.monotonic() - t0
                sleep_for = max(0.0, delay - elapsed)
                if sleep_for > 0 and done < total:
                    await asyncio.sleep(sleep_for)

        # Final flush
        if batch:
            db.write_claude_scores(resume_id, batch)

        # Write output file
        top = db.get_top_n_by_claude(resume_id, SETTINGS.top_n_output)
        ranked = len(top)

        # Add rank field and risk (stored in claude_reason prefix workaround not needed —
        # risk is included in match_reason by the prompt)
        for rank, row in enumerate(top, 1):
            row["rank"] = rank

        out_path = _write_output(resume_id, top)
        LOGGER.info("Output written: %s (%d results)", out_path, ranked)

        # Telegram notification
        top1 = top[0] if top else None
        if top1:
            tg_msg = (
                f"Job ranking complete ({resume_id})\n"
                f"Ranked: {ranked}  |  Failed: {failed}\n"
                f"Top match: {top1.get('title')} @ {top1.get('company')} "
                f"(score {top1.get('claude_score', 0):.1f}/10)\n"
                f"Output: {out_path.name}"
            )
        else:
            tg_msg = (
                f"Job ranking complete ({resume_id})\n"
                f"Ranked: {ranked}  |  Failed: {failed}\n"
                f"No results above threshold."
            )

        send_telegram_safe(
            SETTINGS.telegram_bot_token,
            SETTINGS.telegram_chat_id,
            tg_msg,
        )
        LOGGER.info("ranker_worker done: resume=%s ranked=%d failed=%d", resume_id, ranked, failed)

    except Exception as exc:
        LOGGER.exception("ranker_worker crashed: %s", exc)
        send_telegram_safe(
            SETTINGS.telegram_bot_token,
            SETTINGS.telegram_chat_id,
            f"Job ranker ERROR for {resume_id}:\n{exc}",
        )
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Background Claude Sonnet job ranker")
    parser.add_argument("--resume-id", required=True, help="Resume content hash (12-char)")
    parser.add_argument("--profile-id", required=True, help="Search profile ID")
    parser.add_argument("--rpm", type=int, default=20, help="Max requests per minute (default 20)")
    parser.add_argument("--limit", type=int, default=0, help="Only rank first N (0=all, for testing)")
    args = parser.parse_args()

    asyncio.run(run_ranker(args.resume_id, args.profile_id, rpm=args.rpm, limit=args.limit))


if __name__ == "__main__":
    main()
