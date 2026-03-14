from __future__ import annotations

from job_logging import ensure_process_logging

ensure_process_logging(__file__)

"""
Background Haiku 4.5 scorer — runs against the entire unscored DB.

Usage:
    python scorer_worker.py [--rpm 50] [--dry-run]

Rate limiting: processes in batches of `rpm` concurrent requests, then sleeps
until 60 seconds have elapsed since the batch started. This guarantees <= rpm
requests per minute regardless of API latency.
"""

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from pathlib import Path

import httpx

from config import DATA_DIR, SETTINGS, STATE_DIR, load_resume_text
from db import JobDB
from models import JobPosting

LOGGER = logging.getLogger(__name__)

HAIKU_MODEL = "claude-haiku-4-5-20251001"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
VALID_LEVELS = {"senior", "mid", "staff", "too_junior", "too_senior"}

_SYSTEM = "You are a job-fit scorer. Output JSON only. No prose outside the JSON."

_USER_TEMPLATE = """\
RESUME:
{resume_text}

JOB:
Company: {company}
Title: {title}
Location: {location}
Salary: {salary}
Description:
{description}

Output exactly this JSON object (no markdown, no extra keys):
{{
  "match_score": <integer 0-100>,
  "match_reason": "<2 sentences max: strongest alignment first, then biggest gap>",
  "level_fit": "<senior|mid|staff|too_junior|too_senior>",
  "tier": <1, 2, or 3>,
  "reject": <true or false>
}}

Scoring guide:
90-100  Core skills match exactly, right domain, right seniority level
70-89   Strong overlap, 1-2 minor gaps (missing a tool, adjacent domain)
50-69   Relevant background but notable mismatch (wrong domain or some gaps)
30-49   Tangential — candidate could do it but it is a real stretch
0-29    Wrong role type, wrong domain, or obviously wrong seniority

Tier guide:
1 = security / observability / SRE / production engineering / safety-critical / autonomous systems
2 = platform / infrastructure / backend / embedded / fleet / distributed systems
3 = ML infrastructure, data engineering, or unclear

level_fit guide:
senior       = senior IC role, good fit for candidate's experience level
mid          = mid-level IC role, candidate slightly overqualified but doable
staff        = staff / principal / distinguished — too senior for candidate
too_junior   = junior / new grad / entry-level / intern
too_senior   = director / VP / C-level / manager

Set reject=true if ANY of: management/director/VP/C-level, sales/recruiting/
finance/legal/marketing, intern/junior/entry-level, requires PhD,
pure ML research with no infrastructure component.
"""


def _format_salary(posting: JobPosting) -> str:
    if posting.salary_min and posting.salary_max:
        return f"${posting.salary_min:,} - ${posting.salary_max:,}"
    if posting.salary_min:
        return f"${posting.salary_min:,}+"
    if posting.salary_max:
        return f"up to ${posting.salary_max:,}"
    return "not specified"


def _build_prompt(resume_text: str, posting: JobPosting) -> str:
    return _USER_TEMPLATE.format(
        resume_text=resume_text,
        company=posting.company,
        title=posting.title,
        location=posting.location or "not specified",
        salary=_format_salary(posting),
        description=(posting.description or "")[:3000],
    )


def _parse_response(text: str) -> dict:
    """Extract and validate JSON from Haiku response."""
    # Strip markdown code fences if present
    text = re.sub(r"```(?:json)?\s*", "", text).strip()
    # Find first { ... } block
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError(f"No JSON object in response: {text[:200]}")
    data = json.loads(m.group(0))

    # Coerce and validate fields
    try:
        score = max(0, min(100, int(float(data.get("match_score", 0)))))
    except (TypeError, ValueError):
        score = 0

    level = str(data.get("level_fit", "mid")).strip().lower()
    if level not in VALID_LEVELS:
        level = "mid"

    try:
        tier = int(data.get("tier", 2))
    except (TypeError, ValueError):
        tier = 2
    if tier not in {1, 2, 3}:
        tier = 2

    reject = bool(data.get("reject", False))
    reason = str(data.get("match_reason", "")).strip() or "No reasoning provided."

    return {
        "match_score": score,
        "match_reason": reason,
        "level_fit": level,
        "tier": tier,
        "reject": reject,
    }


