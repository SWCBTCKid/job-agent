from __future__ import annotations

from job_logging import ensure_process_logging

ensure_process_logging(__file__)

import logging
import re
import xml.etree.ElementTree as ET

from models import JobPosting
from scrapers.base import BaseScraper
from utils import make_job_id, strip_html

LOGGER = logging.getLogger(__name__)


class PragmaticScraper(BaseScraper):
    source = "pragmatic"
    source_priority = 2

    def __init__(self, seeds: list[dict] | None = None):
        self.seeds = seeds or [{"url": "https://jobs.pragmaticengineer.com/jobs.rss", "company": "PragmaticEngineer"}]

    async def fetch(self) -> list[JobPosting]:
        postings: list[JobPosting] = []
        for seed in self.seeds:
            url = seed.get("url", "")
            if not url:
                continue
            try:
                xml_text = await self._fetch_text_async(url)
            except Exception as exc:
                LOGGER.warning("Pragmatic fetch failed for %s: %s", url, exc)
                continue

            try:
                root = ET.fromstring(xml_text)
            except Exception as exc:
                LOGGER.warning("Pragmatic XML parse failed for %s: %s", url, exc)
                continue

            for item in root.findall(".//item"):
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                description = strip_html(item.findtext("description") or "")
                company = seed.get("company") or _company_from_title(title)
                postings.append(
                    JobPosting(
                        id=make_job_id(company, title),
                        company=company,
                        title=title,
                        url=link,
                        source=self.source,
                        source_priority=self.source_priority,
                        posted_at=self.parse_timestamp(item.findtext("pubDate")),
                        description=description,
                        remote="remote" in description.lower() or "remote" in title.lower(),
                    )
                )

        uniq: dict[str, JobPosting] = {}
        for p in postings:
            uniq[p.id] = p
        return list(uniq.values())


def _company_from_title(title: str) -> str:
    match = re.search(r"\bat\s+([A-Za-z0-9 &.-]+)", title)
    if match:
        return match.group(1).strip()
    return "PragmaticEngineer"

