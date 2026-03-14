from __future__ import annotations

from job_logging import ensure_process_logging

ensure_process_logging(__file__)

import logging
import re

from models import JobPosting
from scrapers.base import BaseScraper
from utils import make_job_id, strip_html

LOGGER = logging.getLogger(__name__)


class WellfoundScraper(BaseScraper):
    source = "wellfound"
    source_priority = 2

    def __init__(self, seeds: list[dict] | None = None):
        self.seeds = seeds or []

    async def fetch(self) -> list[JobPosting]:
        postings: list[JobPosting] = []
        for seed in self.seeds:
            company = seed.get("company", "Wellfound")
            tier_boost = float(seed.get("tier_boost", 1.0))
            url = seed.get("url", "")
            if not url:
                continue
            try:
                html = await self._fetch_text_async(url)
            except Exception as exc:
                LOGGER.warning("Wellfound fetch failed for %s (%s): %s", company, url, exc)
                continue

            for job in re.finditer(r'<a[^>]+href="([^"]*?/jobs/[^"]+)"[^>]*>(.*?)</a>', html, re.IGNORECASE | re.DOTALL):
                href = job.group(1)
                title = strip_html(job.group(2))[:120]
                apply_url = href if href.startswith("http") else "https://wellfound.com" + href
                desc_slice = html[job.end(): job.end() + 700]
                desc = strip_html(desc_slice)
                postings.append(
                    JobPosting(
                        id=make_job_id(company, title),
                        company=company,
                        title=title or "Wellfound role",
                        url=apply_url,
                        source=self.source,
                        source_priority=self.source_priority,
                        posted_at=self.parse_timestamp(None),
                        description=desc,
                        location="",
                        remote="remote" in desc.lower(),
                        tier_boost=tier_boost,
                    )
                )

        uniq: dict[str, JobPosting] = {}
        for p in postings:
            uniq[p.id] = p
        return list(uniq.values())

