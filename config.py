from __future__ import annotations

from job_logging import ensure_process_logging

ensure_process_logging(__file__)

import json
import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
STATE_DIR = BASE_DIR / "state"


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    voyage_api_key: str = os.getenv("VOYAGE_API_KEY", "")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    theirstack_api_key: str = os.getenv("THEIRSTACK_API_KEY", "")
    serpapi_key: str = os.getenv("SERPAPI_KEY", "")
    salary_floor: int = int(os.getenv("JOB_AGENT_SALARY_FLOOR", "150000"))
    top_n_stage2: int = int(os.getenv("JOB_AGENT_TOP_N_STAGE2", "30"))
    digest_count: int = int(os.getenv("JOB_AGENT_DIGEST_COUNT", "10"))
    stage1_embedder: str = os.getenv("JOB_AGENT_STAGE1_EMBEDDER", "auto")
    voyage_embed_model: str = os.getenv("JOB_AGENT_VOYAGE_MODEL", "voyage-code-2")
    claude_model: str = os.getenv("JOB_AGENT_CLAUDE_MODEL", "claude-sonnet-4-6")
    haiku_model: str = os.getenv("JOB_AGENT_HAIKU_MODEL", "claude-haiku-4-5-20251001")
    stage1_threshold: float = float(os.getenv("JOB_AGENT_STAGE1_THRESHOLD", "0.35"))
    top_n_output: int = int(os.getenv("JOB_AGENT_TOP_N_OUTPUT", "100"))


SETTINGS = Settings()

HARD_EMBEDDED = [
    "firmware",
    "fpga",
]

LEVEL_WEIGHTS = {
    "senior": 1.0,
    "mid": 0.9,
    "staff": 0.5,
    "too_junior": 0.1,
    "too_senior": 0.3,
}

TIER_WEIGHTS = {1: 1.0, 2: 0.85, 3: 0.65}
COMPETITION_PENALTY = {"low": 0.0, "medium": 0.15, "high": 0.35}
SOURCE_BOOST = {1: 1.05, 2: 1.0, 3: 0.9}


def load_resume_text() -> str:
    resume_file = DATA_DIR / "resume.txt"
    if resume_file.exists():
        return resume_file.read_text(encoding="utf-8")
    return (
        "Senior software engineer focused on distributed infrastructure, security observability, "
        "and safety-critical correctness. Meta experience includes ownership of authorization "
        "validation and observability systems at 30M+ services and 10M+ hosts."
    )


def load_target_companies() -> list[dict]:
    path = DATA_DIR / "target_companies.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def load_source_seeds() -> dict:
    """Load optional per-source seed URLs used by non-API scrapers."""
    path = DATA_DIR / "source_seeds.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))

