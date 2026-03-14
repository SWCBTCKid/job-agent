from __future__ import annotations

from job_logging import ensure_process_logging

ensure_process_logging(__file__)

import json
import logging
import re

from models import JobPosting
from scrapers.base import BaseScraper
from utils import make_job_id, strip_html

LOGGER = logging.getLogger(__name__)


class YCScraper(BaseScraper):
    source = "yc"
    source_priority = 2

    def __init__(self, seeds: list[dict] | None = None):
        self.seeds = seeds or [{"url": "https://www.ycombinator.com/jobs", "company": "YC"}]

    async def fetch(self) -> list[JobPosting]:
        postings: list[JobPosting] = []
        for seed in self.seeds:
            url = seed.get("url", "https://www.ycombinator.com/jobs")
            try:
                html = await self._fetch_text_async(url)
            except Exception as exc:
                LOGGER.warning("YC fetch failed for %s: %s", url, exc)
                continue

            # Next.js payload is the most stable YC extraction method.
            next_data = _extract_next_data(html)
            if next_data:
                postings.extend(_jobs_from_next_data(next_data))
                continue

            # HTML fallback.
            for match in re.finditer(r'href="([^"]*/companies/[^"#]+/jobs/[^"]+)"[^>]*>(.*?)</a>', html, re.IGNORECASE | re.DOTALL):
                href = match.group(1)
                title = strip_html(match.group(2))
                link = href if href.startswith("http") else "https://www.ycombinator.com" + href
                company = _company_from_link(link)
                postings.append(
                    JobPosting(
                        id=make_job_id(company, title),
                        company=company,
                        title=title or "YC role",
                        url=link,
                        source=self.source,
                        source_priority=self.source_priority,
                        posted_at=self.parse_timestamp(None),
                        description=f"YC listing for {company}",
                    )
                )

        uniq: dict[str, JobPosting] = {}
        for p in postings:
            uniq[p.id] = p
        return list(uniq.values())


def _extract_next_data(html: str) -> dict | None:
    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">([\s\S]*?)</script>', html)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except Exception as exc:
        LOGGER.warning("YC __NEXT_DATA__ parse failed: %s", exc)
        return None


def _jobs_from_next_data(payload: dict) -> list[JobPosting]:
    text = json.dumps(payload)
    try:
        records = json.loads(text)
    except Exception as exc:
        LOGGER.warning("YC normalized payload parse failed: %s", exc)
        return []

    out: list[JobPosting] = []
    # Walk nested dict/list and collect job-like objects.
    stack = [records]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            keys = {k.lower() for k in node.keys()}
            if "title" in keys and ("company" in keys or "companyname" in keys) and ("url" in keys or "slug" in keys):
                company = str(node.get("company") or node.get("companyName") or "YC Company")
                title = str(node.get("title") or "Role")
                link = str(node.get("url") or "")
                if not link and node.get("slug"):
                    link = f"https://www.ycombinator.com/companies/{node.get('slug')}/jobs"
                if link and not link.startswith("http"):
                    link = "https://www.ycombinator.com" + link
                desc = strip_html(str(node.get("description") or node.get("blurb") or ""))
                out.append(
                    JobPosting(
                        id=make_job_id(company, title),
                        company=company,
                        title=title,
                        url=link,
                        source="yc",
                        source_priority=2,
                        posted_at=BaseScraper.parse_timestamp(node.get("postedAt") or node.get("createdAt")),
                        description=desc,
                        location=str(node.get("location") or ""),
                        remote="remote" in (str(node.get("location") or "") + " " + title + " " + desc).lower(),
                    )
                )
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return out


def _company_from_link(link: str) -> str:
    m = re.search(r"/companies/([^/]+)/", link)
    if not m:
        return "YC Company"
    return m.group(1).replace("-", " ").title()

