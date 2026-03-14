from __future__ import annotations

from job_logging import ensure_process_logging

ensure_process_logging(__file__)

import asyncio
import json
import urllib.request
from abc import ABC, abstractmethod
from datetime import datetime, timezone


class BaseScraper(ABC):
    source: str = "unknown"
    source_priority: int = 2

    @abstractmethod
    async def fetch(self) -> list:
        raise NotImplementedError

    def _fetch_json(self, url: str) -> dict | list:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 job-agent/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", errors="ignore"))

    def _fetch_text(self, url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 job-agent/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="ignore")

    async def _fetch_json_async(self, url: str) -> dict | list:
        return await asyncio.to_thread(self._fetch_json, url)

    async def _fetch_text_async(self, url: str) -> str:
        return await asyncio.to_thread(self._fetch_text, url)

    @staticmethod
    def parse_timestamp(value: str | int | float | None) -> datetime:
        if value is None:
            return datetime.now(timezone.utc)
        if isinstance(value, (int, float)):
            ts = float(value)
            if ts > 1e10:  # Lever returns milliseconds; convert to seconds
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        text = str(value).strip()
        try:
            if text.endswith("Z"):
                text = text.replace("Z", "+00:00")
            return datetime.fromisoformat(text).astimezone(timezone.utc)
        except ValueError:
            return datetime.now(timezone.utc)

