"""
scrapers/eightfold.py — Scraper for Eightfold.ai-powered careers sites.

These sites block their API with a PCSX/reCAPTCHA token, but publish a full
sitemap with job URLs (location + title in slug) and serve JSON-LD structured
data on each job detail page — no JS execution needed.

Strategy:
  1. Fetch sitemap.xml — 2131 jobs for Qualcomm, all URLs with location in slug.
  2. Filter to Bay Area locations by slug keyword.
  3. Filter to engineering-relevant titles by slug keyword.
  4. Fetch each detail page; extract JSON-LD JobPosting.
  5. Return JobPosting objects.

Seed config (stored in profile_sources as source_type="eightfold"):
  {
      "company":   "Qualcomm",
      "base_url":  "https://careers.qualcomm.com",
      "domain":    "qualcomm.com",
      "tier_boost": 1.1
  }
"""

from __future__ import annotations

from job_logging import ensure_process_logging

ensure_process_logging(__file__)

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import httpx

from models import JobPosting
from scrapers.base import BaseScraper
from utils import make_url_id

LOGGER = logging.getLogger(__name__)

_SITEMAP_PATH = "/careers/sitemap.xml"

# Location slugs that indicate Bay Area / CA
_BAY_AREA_SLUGS = [
    "san-francisco",
    "santa-clara",
    "san-jose",
    "sunnyvale",
    "mountain-view",
    "palo-alto",
    "menlo-park",
    "redwood-city",
    "cupertino",
    "foster-city",
    "burlingame",
    "san-mateo",
    "fremont",
    "berkeley",
    "oakland",
    "san-diego",       # Qualcomm HQ
    "los-angeles",
]

# Title slugs that indicate non-engineering roles to skip early (before fetching detail)
_SKIP_TITLE_SLUGS = [
    "marketing", "sales", "recruiter", "recruiting", "attorney", "legal",
    "finance", "accounting", "facilities", "operations-analyst", "hr-",
    "-hr-", "talent-acquisition", "customer-success", "business-development",
    "program-manager", "project-manager", "product-manager", "data-scientist",
    "research-scientist", "principal-scientist",
]

# Must contain at least one of these to be worth fetching
_ENGINEERING_SLUGS = [
    "software", "engineer", "sre", "devops", "infrastructure", "platform",
    "reliability", "security", "systems", "kernel", "firmware", "compiler",
    "gpu", "cpu", "embedded", "distributed", "backend", "fullstack",
    "full-stack", "cloud", "network", "data-engineer",
]

# Max concurrent detail-page fetches
_CONCURRENCY = 8
# Only fetch jobs posted/modified within this many days
_MAX_AGE_DAYS = 30

NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def _slug_is_bay_area(url: str) -> bool:
    return any(loc in url for loc in _BAY_AREA_SLUGS)


def _slug_is_engineering(url: str) -> bool:
    slug = url.split("/careers/job/")[-1].lower()
    if any(s in slug for s in _SKIP_TITLE_SLUGS):
        return False
    return any(s in slug for s in _ENGINEERING_SLUGS)


def _is_recent(lastmod: str | None, max_days: int) -> bool:
    if not lastmod:
        return True  # no date → include
    try:
        dt = datetime.fromisoformat(lastmod.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - dt).days
        return age <= max_days
    except Exception:
        return True


def _extract_job_id(url: str) -> str:
    """Extract numeric Eightfold job ID from URL slug."""
    slug = url.split("/careers/job/")[-1].split("?")[0]
    m = re.match(r"(\d+)", slug)
    return m.group(1) if m else make_url_id(url)


def _parse_jsonld(html: str) -> dict | None:
    blocks = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL
    )
    for block in blocks:
        try:
            data = json.loads(block.strip())
            if isinstance(data, dict) and data.get("@type") == "JobPosting":
                return data
        except Exception:
            continue
    return None