async def _call_haiku(
    client: httpx.AsyncClient,
    api_key: str,
    resume_text: str,
    posting: JobPosting,
    max_retries: int = 4,
) -> dict | None:
    """Call Haiku for a single posting. Retries on 429 with exponential backoff."""
    body = {
        "model": HAIKU_MODEL,
        "max_tokens": 300,
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
            resp = await client.post(ANTHROPIC_URL, headers=headers, json=body, timeout=30.0)
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("retry-after", 2 ** (attempt + 1)))
                LOGGER.debug("429 on %s, sleeping %.1fs", posting.title, retry_after)
                await asyncio.sleep(retry_after)
                continue
            if resp.status_code >= 400:
                LOGGER.warning(
                    "Haiku HTTP %d for %s — %s (skipping)",
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
            LOGGER.warning("Haiku call failed for %s — %s: %s", posting.company, posting.title, exc)
            return None
    LOGGER.warning("Haiku call exhausted retries for %s — %s", posting.company, posting.title)
    return None


def _apply_result(posting: JobPosting, result: dict) -> None:
    """Write Haiku scores back onto the posting object in place."""
    posting.match_score = result["match_score"] / 10.0  # store as 0-10 to match existing schema
    posting.match_reason = result["match_reason"]
    posting.level_fit = result["level_fit"]
    posting.tier = result["tier"]
    if result["reject"]:
        posting.match_score = 0.0
        posting.reason_codes.append("HAIKU_REJECT")


async def run_scorer(rpm: int = 20, dry_run: bool = False) -> dict:
    api_key = SETTINGS.anthropic_api_key
    if not api_key:
        LOGGER.error("ANTHROPIC_API_KEY not set — scorer_worker cannot run")
        sys.exit(1)

    resume_text = load_resume_text()
    db = JobDB(STATE_DIR / "jobs.db")

    try:
        jobs = db.get_unscored()
        total = len(jobs)
        LOGGER.info("scorer_worker: %d unscored jobs to process at %d RPM", total, rpm)
        print(f"Scoring {total} jobs at {rpm} RPM (~{total / rpm:.0f} min)")

        scored = 0
        rejected = 0
        failed = 0
        delay = 60.0 / rpm  # seconds between requests e.g. 50 RPM → 1.2s

        async with httpx.AsyncClient() as client:
            for i, job in enumerate(jobs):
                t0 = time.monotonic()

                result = await _call_haiku(client, api_key, resume_text, job)

                if result is None:
                    failed += 1
                else:
                    _apply_result(job, result)
                    if not dry_run:
                        db.mark_scored(job)
                    if result["reject"]:
                        rejected += 1
                    else:
                        scored += 1

                done = i + 1
                if done % 50 == 0 or done == total:
                    pct = done / total * 100
                    print(
                        f"  [{done}/{total} {pct:.0f}%] scored={scored} rejected={rejected} failed={failed}",
                        flush=True,
                    )

                # Enforce rate limit — sleep for remainder of interval
                elapsed = time.monotonic() - t0
                sleep_for = max(0.0, delay - elapsed)
                if sleep_for > 0 and done < total:
                    await asyncio.sleep(sleep_for)

    finally:
        db.close()

    summary = {
        "total": total,
        "scored": scored,
        "rejected": rejected,
        "failed": failed,
    }
    LOGGER.info("scorer_worker done: %s", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Background Haiku 4.5 job scorer")
    parser.add_argument("--rpm", type=int, default=20, help="Max requests per minute (default 20)")
    parser.add_argument("--dry-run", action="store_true", help="Score but do not write to DB")
    args = parser.parse_args()

    summary = asyncio.run(run_scorer(rpm=args.rpm, dry_run=args.dry_run))
    print(f"\nDone. total={summary['total']} scored={summary['scored']} "
          f"rejected={summary['rejected']} failed={summary['failed']}")


if __name__ == "__main__":
    main()
