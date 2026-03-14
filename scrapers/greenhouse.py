from __future__ import annotations

from job_logging import ensure_process_logging

ensure_process_logging(__file__)

import logging

from models import JobPosting
from scrapers.base import BaseScraper
from utils import make_job_id, make_url_id, strip_html

LOGGER = logging.getLogger(__name__)


class GreenhouseScraper(BaseScraper):
    source = "greenhouse"
    source_priority = 1

    def __init__(self, slugs: list[tuple[str, str, float]]):
        self.slugs = slugs

    async def fetch(self) -> list[JobPosting]:
        postings: list[JobPosting] = []
        for company, slug, tier_boost in self.slugs:
            url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
            try:
                payload = await self._fetch_json_async(url)
            except Exception as exc:
                LOGGER.warning("Greenhouse fetch failed for %s (%s): %s", company, slug, exc)
                continue
            for job in payload.get("jobs", []):
                title = job.get("title", "")
                ats_id = job.get("id")
                job_id = f"gh::{ats_id}" if ats_id else make_job_id(company, title)
                postings.append(
                    JobPosting(
                        id=job_id,
                        company=company,
                        title=title,
                        url=job.get("absolute_url", ""),
                        source=self.source,
                        source_priority=self.source_priority,
                        posted_at=self.parse_timestamp(job.get("updated_at") or job.get("created_at")),
                        description=strip_html(job.get("content", "")),
                        location=(job.get("location") or {}).get("name", ""),
                        remote="remote" in ((job.get("location") or {}).get("name", "").lower()),
                        tier_boost=tier_boost,
                    )
                )
        return postings

