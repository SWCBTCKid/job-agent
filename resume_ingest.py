from __future__ import annotations

"""
resume_ingest.py — Resume ingestion via Claude Haiku.

Hashes resume content (sha256[:12]) for deduplication.
On first sight: calls Haiku to produce quality score, improvement suggestions,
and keyword tags used to enrich Stage 1 embedding queries.
On subsequent calls with the same content: returns cached DB record instantly.

Usage:
    from resume_ingest import ingest_resume
    record = ingest_resume("/path/to/resume.txt", db)
    print(record.quality_score, record.keywords)
"""

import hashlib
import json
import logging
import re
from pathlib import Path

import httpx

from config import SETTINGS
from db import JobDB, ResumeRecord

LOGGER = logging.getLogger(__name__)

HAIKU_MODEL = "claude-haiku-4-5-20251001"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

_SYSTEM = (
    "You are a technical resume analyst. "
    "Output only valid JSON. No prose outside the JSON object."
)

_PROMPT = """\
Analyse the following resume and return a JSON object with exactly these fields:

{{
  "quality_score": <float 0.0-10.0>,
  "improvements": [
    "<specific actionable improvement>",
    "<specific actionable improvement>",
    ...
  ],
  "keywords": {{
    "titles": ["<job title>", ...],
    "skills": ["<technical skill>", ...],
    "domains": ["<domain>", ...],
    "terminology": {{
      "<internal/proprietary term>": "<industry equivalent keywords>",
      ...
    }}
  }}
}}

quality_score guide:
  9-10  Publication-ready. Strong metrics, clear narrative, no wasted space.
  7-8   Good. A few missing metrics or framing issues.
  5-6   Average. Needs better impact quantification or role clarity.
  3-4   Weak. Vague bullets, missing context, poor structure.
  1-2   Poor. Major gaps, unreadable, or completely generic.

improvements: List 3-6 specific, actionable suggestions. Be direct and precise.
  Good: "Add request volume metric to the observability platform bullet (e.g. '30M+ services')"
  Bad: "Add more metrics"

keywords.titles: 4-8 job titles this candidate should search for, based on their actual experience.
keywords.skills: 8-15 specific technical skills/tools visible in or directly implied by the resume.
keywords.domains: 4-8 SHORT domain phrases (2-4 words) that appear verbatim or near-verbatim in job
  descriptions. Use the exact terminology a JD would use — not descriptive labels.
  Good: "observability platform", "distributed systems", "security enforcement", "fleet management",
        "site reliability", "control plane", "production engineering", "safety-critical"
  Bad:  "Security observability & diagnostics", "Fleet management & autonomous vehicles"
keywords.terminology: Internal or company-specific tool/system names that appear in the resume, mapped
  to industry-standard equivalent keywords a job description would use. Only include if the resume
  contains proprietary/internal names (e.g. company codenames, internal platforms). Return {{}} if none.
  Example: {{"scuba": "metrics aggregation observability platform", "tupperware": "kubernetes container orchestration"}}

RESUME:
{resume_text}
"""


def _call_haiku(resume_text: str) -> dict:
    """Call Haiku synchronously. Returns parsed quality/improvements/keywords dict."""
    prompt = _PROMPT.format(resume_text=resume_text[:8000])
    body = {
        "model": HAIKU_MODEL,
        "max_tokens": 1024,
        "temperature": 0,
        "system": _SYSTEM,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "Content-Type": "application/json",
        "x-api-key": SETTINGS.anthropic_api_key,
        "anthropic-version": "2023-06-01",
    }

    try:
        resp = httpx.post(ANTHROPIC_URL, headers=headers, json=body, timeout=30.0)
        resp.raise_for_status()
        raw_content = resp.json()
        text = "\n".join(
            part.get("text", "")
            for part in raw_content.get("content", [])
            if part.get("type") == "text"
        )
        return _parse_haiku_response(text)
    except httpx.HTTPStatusError as e:
        LOGGER.error("Haiku HTTP error %d: %s", e.response.status_code, e.response.text[:200])
        raise
    except Exception as e:
        LOGGER.error("Haiku call failed: %s", e)
        raise


