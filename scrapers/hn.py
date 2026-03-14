from __future__ import annotations

from job_logging import ensure_process_logging

ensure_process_logging(__file__)

import logging
import re

from models import JobPosting
from scrapers.base import BaseScraper
from utils import make_job_id, strip_html

LOGGER = logging.getLogger(__name__)


class HNScraper(BaseScraper):
    source = "hn"
    source_priority = 2

    async def fetch(self) -> list[JobPosting]:
        postings: list[JobPosting] = []
        try:
            stories = await self._fetch_json_async("https://hn.algolia.com/api/v1/search?query=Who%20is%20hiring%3F&tags=story")
            hits = stories.get("hits", [])
            if not hits:
                return postings
            latest = sorted(hits, key=lambda x: x.get("created_at_i", 0), reverse=True)[0]
            story_id = latest.get("objectID")
            comments = await self._fetch_json_async(
                f"https://hn.algolia.com/api/v1/search?tags=comment,story_{story_id}&hitsPerPage=500"
            )
        except Exception as exc:
            LOGGER.warning("HN fetch failed: %s", exc)
            return postings

        for hit in comments.get("hits", []):
            text = strip_html(hit.get("comment_text", ""))
            if "http" not in text:
                continue
            title = text.split("|", 1)[0][:100]
            company = _extract_company(title or text)
            hn_id = hit.get("objectID")
            postings.append(
                JobPosting(
                    id=f"hn::{hn_id}" if hn_id else make_job_id(company, title),
                    company=company,
                    title=title or "Hiring post",
                    url=hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
                    source=self.source,
                    source_priority=self.source_priority,
                    posted_at=self.parse_timestamp(hit.get("created_at_i")),
                    description=text,
                    location="Remote" if "remote" in text.lower() else "",
                    remote="remote" in text.lower(),
                )
            )
        return postings


def _extract_company(text: str) -> str:
    raw = text.strip()
    if not raw:
        return "HNCompany"
    for delim in ["|", " - ", "(", "\n"]:
        if delim in raw:
            raw = raw.split(delim, 1)[0].strip()
            break
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw[:60] if raw else "HNCompany"

