from __future__ import annotations

from job_logging import ensure_process_logging

ensure_process_logging(__file__)

import asyncio
import json
import logging
import re
from typing import Any, Awaitable, Callable

import urllib.request

from config import SETTINGS
from models import JobPosting

LOGGER = logging.getLogger(__name__)

VALID_LEVELS = {"senior", "mid", "staff", "too_junior", "too_senior"}


def _infer_tier(text: str) -> int:
    t = text.lower()
    if any(k in t for k in ["observability", "security", "sre", "production engineer", "defense", "autonomous"]):
        return 1
    if any(k in t for k in ["platform", "infrastructure", "fleet", "deployment", "transit", "rail"]):
        return 2
    if re.search(r"\bml\b", t) or any(k in t for k in ["machine learning", "kubernetes", "service mesh", "control plane"]):
        return 3
    return 2


def _infer_level_fit(title: str, description: str) -> str:
    t = f"{title} {description}".lower()
    if any(k in t for k in ["principal", "distinguished", "director", "vp"]):
        return "too_senior"
    if "staff" in t:
        return "staff"
    if any(k in t for k in ["senior", "swe iii", "engineer iii", "l5", "e5"]):
        return "senior"
    if any(k in t for k in ["engineer ii", "mid-level", "mid level", "swe ii"]):
        return "mid"
    if any(k in t for k in ["junior", "new grad", "entry"]):
        return "too_junior"
    return "mid"


def _infer_match_score(posting: JobPosting) -> float:
    text = f"{posting.title} {posting.description}".lower()
    base = (posting.stage1_score or 0.0) * 12.0
    if _infer_tier(posting.description + " " + posting.title) == 1:
        base += 1.0
    # Fallback scoring should avoid over-ranking pure ML roles when the profile is infra/security-focused.
    ml_signals = any(k in text for k in ["machine learning", "deep learning", "computer vision", "research scientist", "model training"])
    infra_signals = any(k in text for k in ["platform", "infrastructure", "reliability", "security", "distributed", "backend", "mlops"])
    if ml_signals and not infra_signals:
        base -= 2.0
    if posting.embedded_flag:
        base -= 1.2
    return max(0.0, min(10.0, base))


def _fallback_reasoning(postings: list[JobPosting]) -> list[JobPosting]:
    for p in postings:
        p.tier = _infer_tier(f"{p.title} {p.description}")
        p.level_fit = _infer_level_fit(p.title, p.description)
        p.match_score = _infer_match_score(p)
        p.match_reason = (
            "Role aligns with distributed systems, reliability, and ownership experience from Meta and safety-critical systems work."
        )
        p.risk = "Role requirements may skew toward specialized domain expectations; validate interview bar and scope."
    return postings


def _extract_json_array(text: str) -> list[dict[str, Any]]:
    match = re.search(r"\[[\s\S]*\]", text)
    if not match:
        raise RuntimeError("Claude response missing JSON array")
    parsed = json.loads(match.group(0))
    if not isinstance(parsed, list):
        raise RuntimeError("Claude response is not an array")
    output: list[dict[str, Any]] = []
    for row in parsed:
        if isinstance(row, dict):
            output.append(row)
    return output


def _coerce_item(item: dict[str, Any]) -> dict[str, Any]:
    level_fit = str(item.get("level_fit", "mid")).strip().lower()
    if level_fit not in VALID_LEVELS:
        level_fit = "mid"

    tier_raw = item.get("tier", 2)
    try:
        tier = int(tier_raw)
    except Exception:
        tier = 2
    if tier not in {1, 2, 3}:
        tier = 2

    try:
        score = float(item.get("match_score", 0.0))
    except Exception:
        score = 0.0
    score = max(0.0, min(10.0, score))

    return {
        "id": str(item.get("id", "")),
        "match_score": score,
        "tier": tier,
        "match_reason": str(item.get("match_reason", "")).strip() or "No reasoning provided.",
        "risk": str(item.get("risk", "")).strip() or "No risk provided.",
        "level_fit": level_fit,
        "embedded_flag": bool(item.get("embedded_flag", False)),
    }


