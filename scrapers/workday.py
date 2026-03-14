from __future__ import annotations

from job_logging import ensure_process_logging

ensure_process_logging(__file__)

import logging
import re

from models import JobPosting
from scrapers.base import BaseScraper
from utils import make_job_id, make_url_id, strip_html

LOGGER = logging.getLogger(__name__)


class WorkdayScraper(BaseScraper):
    source = "workday"
    source_priority = 2

    def __init__(self, seeds: list[dict] | None = None):
        self.seeds = seeds or []
        self.fetch_errors: list[str] = []  # populated on per-seed failures

    async def fetch(self) -> list[JobPosting]:
        postings: list[JobPosting] = []
        for seed in self.seeds:
            company = seed.get("company", "WorkdayCompany")
            tier_boost = float(seed.get("tier_boost", 1.0))
            api_url = seed.get("api_url", "")
            page_url = seed.get("url", "")

            if api_url:
                try:
                    payload = await self._fetch_json_async(api_url)
                    jobs = payload.get("jobPostings") or payload.get("jobs") or []
                    for job in jobs:
                        title = job.get("title") or job.get("externalTitle") or ""
                        path = job.get("externalPath") or job.get("url") or ""
                        url = path if path.startswith("http") else (page_url.rstrip("/") + "/" + path.lstrip("/"))
                        desc = strip_html(job.get("description", ""))
                        loc = job.get("locationsText") or job.get("location", "")
                        postings.append(
                            JobPosting(
                                id=make_url_id(url) if url else make_job_id(company, title),
                                company=company,
                                title=title,
                                url=url,
                                source=self.source,
                                source_priority=self.source_priority,
                                posted_at=self.parse_timestamp(job.get("postedOn") or job.get("postedDate")),
                                description=desc,
                                location=loc,
                                remote="remote" in str(loc).lower(),
                                tier_boost=tier_boost,
                            )
                        )
                    continue
                except Exception as exc:
                    LOGGER.warning("Workday API fetch failed for %s (%s): %s", company, api_url, exc)
                    self.fetch_errors.append(f"{company}: {exc}")

            if not page_url:
                continue
            try:
                html = await self._fetch_text_async(page_url)
            except Exception as exc:
                LOGGER.warning("Workday page fetch failed for %s (%s): %s", company, page_url, exc)
                self.fetch_errors.append(f"{company}: {exc}")
                continue

            for match in re.finditer(r'href="([^"]*(?:/job/|/jobs/)[^"]+)"', html, re.IGNORECASE):
                href = match.group(1)
                url = href if href.startswith("http") else page_url.rstrip("/") + "/" + href.lstrip("/")
                title_match = re.search(r">([^<]{8,120})<", html[max(0, match.start()-200):match.end()+200])
                title = strip_html(title_match.group(1) if title_match else "Workday Role")
                postings.append(
                    JobPosting(
                        id=make_url_id(url) if url else make_job_id(company, title),
                        company=company,
                        title=title,
                        url=url,
                        source=self.source,
                        source_priority=self.source_priority,
                        posted_at=self.parse_timestamp(None),
                        description=f"Workday listing from {company}",
                        tier_boost=tier_boost,
                    )
                )

        uniq: dict[str, JobPosting] = {}
        for p in postings:
            uniq[p.id] = p
        return list(uniq.values())

