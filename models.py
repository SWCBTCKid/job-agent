from __future__ import annotations

from job_logging import ensure_process_logging

ensure_process_logging(__file__)

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class JobPosting:
    id: str
    company: str
    title: str
    url: str
    source: str
    source_priority: int
    posted_at: datetime
    description: str
    location: str = ""
    remote: bool = False
    salary_min: int | None = None
    salary_max: int | None = None
    salary_inferred: bool = False
    salary_confidence: str = "unknown"
    embed_score: float | None = None
    penalty_multiplier: float = 1.0
    stage1_score: float | None = None
    match_score: float | None = None
    tier: int | None = None
    match_reason: str | None = None
    risk: str | None = None
    level_fit: str | None = None
    embedded_flag: bool = False
    final_score: float | None = None
    competition: str = "medium"
    linkedin_crosspost: bool = False
    tier_boost: float = 1.0
    reason_codes: list[str] = field(default_factory=list)

    @property
    def age_days(self) -> int:
        return max(0, (datetime.now(timezone.utc) - self.posted_at).days)

