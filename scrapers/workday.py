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
_WD_MAX_JOBS = 200       # default cap — override per-seed with "max_jobs"
_WD_DETAIL_CONCURRENCY = 8  # concurrent detail-page fetches

# Quick Bay Area check on raw locationsText from the listing response (no description yet)
_BAY_AREA_TOKENS = [
    "santa clara", "san jose", "san francisco", "bay area",
    "mountain view", "sunnyvale", "palo alto", "menlo park",
    "cupertino", "redwood city", "san mateo", "fremont",
    "south san francisco", "foster city", "hayward", "emeryville",
    "burlingame", "san carlos", "belmont", "oakland", "berkeley",
]


def _location_needs_detail(loc: str) -> bool:
    """Return True if this job should have its detail page fetched.

    Covers: confirmed Bay Area cities, remote, and multi-location entries
    (e.g. '2 Locations') which may include a Bay Area site.
    """
    loc_lower = loc.lower()
    if any(t in loc_lower for t in _BAY_AREA_TOKENS):
        return True
    if "remote" in loc_lower:
        return True
    # "2 Locations", "3 Locations", etc. — need detail to resolve actual cities
    if re.match(r"^\d+ locations?$", loc_lower.strip()):
        return True
    return False


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


def _fetch_workday_detail(detail_url: str) -> str:
    """Fetch the job description from the CXS job detail endpoint."""
    req = urllib.request.Request(
        detail_url,
        headers={"Accept": "application/json", "User-Agent": _WD_UA},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8", errors="ignore"))
    info = data.get("jobPostingInfo", {})
    return (info.get("jobDescription") or info.get("jobSummary") or "").strip()


async def _enrich_with_detail(sem: asyncio.Semaphore, detail_url: str, job: dict) -> None:
    """Fetch one job detail page and store description in job['_description']."""
    async with sem:
        try:
            raw = await asyncio.to_thread(_fetch_workday_detail, detail_url)
            job["_description"] = raw
        except Exception as exc:
            LOGGER.debug("Workday detail fetch failed (%s): %s", detail_url, exc)
            job["_description"] = ""


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

            # Auto-derive CXS API URL from page URL if not explicitly set.
            # Page URL format: https://{sub}.wd{N}.myworkdayjobs.com/{Tenant}
            # API URL format:  https://{sub}.wd{N}.myworkdayjobs.com/wday/cxs/{sub}/{Tenant}/jobs
            if not api_url and page_url and "myworkdayjobs.com" in page_url:
                parsed_page = urlparse(page_url)
                subdomain = parsed_page.netloc.split(".")[0]  # e.g. "bah"
                tenant = parsed_page.path.strip("/")          # e.g. "BAH_Jobs"
                if subdomain and tenant:
                    api_url = (
                        f"{parsed_page.scheme}://{parsed_page.netloc}"
                        f"/wday/cxs/{subdomain}/{tenant}/jobs"
                    )
                    LOGGER.debug("Workday: auto-derived api_url=%s", api_url)

            if api_url:
                try:
                    max_jobs = int(seed.get("max_jobs", _WD_MAX_JOBS))
                    jobs = await asyncio.to_thread(_fetch_workday_jobs, api_url, page_url, max_jobs)
                    parsed = urlparse(api_url)
                    base_url = f"{parsed.scheme}://{parsed.netloc}"
                    # detail_base = everything before the trailing /jobs in the api_url
                    detail_base = api_url.rsplit("/jobs", 1)[0]

                    # Enrich Bay Area + multi-location jobs with descriptions from detail endpoints
                    bay_jobs = [j for j in jobs if _location_needs_detail(j.get("locationsText", ""))]
                    if bay_jobs:
                        sem = asyncio.Semaphore(_WD_DETAIL_CONCURRENCY)
                        tasks = []
                        for job in bay_jobs:
                            path = job.get("externalPath", "")
                            if path:
                                detail_url = detail_base + path
                                tasks.append(_enrich_with_detail(sem, detail_url, job))
                        if tasks:
                            await asyncio.gather(*tasks)
                            LOGGER.info(
                                "Workday detail: %s — fetched %d descriptions for %d Bay Area jobs",
                                company, sum(1 for j in bay_jobs if j.get("_description")), len(bay_jobs),
                            )

                    for job in jobs:
                        title = job.get("title") or job.get("externalTitle") or ""
                        path = job.get("externalPath") or job.get("url") or ""
                        url = path if path.startswith("http") else (base_url + "/" + path.lstrip("/"))
                        desc = strip_html(job.get("_description") or job.get("description", ""))
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
