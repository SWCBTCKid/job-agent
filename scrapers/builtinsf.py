from __future__ import annotations

from job_logging import ensure_process_logging

ensure_process_logging(__file__)

import json
import logging
import re

import httpx

from models import JobPosting
from scrapers.base import BaseScraper
from utils import make_url_id, strip_html

LOGGER = logging.getLogger(__name__)

_BASE_URL = "https://builtin.com"
_SEARCH_URL = "https://builtin.com/jobs/dev-engineering"
_CITY = "san-francisco"
_SEARCH_TERMS = [
    "infrastructure engineer",
    "platform engineer",
    "site reliability engineer",
    "production engineer",
    "security engineer",
    "observability engineer",
    "systems engineer",
    "reliability engineer",
    "embedded engineer",
    "backend engineer",
    "control plane",
    "safety critical",
    "mission critical",
    "fleet management",
    "RTOS",
    "distributed systems reliability",
    "low latency",
    "autonomous systems",
    "firmware engineer",
]
_PAGES_PER_TERM = 3  # 25 jobs/page → up to 75 per term


class BuiltinSFScraper(BaseScraper):
    source = "builtinsf"
    source_priority = 2  # aggregator, higher competition than direct boards

    def __init__(self, tier_boost: float = 1.0):
        self.tier_boost = tier_boost

    async def fetch(self) -> list[JobPosting]:
        job_urls: dict[str, None] = {}  # preserve insertion order, dedupe

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

        async with httpx.AsyncClient(timeout=30, headers=headers, follow_redirects=True) as client:
            # Phase 1: collect job URLs from listing pages
            for term in _SEARCH_TERMS:
                for page in range(1, _PAGES_PER_TERM + 1):
                    params = {
                        "search": term,
                        "city": _CITY,
                    }
                    if page > 1:
                        params["page"] = str(page)
                    try:
                        resp = await client.get(_SEARCH_URL, params=params)
                        resp.raise_for_status()
                        urls = _extract_job_urls(resp.text)
                        if not urls:
                            break  # no results on this page, stop paginating
                        for u in urls:
                            job_urls[u] = None
                        LOGGER.info(
                            "BuiltinSF: term=%r page=%d found=%d urls (total=%d)",
                            term, page, len(urls), len(job_urls),
                        )
                    except Exception as exc:
                        LOGGER.warning("BuiltinSF listing failed term=%r page=%d: %s", term, page, exc)
                        break

            # Phase 2: fetch each job detail page concurrently (batched)
            postings: list[JobPosting] = []
            url_list = list(job_urls.keys())
            batch_size = 10
            for i in range(0, len(url_list), batch_size):
                batch = url_list[i : i + batch_size]
                import asyncio
                results = await asyncio.gather(
                    *(_fetch_job_detail(client, u, self.tier_boost) for u in batch),
                    return_exceptions=True,
                )
                for url, result in zip(batch, results):
                    if isinstance(result, Exception):
                        LOGGER.warning("BuiltinSF detail fetch failed %s: %s", url, result)
                    elif result is not None:
                        postings.append(result)

        # Dedupe by id
        uniq: dict[str, JobPosting] = {}
        for p in postings:
            uniq[p.id] = p
        LOGGER.info("BuiltinSF: total unique postings=%d", len(uniq))
        return list(uniq.values())


def _extract_job_urls(html: str) -> list[str]:
    """Extract job detail URLs from Builtin SF listing page HTML.

    Builtin embeds absolute job URLs directly in the HTML (not via JSON-LD or
    relative hrefs). We match https://builtin.com/job/... patterns directly,
    then fall back to relative /job/ hrefs if nothing found.
    """
    # Primary: absolute builtin.com/job/ URLs anywhere in the HTML
    seen: dict[str, None] = {}
    for u in re.findall(r'https://builtin\.com/job/[^\s"\'<>?#]+', html):
        seen[u] = None
    if seen:
        return list(seen.keys())

    # Fallback: relative /job/ hrefs
    urls: list[str] = []
    for href in re.findall(r'href=["\'](/job/[^"\'?\s]+)["\']', html):
        full = _BASE_URL + href
        if full not in urls:
            urls.append(full)
    return urls


