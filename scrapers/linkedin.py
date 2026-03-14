from __future__ import annotations

from job_logging import ensure_process_logging

ensure_process_logging(__file__)

import logging
import re

from models import JobPosting
from scrapers.base import BaseScraper
from utils import make_job_id, strip_html

LOGGER = logging.getLogger(__name__)


class LinkedInScraper(BaseScraper):
    source = "linkedin"
    source_priority = 3

    def __init__(self, seeds: list[dict] | None = None):
        self.seeds = seeds or []

    async def fetch(self) -> list[JobPosting]:
        postings: list[JobPosting] = []
        for seed in self.seeds:
            url = seed.get("url", "")
            if not url:
                continue
            try:
                html = await self._fetch_text_async(url)
            except Exception as exc:
                LOGGER.warning("LinkedIn seed fetch failed for %s: %s", url, exc)
                continue

            for match in re.finditer(r'href="([^"]*linkedin\.com/jobs/view/[0-9]+[^"]*)"[^>]*>(.*?)</a>', html, re.IGNORECASE | re.DOTALL):
                link = match.group(1)
                title = strip_html(match.group(2))
                context = html[max(0, match.start() - 600): match.end() + 600]
                company_match = re.search(r'([A-Z][A-Za-z0-9& .-]{2,})\s*</', context)
                company = company_match.group(1).strip() if company_match else "LinkedInCompany"
                desc = strip_html(context)
                postings.append(
                    JobPosting(
                        id=make_job_id(company, title),
                        company=company,
                        title=title or "LinkedIn role",
                        url=link,
                        source=self.source,
                        source_priority=self.source_priority,
                        posted_at=self.parse_timestamp(None),
                        description=desc,
                        remote="remote" in desc.lower(),
                    )
                )

        uniq: dict[str, JobPosting] = {}
        for p in postings:
            uniq[p.id] = p
        return list(uniq.values())

