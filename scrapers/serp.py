from __future__ import annotations

from job_logging import ensure_process_logging

ensure_process_logging(__file__)

import logging
from urllib.parse import urlencode

import httpx

from models import JobPosting
from scrapers.base import BaseScraper
from utils import make_url_id, make_job_id

LOGGER = logging.getLogger(__name__)

_BASE_URL = "https://serpapi.com/search"


class SerpScraper(BaseScraper):
    source = "serp"
    source_priority = 2  # aggregated, more competition

    def __init__(self, api_key: str, seeds: list[dict] | None = None):
        self.api_key = api_key
        self.seeds = seeds or []

    async def fetch(self) -> list[JobPosting]:
        if not self.api_key:
            LOGGER.warning("SerpAPI key not set — skipping")
            return []

        postings: list[JobPosting] = []

        async with httpx.AsyncClient(timeout=30) as client:
            for seed in self.seeds:
                query = seed.get("query", "")
                location = seed.get("location", "San Francisco, California")
                date_posted = seed.get("date_posted", "week")  # hour/day/week/month
                tier_boost = float(seed.get("tier_boost", 1.0))

                if not query:
                    continue

                params = {
                    "engine": "google_jobs",
                    "q": query,
                    "location": location,
                    "date_posted": date_posted,
                    "api_key": self.api_key,
                    "hl": "en",
                    "gl": "us",
                }

                try:
                    resp = await client.get(_BASE_URL, params=params)
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as exc:
                    LOGGER.warning("SerpAPI fetch failed for query '%s': %s", query, exc)
                    continue

                jobs = data.get("jobs_results", [])
                LOGGER.info("SerpAPI [%s]: %d jobs returned", query, len(jobs))

                for job in jobs:
                    title = job.get("title") or ""
                    company_name = job.get("company_name") or ""
                    location_str = job.get("location") or ""
                    description = job.get("description") or ""

                    # Pick best apply URL
                    apply_options = job.get("apply_options") or []
                    url = apply_options[0].get("link", "") if apply_options else ""

                    # Extract salary from detected_extensions
                    extensions = job.get("detected_extensions") or {}
                    salary_str = extensions.get("salary", "")
                    salary_min, salary_max = _parse_salary_string(salary_str)

                    job_id = make_url_id(url) if url else make_job_id(company_name, title)

                    postings.append(
                        JobPosting(
                            id=job_id,
                            company=company_name,
                            title=title,
                            url=url,
                            source=self.source,
                            source_priority=self.source_priority,
                            posted_at=self.parse_timestamp(None),
                            description=description,
                            location=location_str,
                            remote="remote" in (location_str + title).lower(),
                            salary_min=salary_min,
                            salary_max=salary_max,
                            tier_boost=tier_boost,
                        )
                    )

        return postings


def _parse_salary_string(salary_str: str) -> tuple[int | None, int | None]:
    """Parse salary ranges like '$150K–$200K a year' or '$120,000 - $160,000'."""
    import re
    if not salary_str:
        return None, None
    nums = re.findall(r"[\$]?([\d,]+)[Kk]?", salary_str)
    values = []
    for n in nums:
        val = int(n.replace(",", ""))
        if val < 1000:  # it's in K
            val *= 1000
        values.append(val)
    if len(values) >= 2:
        return values[0], values[1]
    if len(values) == 1:
        return values[0], None
    return None, None