async def _fetch_job_detail(
    client: httpx.AsyncClient,
    url: str,
    tier_boost: float,
) -> JobPosting | None:
    """Fetch a single Builtin SF job detail page and parse it."""
    resp = await client.get(url)
    resp.raise_for_status()
    html = resp.text

    # Find all JSON-LD blocks and look for JobPosting schema.
    # Builtin uses plain <script> blocks (no type attribute) with @graph arrays.
    schema: dict = {}
    for block in re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL):
        block = block.strip()
        if not block.startswith("{"):
            continue
        try:
            data = json.loads(block)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        # Direct JobPosting
        if data.get("@type") == "JobPosting":
            schema = data
            break
        # @graph array containing a JobPosting
        for node in data.get("@graph", []):
            if isinstance(node, dict) and node.get("@type") == "JobPosting":
                schema = node
                break
        if schema:
            break

    # Fallback: try to parse Next.js __NEXT_DATA__ if no JSON-LD found
    if not schema:
        schema = _extract_from_next_data(html)

    if not schema:
        LOGGER.debug("BuiltinSF: no JobPosting schema found at %s", url)
        return None

    company = (schema.get("hiringOrganization") or {}).get("name", "") or _extract_company_meta(html)
    title = schema.get("title", "") or schema.get("name", "")
    if not title:
        return None

    description_html = schema.get("description", "")
    description = strip_html(description_html) if description_html else ""
    if not description:
        description = _extract_description_meta(html)

    location = _parse_location(schema)
    remote = schema.get("jobLocationType") == "TELECOMMUTE" or "remote" in location.lower()

    salary_min, salary_max = _parse_salary_schema(schema)

    date_posted = schema.get("datePosted") or schema.get("validThrough") or None
    from scrapers.base import BaseScraper as _BS
    posted_at = _BS.parse_timestamp(date_posted)

    job_id = make_url_id(url)

    return JobPosting(
        id=job_id,
        company=company,
        title=title,
        url=url,
        source="builtinsf",
        source_priority=2,
        posted_at=posted_at,
        description=description,
        location=location,
        remote=remote,
        salary_min=salary_min,
        salary_max=salary_max,
        tier_boost=tier_boost,
    )


def _parse_location(schema: dict) -> str:
    loc = schema.get("jobLocation")
    if not loc:
        return ""
    # Normalize to list
    locs = loc if isinstance(loc, list) else [loc]
    # Prefer a Bay Area location if multiple options exist
    _BAY_CITIES = {"san francisco", "palo alto", "mountain view", "sunnyvale", "santa clara",
                   "san mateo", "redwood city", "menlo park", "berkeley", "oakland", "san jose"}
    for candidate in locs:
        address = candidate.get("address") or {}
        if isinstance(address, str):
            return address
        city = address.get("addressLocality", "").lower()
        if city in _BAY_CITIES:
            parts = filter(None, [address.get("addressLocality", ""), address.get("addressRegion", "")])
            return ", ".join(parts)
    # Fall back to first location
    address = locs[0].get("address") or {} if locs else {}
    if isinstance(address, str):
        return address
    parts = filter(None, [address.get("addressLocality", ""), address.get("addressRegion", "")])
    return ", ".join(parts)


def _parse_salary_schema(schema: dict) -> tuple[int | None, int | None]:
    base = schema.get("baseSalary")
    if not base:
        return None, None
    value = base.get("value") or {}
    currency = base.get("currency") or value.get("currency") or ""
    if currency and currency.upper() != "USD":
        return None, None
    low = value.get("minValue")
    high = value.get("maxValue")
    # Handle single value
    if low is None and high is None:
        single = value.get("value")
        if single:
            return int(single), int(single)
    return (int(low) if low else None, int(high) if high else None)


def _extract_company_meta(html: str) -> str:
    m = re.search(r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
    return m.group(1) if m else ""


def _extract_description_meta(html: str) -> str:
    m = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']{50,})["\']', html, re.IGNORECASE)
    return m.group(1) if m else ""


def _extract_id_from_url(url: str) -> str:
    """Extract numeric ID from builtin.com/job/company-slug/12345 URLs."""
    m = re.search(r"/job/[^/]+/(\d+)", url)
    return m.group(1) if m else url


def _extract_from_next_data(html: str) -> dict:
    """Fallback: extract job info from Next.js __NEXT_DATA__ JSON."""
    m = re.search(r'<script id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, re.DOTALL)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
        # Navigate to job details — structure varies by page
        props = data.get("props", {}).get("pageProps", {})
        job = props.get("job") or props.get("jobPosting") or {}
        if not job:
            return {}
        # Map to schema.org-like structure so caller can parse uniformly
        return {
            "@type": "JobPosting",
            "title": job.get("title", ""),
            "description": job.get("description", ""),
            "datePosted": job.get("datePosted") or job.get("posted_at"),
            "hiringOrganization": {"name": (job.get("company") or {}).get("name", "")},
            "jobLocation": {"address": {"addressLocality": job.get("city", ""), "addressRegion": job.get("state", "")}},
        }
    except (json.JSONDecodeError, AttributeError, KeyError):
        return {}
