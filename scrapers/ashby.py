from __future__ import annotations

from job_logging import ensure_process_logging

ensure_process_logging(__file__)

import logging

import httpx

from models import JobPosting
from scrapers.base import BaseScraper
from utils import make_job_id, strip_html

LOGGER = logging.getLogger(__name__)

_ASHBY_BASE = "https://api.ashbyhq.com/posting-api/job-board"


class AshbyScraper(BaseScraper):
    source = "ashby"
    source_priority = 1  # direct company board, low competition

    def __init__(self, seeds: list[dict] | None = None):
        self.seeds = seeds or []

    async def fetch(self) -> list[JobPosting]:
        postings: list[JobPosting] = []
        async with httpx.AsyncClient(timeout=30) as client:
            for seed in self.seeds:
                company = seed.get("company", "")
                slug = seed.get("slug", "")
                tier_boost = float(seed.get("tier_boost", 1.0))
                if not slug:
                    continue
                url = f"{_ASHBY_BASE}/{slug}?includeCompensation=true"
                try:
                    resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 job-agent/1.0"})
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as exc:
                    LOGGER.warning("Ashby fetch failed for %s (%s): %s", company, slug, exc)
                    continue

                for job in data.get("jobs", []):
                    if not job.get("isListed"):
                        continue
                    title = job.get("title", "")
                    ats_id = job.get("id")
                    job_id = f"ashby::{ats_id}" if ats_id else make_job_id(company, title)
                    location = job.get("location") or ""
                    # secondaryLocations may have additional location strings
                    secondary = [loc.get("location", "") for loc in job.get("secondaryLocations", [])]
                    all_locations = " | ".join(filter(None, [location] + secondary))

                    description = strip_html(job.get("descriptionHtml") or "") or job.get("descriptionPlain") or ""

                    salary_min, salary_max = _parse_salary(job)

                    postings.append(
                        JobPosting(
                            id=make_job_id(company, title),
                            company=company,
                            title=title,
                            url=job.get("jobUrl", ""),
                            source=self.source,
                            source_priority=self.source_priority,
                            posted_at=self.parse_timestamp(job.get("publishedAt")),
                            description=description,
                            location=all_locations,
                            remote=job.get("isRemote") or "remote" in (location + title).lower(),
                            salary_min=salary_min,
                            salary_max=salary_max,
                            tier_boost=tier_boost,
                        )
                    )

        uniq: dict[str, JobPosting] = {}
        for p in postings:
            uniq[p.id] = p
        return list(uniq.values())


def _parse_salary(job: dict) -> tuple[int | None, int | None]:
    """Extract USD salary min/max from Ashby compensation structure."""
    comp = job.get("compensation") or {}
    for tier in comp.get("compensationTiers") or []:
        for component in tier.get("components") or []:
            if component.get("compensationType") == "Salary" and component.get("currencyCode") == "USD":
                low = component.get("minValue")
                high = component.get("maxValue")
                return (int(low) if low else None, int(high) if high else None)
    return None, None