def _salary_from_jsonld(data: dict) -> tuple[int | None, int | None]:
    bp = data.get("baseSalary") or {}
    val = bp.get("value") or {}
    mn = val.get("minValue") or bp.get("minValue")
    mx = val.get("maxValue") or bp.get("maxValue")
    try:
        return (int(mn) if mn else None, int(mx) if mx else None)
    except Exception:
        return None, None


def _location_from_jsonld(data: dict) -> str:
    loc = data.get("jobLocation") or {}
    if isinstance(loc, list):
        loc = loc[0] if loc else {}
    addr = loc.get("address") or {}
    parts = [
        addr.get("addressLocality", ""),
        addr.get("addressRegion", ""),
    ]
    return ", ".join(p for p in parts if p) or data.get("jobLocationType", "")


class EightfoldScraper(BaseScraper):
    source = "eightfold"
    source_priority = 1  # direct company board

    def __init__(self, seeds: list[dict] | None = None):
        self.seeds = seeds or []

    async def fetch(self) -> list[JobPosting]:
        postings: list[JobPosting] = []
        for seed in self.seeds:
            company   = seed.get("company", "")
            base_url  = seed.get("base_url", "").rstrip("/")
            domain    = seed.get("domain", "")
            tier_boost = float(seed.get("tier_boost", 1.0))
            max_age   = int(seed.get("max_age_days", _MAX_AGE_DAYS))
            if not base_url:
                continue

            sitemap_url = f"{base_url}{_SITEMAP_PATH}?domain={domain}"
            try:
                async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                    resp = await client.get(sitemap_url,
                        headers={"User-Agent": "Mozilla/5.0 job-agent/1.0"})
                    resp.raise_for_status()
                    root = ET.fromstring(resp.text)
            except Exception as exc:
                LOGGER.warning("Eightfold sitemap fetch failed for %s: %s", company, exc)
                continue

            candidates: list[tuple[str, str | None]] = []
            for url_el in root.findall("sm:url", NS):
                loc_el = url_el.find("sm:loc", NS)
                lastmod_el = url_el.find("sm:lastmod", NS)
                if loc_el is None or "/careers/job/" not in loc_el.text:
                    continue
                url = loc_el.text.strip()
                lastmod = lastmod_el.text.strip() if lastmod_el is not None else None
                if not _slug_is_bay_area(url):
                    continue
                if not _slug_is_engineering(url):
                    continue
                if not _is_recent(lastmod, max_age):
                    continue
                candidates.append((url, lastmod))

            LOGGER.info("Eightfold %s: %d candidates after sitemap filter", company, len(candidates))

            sem = asyncio.Semaphore(_CONCURRENCY)

            async def fetch_detail(url: str, lastmod: str | None) -> JobPosting | None:
                detail_url = url if "?domain=" in url else f"{url}?domain={domain}"
                async with sem:
                    try:
                        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
                            r = await c.get(detail_url,
                                headers={"User-Agent": "Mozilla/5.0 job-agent/1.0"})
                            r.raise_for_status()
                            html = r.text
                    except Exception as exc:
                        LOGGER.debug("Eightfold detail fetch failed %s: %s", url, exc)
                        return None

                data = _parse_jsonld(html)
                if not data:
                    return None

                title = data.get("title", "").strip()
                description = re.sub(r"<[^>]+>", " ", data.get("description", "")).strip()
                date_posted = data.get("datePosted") or lastmod
                salary_min, salary_max = _salary_from_jsonld(data)
                location = _location_from_jsonld(data)
                job_id = _extract_job_id(url)

                return JobPosting(
                    id=f"eightfold::{job_id}",
                    company=company,
                    title=title,
                    url=url.split("?")[0],
                    source=self.source,
                    source_priority=self.source_priority,
                    posted_at=self.parse_timestamp(date_posted),
                    description=description,
                    location=location,
                    remote=False,
                    salary_min=salary_min,
                    salary_max=salary_max,
                    tier_boost=tier_boost,
                )

            results = await asyncio.gather(*(fetch_detail(u, lm) for u, lm in candidates))
            found = [r for r in results if r is not None]
            LOGGER.info("Eightfold %s: %d jobs fetched", company, len(found))
            postings.extend(found)

        return postings
