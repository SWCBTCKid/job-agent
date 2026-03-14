from __future__ import annotations

from job_logging import ensure_process_logging

ensure_process_logging(__file__)

import logging

import httpx

from models import JobPosting
from scrapers.base import BaseScraper
from utils import make_job_id, strip_html  # noqa: F401 (make_job_id kept for compat)

LOGGER = logging.getLogger(__name__)


class LeverScraper(BaseScraper):
    source = "lever"
    source_priority = 1

    def __init__(self, slugs: list[tuple[str, str, float]]):
        self.slugs = slugs

    async def fetch(self) -> list[JobPosting]:
        postings: list[JobPosting] = []
        async with httpx.AsyncClient(timeout=30) as client:
            for company, slug, tier_boost in self.slugs:
                url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
                try:
                    resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 job-agent/1.0"})
                    resp.raise_for_status()
                    payload = resp.json()
                except Exception as exc:
                    LOGGER.warning("Lever fetch failed for %s (%s): %s", company, slug, exc)
                    continue
                for job in payload:
                    title = job.get("text", "")
                    ats_id = job.get("id")
                    job_id = f"lv::{ats_id}" if ats_id else make_job_id(company, title)
                    categories = job.get("categories") or {}
                    location = categories.get("location", "")
                    description = "\n".join(
                        [
                            strip_html(job.get("description", "")),
                            strip_html(job.get("descriptionPlain", "")),
                            strip_html(job.get("lists", [{}])[0].get("text", "")) if job.get("lists") else "",
                        ]
                    ).strip()
                    postings.append(
                        JobPosting(
                            id=job_id,
                            company=company,
                            title=title,
                            url=job.get("hostedUrl", ""),
                            source=self.source,
                            source_priority=self.source_priority,
                            posted_at=self.parse_timestamp(job.get("createdAt")),
                            description=description,
                            location=location,
                            remote="remote" in location.lower(),
                            tier_boost=tier_boost,
                        )
                    )
        return postings