def _parse_haiku_response(text: str) -> dict:
    """Extract and validate JSON from Haiku response."""
    # Strip markdown code fences if present
    text = re.sub(r"```(?:json)?\s*", "", text).strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError(f"No JSON in Haiku response: {text[:300]}")

    data = json.loads(m.group(0))

    # Coerce quality_score
    try:
        quality_score = max(0.0, min(10.0, float(data.get("quality_score", 5.0))))
    except (TypeError, ValueError):
        quality_score = 5.0

    # Coerce improvements
    improvements = data.get("improvements", [])
    if not isinstance(improvements, list):
        improvements = []
    improvements = [str(i).strip() for i in improvements if i]

    # Coerce keywords
    keywords = data.get("keywords", {})
    if not isinstance(keywords, dict):
        keywords = {}
    raw_terminology = keywords.get("terminology", {})
    if not isinstance(raw_terminology, dict):
        raw_terminology = {}
    keywords = {
        "titles":      [str(t) for t in keywords.get("titles",  []) if t],
        "skills":      [str(s) for s in keywords.get("skills",  []) if s],
        "domains":     [str(d) for d in keywords.get("domains", []) if d],
        "terminology": {str(k): str(v) for k, v in raw_terminology.items() if k and v},
    }

    return {
        "quality_score": quality_score,
        "improvements":  improvements,
        "keywords":      keywords,
    }


def _print_resume_report(record: ResumeRecord) -> None:
    """Print quality score and improvements to terminal."""
    score = record.quality_score or 0.0
    bar_filled = int(score)
    bar = "#" * bar_filled + "-" * (10 - bar_filled)

    print()
    print("=" * 60)
    print(f"  Resume Quality: {score:.1f}/10  [{bar}]")
    print(f"  Resume ID:      {record.id}")
    print("=" * 60)

    if record.improvements:
        print("\n  Suggested Improvements:")
        for i, imp in enumerate(record.improvements, 1):
            # Word-wrap at 56 chars
            words = imp.split()
            lines, current = [], []
            for word in words:
                if sum(len(w) + 1 for w in current) + len(word) > 56:
                    lines.append(" ".join(current))
                    current = [word]
                else:
                    current.append(word)
            if current:
                lines.append(" ".join(current))
            print(f"  {i}. {lines[0]}")
            for line in lines[1:]:
                print(f"     {line}")

    kw = record.keywords
    if kw.get("titles"):
        print(f"\n  Target Titles:  {', '.join(kw['titles'][:5])}")
    if kw.get("domains"):
        print(f"  Domains:        {', '.join(kw['domains'][:5])}")
    if kw.get("skills"):
        print(f"  Key Skills:     {', '.join(kw['skills'][:8])}")
    print()


def ingest_resume(path: str, db: JobDB, silent: bool = False, force: bool = False) -> ResumeRecord:
    """
    Ingest a resume file. Returns a ResumeRecord.

    - If content has been seen before: returns cached record instantly (no API call).
    - If new: calls Haiku, stores result, prints report to terminal.

    Args:
        path:   Path to .txt or .md resume file.
        db:     Open JobDB instance.
        silent: If True, suppress terminal output (useful in background processes).
        force:  If True, delete cached record and re-call Haiku even if content unchanged.
    """
    resume_path = Path(path)
    if not resume_path.exists():
        raise FileNotFoundError(f"Resume file not found: {path}")

    content = resume_path.read_text(encoding="utf-8")
    if not content.strip():
        raise ValueError(f"Resume file is empty: {path}")

    resume_id = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]

    if force:
        db.delete_resume(resume_id)
        if not silent:
            print(f"\nForce re-ingest: cleared cached record (id={resume_id})")
        LOGGER.info("Force re-ingest: deleted cached record %s", resume_id)

    # Fast path — already in DB
    existing = db.get_resume(resume_id)
    if existing:
        if not silent:
            print(f"\nResume already known (id={resume_id}) — skipping Haiku")
            _print_resume_report(existing)
        LOGGER.info("Resume %s already in DB — skipping ingest", resume_id)
        return existing

    # New resume — call Haiku
    LOGGER.info("New resume %s — calling Haiku for analysis...", resume_id)
    if not silent:
        print(f"\nAnalysing resume with Haiku (id={resume_id})...")

    if not SETTINGS.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set — cannot call Haiku for resume ingest")

    result = _call_haiku(content)
    db.store_resume(resume_id, str(resume_path.resolve()), result)

    record = db.get_resume(resume_id)
    if record is None:
        raise RuntimeError(f"Failed to retrieve resume {resume_id} after storing")

    if not silent:
        _print_resume_report(record)

    LOGGER.info(
        "Resume %s ingested — quality=%.1f improvements=%d keywords=%s",
        resume_id,
        record.quality_score or 0,
        len(record.improvements),
        list(record.keywords.keys()),
    )
    return record
