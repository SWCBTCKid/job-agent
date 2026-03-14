from __future__ import annotations

from job_logging import ensure_process_logging

ensure_process_logging(__file__)

import json
from pathlib import Path


def load_salary_table(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def infer_salary(company: str, level: str, role_family: str, table: list[dict]) -> tuple[int | None, int | None, str]:
    """Infer salary from most-specific to broadest match."""
    company_l = company.lower().strip()
    level_l = level.lower().strip()
    family_l = role_family.lower().strip()
    candidates = [
        row
        for row in table
        if row.get("company", "").lower() == company_l
        and row.get("level", "").lower() == level_l
        and row.get("role_family", "").lower() == family_l
    ]
    if candidates:
        r = candidates[0]
        return int(r.get("salary_min", 0)), int(r.get("salary_max", 0)), "inferred_high"

    fallback = [
        row for row in table if row.get("company", "").lower() == company_l and row.get("level", "").lower() == level_l
    ]
    if fallback:
        r = fallback[0]
        return int(r.get("salary_min", 0)), int(r.get("salary_max", 0)), "inferred_low"

    family_level = [
        row
        for row in table
        if row.get("company", "").lower() in {"*", "industry_average"}
        and row.get("level", "").lower() == level_l
        and row.get("role_family", "").lower() == family_l
    ]
    if family_level:
        r = family_level[0]
        return int(r.get("salary_min", 0)), int(r.get("salary_max", 0)), "inferred_low"

    family_any = [
        row
        for row in table
        if row.get("company", "").lower() in {"*", "industry_average"}
        and row.get("role_family", "").lower() == family_l
    ]
    if family_any:
        r = family_any[0]
        return int(r.get("salary_min", 0)), int(r.get("salary_max", 0)), "inferred_low"
    return None, None, "unknown"

