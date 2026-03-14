from __future__ import annotations

from job_logging import ensure_process_logging

ensure_process_logging(__file__)

import asyncio
import http.cookiejar
import json
import logging
import re
import urllib.request
from urllib.parse import urlparse

from models import JobPosting
from scrapers.base import BaseScraper
from utils import make_job_id, make_url_id, strip_html

LOGGER = logging.getLogger(__name__)

_WD_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_WD_PAGE_LIMIT = 20
_WD_MAX_JOBS = 200  # default cap — override per-seed with "max_jobs"


def _fetch_workday_jobs(api_url: str, referer: str, max_jobs: int = _WD_MAX_JOBS) -> list[dict]:
    """POST to Workday CXS jobs API with session cookies and full pagination.

    The CXS endpoint requires:
      - appliedFacets key (even when empty) — without it the server returns 422
      - A browser-like User-Agent
      - Session cookies acquired by GETting the job board page first
    """
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    # Step 1: GET the job board page to acquire session cookies
    if referer:
        try:
            req = urllib.request.Request(referer, headers={"User-Agent": _WD_UA})
            with opener.open(req, timeout=30):
                pass
        except Exception as exc:
            LOGGER.debug("Workday session GET failed (%s): %s", referer, exc)

    # Step 2: Paginate via POST
    # NOTE: Workday only returns `total` on the first page; subsequent pages return total=0.
    # Capture total once and reuse it.
    all_jobs: list[dict] = []
    offset = 0
    total: int | None = None
    while True:
        body = json.dumps({
            "appliedFacets": {},
            "limit": _WD_PAGE_LIMIT,
            "offset": offset,
            "searchText": "",
        }).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _WD_UA,
        }
        if referer:
            headers["Referer"] = referer
        req = urllib.request.Request(api_url, data=body, headers=headers, method="POST")
        with opener.open(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))

        jobs = data.get("jobPostings") or []
        all_jobs.extend(jobs)
        if total is None:
            total = data.get("total", 0)
        offset += _WD_PAGE_LIMIT
        if not jobs or len(all_jobs) >= max_jobs or offset >= total:
            break

    return all_jobs[:max_jobs]


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
                    max_jobs = int(seed.get("max_jobs", _WD_MAX_JOBS))
                    jobs = await asyncio.to_thread(_fetch_workday_jobs, api_url, page_url, max_jobs)
                    parsed = urlparse(api_url)
                    base_url = f"{parsed.scheme}://{parsed.netloc}"
                    for job in jobs:
                        title = job.get("title") or job.get("externalTitle") or ""
                        path = job.get("externalPath") or job.get("url") or ""
                        url = path if path.startswith("http") else (base_url + "/" + path.lstrip("/"))
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
                    LOGGER.info("Workday API: %s — %d jobs fetched", company, len(jobs))
                    continue
                except Exception as exc:
                    LOGGER.warning("Workday API fetch failed for %s (%s): %s", company, api_url, exc)
                    self.fetch_errors.append(f"{company}: {exc}")
                    # fall through to HTML scraping if page_url available

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
