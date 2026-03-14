from __future__ import annotations

from job_logging import ensure_process_logging

ensure_process_logging(__file__)

from config import LEVEL_WEIGHTS, TIER_WEIGHTS, COMPETITION_PENALTY, SOURCE_BOOST
from models import JobPosting


def freshness_weight(age_days: int) -> float:
    return max(0.5, 1.0 - (age_days / 60.0))


def final_score(posting: JobPosting) -> float:
    match = (posting.match_score or 0.0) / 10.0
    freshness = freshness_weight(posting.age_days)
    level_w = LEVEL_WEIGHTS.get(posting.level_fit or "mid", 0.7)
    tier_w = TIER_WEIGHTS.get(posting.tier or 3, 0.5)
    comp = COMPETITION_PENALTY.get(posting.competition, 0.1)
    src = SOURCE_BOOST.get(posting.source_priority, 1.0)
    tier_boost = posting.tier_boost or 1.0
    return match * freshness * level_w * tier_w * (1 - comp) * src * tier_boost


def rank_postings(postings: list[JobPosting], limit: int = 10) -> list[JobPosting]:
    for posting in postings:
        posting.final_score = final_score(posting)
    return sorted(postings, key=lambda p: p.final_score or 0.0, reverse=True)[:limit]

