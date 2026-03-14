from __future__ import annotations

from job_logging import ensure_process_logging

ensure_process_logging(__file__)

import logging
from datetime import datetime, timezone

import httpx

from models import JobPosting
from scrapers.base import BaseScraper
from utils import make_url_id, make_job_id

LOGGER = logging.getLogger(__name__)

_BASE_URL = "https://api.theirstack.com/v1/jobs/search"


class TheirStackScraper(BaseScraper):
    source = "theirstack"
    source_priority = 1  # direct company data

    def __init__(self, api_key: str, seeds: list[dict] | None = None):
        self.api_key = api_key
        self.seeds = seeds or []

    async def fetch(self) -> list[JobPosting]:
        if not self.api_key:
            LOGGER.warning("TheirStack API key not set — skipping")
            return []

        postings: list[JobPosting] = []
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=30) as client:
            for seed in self.seeds:
                tier_boost = float(seed.get("tier_boost", 1.0))
                limit = int(seed.get("limit", 25))

                payload: dict = {
                    "page": 0,
                    "limit": limit,
                    "posted_at_max_age_days": int(seed.get("max_age_days", 14)),
                    "job_country_code_or": ["US"],
                    "order_by": [{"field": "date_posted", "desc": True}],
                }

                # Company targeting — use domains if provided, else names
                if seed.get("company_domains"):
                    payload["company_domain_or"] = seed["company_domains"]
                elif seed.get("company_names"):
                    payload["company_name_case_insensitive_or"] = seed["company_names"]

                if seed.get("job_title_pattern_or"):
                    payload["job_title_pattern_or"] = seed["job_title_pattern_or"]

                if seed.get("job_location_pattern_or"):
                    payload["job_location_pattern_or"] = seed["job_location_pattern_or"]

                if seed.get("min_salary_usd"):
                    payload["min_salary_usd"] = seed["min_salary_usd"]

                label = seed.get("label", str(seed.get("company_domains") or seed.get("company_names", "batch")))
                try:
                    resp = await client.post(_BASE_URL, json=payload, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as exc:
                    LOGGER.warning("TheirStack fetch failed for %s: %s", label, exc)
                    continue

                jobs = data.get("data", [])
                LOGGER.info("TheirStack [%s]: %d jobs returned", label, len(jobs))

                for job in jobs:
                    company_obj = job.get("company") or {}
                    company_name = company_obj.get("name") or ""
                    title = job.get("job_title") or ""
                    url = job.get("url") or job.get("final_url") or job.get("source_url") or ""
                    location = job.get("location") or job.get("short_location") or ""
                    description = job.get("description") or ""
                    salary_min = job.get("min_annual_salary")
                    salary_max = job.get("max_annual_salary")
                    remote = bool(job.get("remote"))

                    job_id = f"theirstack::{job['id']}" if job.get("id") else (
                        make_url_id(url) if url else make_job_id(company_name, title)
                    )

                    postings.append(
                        JobPosting(
                            id=job_id,
                            company=company_name,
                            title=title,
                            url=url,
                            source=self.source,
                            source_priority=self.source_priority,
                            posted_at=self.parse_timestamp(job.get("date_posted")),
                            description=description,
                            location=location,
                            remote=remote,
                            salary_min=int(salary_min) if salary_min else None,
                            salary_max=int(salary_max) if salary_max else None,
                            tier_boost=tier_boost,
                        )
                    )

        return postings