async def _call_claude_httpx(api_key: str, resume_text: str, postings: list[JobPosting]) -> list[dict[str, Any]]:
    try:
        import httpx  # type: ignore
    except Exception:
        return await asyncio.to_thread(_call_claude_urllib, api_key, resume_text, postings)

    jobs_json = [
        {
            "id": p.id,
            "company": p.company,
            "title": p.title,
            "description": p.description[:5000],
        }
        for p in postings
    ]
    prompt = (
        "Evaluate job fit for this candidate (mid-to-senior, not staff/principal). "
        "Return strict JSON array with keys: id,match_score,tier,match_reason,risk,level_fit,embedded_flag.\n\n"
        f"RESUME:\n{resume_text[:6000]}\n\n"
        f"JOBS:\n{json.dumps(jobs_json)}"
    )
    body = {
        "model": SETTINGS.claude_model,
        "max_tokens": 6000,
        "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post("https://api.anthropic.com/v1/messages", headers=headers, json=body)
        resp.raise_for_status()
        raw = resp.json()

    content_text = "\n".join(part.get("text", "") for part in raw.get("content", []) if part.get("type") == "text")
    items = _extract_json_array(content_text)
    return [_coerce_item(item) for item in items]


def _call_claude_urllib(api_key: str, resume_text: str, postings: list[JobPosting]) -> list[dict[str, Any]]:
    jobs_json = [
        {
            "id": p.id,
            "company": p.company,
            "title": p.title,
            "description": p.description[:5000],
        }
        for p in postings
    ]
    prompt = (
        "Evaluate job fit for this candidate (mid-to-senior, not staff/principal). "
        "Return strict JSON array with keys: id,match_score,tier,match_reason,risk,level_fit,embedded_flag.\n\n"
        f"RESUME:\n{resume_text[:6000]}\n\n"
        f"JOBS:\n{json.dumps(jobs_json)}"
    )
    body = {
        "model": SETTINGS.claude_model,
        "max_tokens": 6000,
        "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("x-api-key", api_key)
    req.add_header("anthropic-version", "2023-06-01")
    payload = json.dumps(body).encode("utf-8")
    with urllib.request.urlopen(req, data=payload, timeout=120) as resp:
        raw = json.loads(resp.read().decode("utf-8", errors="ignore"))
    content_text = "\n".join(part.get("text", "") for part in raw.get("content", []) if part.get("type") == "text")
    items = _extract_json_array(content_text)
    return [_coerce_item(item) for item in items]


async def stage2_match(
    postings: list[JobPosting],
    resume_text: str,
    anthropic_api_key: str = "",
    alert_cb: Callable[[str], Awaitable[None]] | None = None,
) -> list[JobPosting]:
    """Stage 2 reasoning with Claude and strict response validation."""
    if not postings:
        return postings
    if not anthropic_api_key:
        LOGGER.warning("Stage 2 using fallback reasoning: missing ANTHROPIC_API_KEY")
        for p in postings:
            p.reason_codes.append("STAGE2_FALLBACK_MISSING_API_KEY")
        return _fallback_reasoning(postings)

    try:
        response = await _call_claude_httpx(anthropic_api_key, resume_text, postings)
    except Exception as exc:
        LOGGER.exception("Stage 2 Claude call failed, using fallback reasoning: %s", exc)
        if alert_cb:
            await alert_cb(f"[JobAgent] Stage2 Claude failure, using fallback reasoning: {exc}")
        for p in postings:
            p.reason_codes.append("STAGE2_FALLBACK_API_FAILURE")
        return _fallback_reasoning(postings)

    by_id = {p.id: p for p in postings}
    for item in response:
        p = by_id.get(item["id"])
        if not p:
            continue
        p.match_score = item["match_score"]
        p.tier = item["tier"]
        p.match_reason = item["match_reason"]
        p.risk = item["risk"]
        p.level_fit = item["level_fit"]
        p.embedded_flag = item["embedded_flag"] or p.embedded_flag
    return postings

