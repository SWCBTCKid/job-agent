from __future__ import annotations

from job_logging import ensure_process_logging

ensure_process_logging(__file__)

import logging
import re
from urllib.parse import unquote

import httpx

from models import JobPosting
from scrapers.base import BaseScraper
from utils import make_job_id, make_url_id

LOGGER = logging.getLogger(__name__)

# iCIMS search page returns parseable HTML when ?in_iframe=1 is passed
_SEARCH_PATH = "/jobs/search?ss=1&searchCategory=0&in_iframe=1"
# Job links look like: https://careers-company.icims.com/jobs/1234/some-title/job
_JOB_RE = re.compile(r'href="(https://[^"]+/jobs/(\d+)/([^"?/]+)/job)[^"]*"')


class ICIMSScraper(BaseScraper):
    source = "icims"
    source_priority = 1  # direct company board

    def __init__(self, seeds: list[dict] | None = None):
        self.seeds = seeds or []

    async def fetch(self) -> list[JobPosting]:
        postings: list[JobPosting] = []
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            for seed in self.seeds:
                company = seed.get("company", "")
                base_url = seed.get("base_url", "").rstrip("/")
                tier_boost = float(seed.get("tier_boost", 1.0))
                if not base_url:
                    continue

                search_url = base_url + _SEARCH_PATH
                try:
                    resp = await client.get(
                        search_url,
                        headers={"User-Agent": "Mozilla/5.0 job-agent/1.0"},
                    )
                    resp.raise_for_status()
                    html = resp.text
                except Exception as exc:
                    LOGGER.warning("iCIMS fetch failed for %s (%s): %s", company, search_url, exc)
                    continue

                seen: set[str] = set()
                for match in _JOB_RE.finditer(html):
                    job_url, job_id, slug = match.group(1), match.group(2), match.group(3)
                    if job_id in seen:
                        continue
                    seen.add(job_id)

                    title = unquote(slug).replace("-", " ").strip()

                    postings.append(
                        JobPosting(
                            id=f"icims::{job_id}",
                            company=company,
                            title=title,
                            url=job_url,
                            source=self.source,
                            source_priority=self.source_priority,
                            posted_at=self.parse_timestamp(None),
                            description="",
                            location="",
                            remote=False,
                            tier_boost=tier_boost,
                        )
                    )

        return postings
