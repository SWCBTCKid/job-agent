from __future__ import annotations

from job_logging import ensure_process_logging

ensure_process_logging(__file__)

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from models import JobPosting


# ── Resume record returned from get_resume() ─────────────────────────────────

@dataclass
class ResumeRecord:
    id: str
    path: str
    created_at: str
    quality_score: float | None
    improvements: list[str]
    keywords: dict          # {titles[], skills[], domains[]}


# ── Main DB class ─────────────────────────────────────────────────────────────

class JobDB:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    # ── Schema init ───────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS postings (
                id TEXT PRIMARY KEY,
                company TEXT,
                title TEXT,
                url TEXT,
                source TEXT,
                posted_at TEXT,
                first_seen TEXT,
                last_seen TEXT,
                final_score REAL,
                tier INTEGER,
                match_reason TEXT,
                alerted INTEGER DEFAULT 0,
                applied INTEGER DEFAULT 0,
                applied_at TEXT
            );

            CREATE TABLE IF NOT EXISTS seen_hashes (
                hash TEXT PRIMARY KEY,
                first_seen TEXT
            );

            CREATE TABLE IF NOT EXISTS resumes (
                id            TEXT PRIMARY KEY,
                path          TEXT,
                created_at    TEXT,
                quality_score REAL,
                improvements  TEXT,
                keywords      TEXT
            );

            CREATE TABLE IF NOT EXISTS search_profiles (
                id         TEXT PRIMARY KEY,
                label      TEXT,
                created_at TEXT,
                is_default INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS profile_companies (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id TEXT REFERENCES search_profiles(id),
                name       TEXT,
                ats        TEXT,
                slug       TEXT,
                tier_boost REAL DEFAULT 1.0,
                active     INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS profile_sources (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id  TEXT REFERENCES search_profiles(id),
                source_type TEXT,
                config      TEXT,
                active      INTEGER DEFAULT 1
            );
            """
        )
        self.conn.commit()
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        """Add new columns to existing postings table safely, and add missing indexes."""
        # Unique index on profile_companies to prevent duplicate seeding
        try:
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_profile_company "
                "ON profile_companies(profile_id, ats, slug)"
            )
            self.conn.commit()
        except sqlite3.OperationalError:
            pass

        new_columns = [
            ("source_priority", "INTEGER DEFAULT 2"),
            ("location", "TEXT DEFAULT ''"),
            ("remote", "INTEGER DEFAULT 0"),
            ("salary_min", "INTEGER"),
            ("salary_max", "INTEGER"),
            ("salary_inferred", "INTEGER DEFAULT 0"),
            ("tier_boost", "REAL DEFAULT 1.0"),
            ("description", "TEXT"),
            ("embed_score", "REAL"),
            ("stage1_score", "REAL"),
            ("match_score", "REAL"),
            ("level_fit", "TEXT"),
            ("competition", "TEXT DEFAULT 'medium'"),
            ("embedded_flag", "INTEGER DEFAULT 0"),
            ("stage2_scored", "INTEGER DEFAULT 0"),
        ]
        for col_name, col_def in new_columns:
            try:
                self.conn.execute(f"ALTER TABLE postings ADD COLUMN {col_name} {col_def}")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists

    # ── Resume management ─────────────────────────────────────────────────────

    def store_resume(self, resume_id: str, path: str, haiku_result: dict) -> None:
        """Store a new resume record. Silently ignores if already exists."""
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            INSERT OR IGNORE INTO resumes(id, path, created_at, quality_score, improvements, keywords)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                resume_id,
                path,
                now,
                haiku_result.get("quality_score"),
                json.dumps(haiku_result.get("improvements", [])),
                json.dumps(haiku_result.get("keywords", {})),
            ),
        )
        self.conn.commit()

    def get_resume(self, resume_id: str) -> ResumeRecord | None:
        row = self.conn.execute(
            "SELECT * FROM resumes WHERE id=?", (resume_id,)
        ).fetchone()
        if not row:
            return None
        return ResumeRecord(
            id=row["id"],
            path=row["path"] or "",
            created_at=row["created_at"] or "",
            quality_score=row["quality_score"],
            improvements=json.loads(row["improvements"] or "[]"),
            keywords=json.loads(row["keywords"] or "{}"),
        )

    def list_resumes(self) -> list[ResumeRecord]:
        rows = self.conn.execute(
            "SELECT * FROM resumes ORDER BY created_at DESC"
        ).fetchall()
        return [
            ResumeRecord(
                id=r["id"],
                path=r["path"] or "",
                created_at=r["created_at"] or "",
                quality_score=r["quality_score"],
                improvements=json.loads(r["improvements"] or "[]"),
                keywords=json.loads(r["keywords"] or "{}"),
            )
            for r in rows
        ]

    # ── Search profile management ─────────────────────────────────────────────

    def profile_exists(self, profile_id: str) -> bool:
        row = self.conn.execute(
            "SELECT id FROM search_profiles WHERE id=?", (profile_id,)
        ).fetchone()
        return row is not None

    def create_profile(self, profile_id: str, label: str, is_default: bool = False) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT OR IGNORE INTO search_profiles(id, label, created_at, is_default) VALUES(?,?,?,?)",
            (profile_id, label, now, int(is_default)),
        )
        self.conn.commit()

    def get_default_profile_id(self) -> str | None:
        row = self.conn.execute(
            "SELECT id FROM search_profiles WHERE is_default=1 LIMIT 1"
        ).fetchone()
        return row["id"] if row else None

    def list_profiles(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, label, created_at, is_default FROM search_profiles ORDER BY is_default DESC, id"
        ).fetchall()
        result = []
        for r in rows:
            count = self.conn.execute(
                "SELECT COUNT(*) FROM profile_companies WHERE profile_id=? AND active=1",
                (r["id"],),
            ).fetchone()[0]
            result.append({
                "id": r["id"],
                "label": r["label"],
                "created_at": r["created_at"],
                "is_default": bool(r["is_default"]),
                "company_count": count,
            })
        return result

    # ── Profile companies ─────────────────────────────────────────────────────

    def seed_profile_companies(self, profile_id: str, companies: list[dict]) -> None:
        """Bulk-insert companies from target_companies.json format. Skips duplicates."""
        for c in companies:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO profile_companies(profile_id, name, ats, slug, tier_boost, active)
                VALUES (?, ?, ?, ?, ?, 1)
                """,
                (profile_id, c["name"], c["ats"], c.get("slug", ""), c.get("tier_boost", 1.0)),
            )
        self.conn.commit()

    def get_profile_companies(self, profile_id: str) -> list[dict]:
        """Return active companies for a profile in target_companies.json format."""
        rows = self.conn.execute(
            "SELECT name, ats, slug, tier_boost FROM profile_companies WHERE profile_id=? AND active=1",
            (profile_id,),
        ).fetchall()
        return [{"name": r["name"], "ats": r["ats"], "slug": r["slug"],
                 "tier_boost": r["tier_boost"]} for r in rows]

    def add_profile_company(
        self,
        profile_id: str,
        name: str,
        ats: str,
        slug: str,
        tier_boost: float = 1.0,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO profile_companies(profile_id, name, ats, slug, tier_boost, active)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            (profile_id, name, ats, slug, tier_boost),
        )
        self.conn.commit()

    def list_profile_companies(self, profile_id: str) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT id, name, ats, slug, tier_boost, active
            FROM profile_companies WHERE profile_id=?
            ORDER BY name
            """,
            (profile_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Profile sources ───────────────────────────────────────────────────────

    def seed_profile_sources(self, profile_id: str, sources_dict: dict) -> None:
        """
        Bulk-insert sources from source_seeds.json format.
        sources_dict = {"workday": [...], "theirstack": [...], ...}
        Each list item becomes one row with source_type=key, config=JSON(item).
        """
        for source_type, items in sources_dict.items():
            for item in items:
                self.conn.execute(
                    """
                    INSERT INTO profile_sources(profile_id, source_type, config, active)
                    VALUES (?, ?, ?, 1)
                    """,
                    (profile_id, source_type, json.dumps(item)),
                )
        self.conn.commit()

    def get_profile_sources(self, profile_id: str) -> list[dict]:
        """
        Return active sources for a profile.
        Each dict has: source_type (str), config (dict already parsed from JSON).
        """
        rows = self.conn.execute(
            "SELECT source_type, config FROM profile_sources WHERE profile_id=? AND active=1",
            (profile_id,),
        ).fetchall()
        return [{"source_type": r["source_type"], "config": json.loads(r["config"])} for r in rows]

    # ── Per-resume postings table ─────────────────────────────────────────────

    def _resume_table(self, resume_id: str) -> str:
        """Safe table name for a resume's postings. resume_id is a hex prefix — no injection risk."""
        return f"postings_{resume_id}"

    def create_resume_postings_table(self, resume_id: str) -> None:
        """Create the per-resume postings table if it doesn't exist."""
        table = self._resume_table(resume_id)
        self.conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS {table} (
                id              TEXT PRIMARY KEY,
                company         TEXT,
                title           TEXT,
                url             TEXT,
                source          TEXT,
                source_priority INTEGER DEFAULT 2,
                posted_at       TEXT,
                first_seen      TEXT,
                last_seen       TEXT,
                location        TEXT DEFAULT '',
                remote          INTEGER DEFAULT 0,
                salary_min      INTEGER,
                salary_max      INTEGER,
                salary_inferred INTEGER DEFAULT 0,
                tier_boost      REAL DEFAULT 1.0,
                description     TEXT,
                tier            INTEGER,
                embed_score     REAL,
                stage1_score    REAL,
                match_score     REAL,
                level_fit       TEXT,
                competition     TEXT DEFAULT 'medium',
                embedded_flag   INTEGER DEFAULT 0,
                stage2_scored   INTEGER DEFAULT 0,
                alerted         INTEGER DEFAULT 0,
                applied         INTEGER DEFAULT 0,
                applied_at      TEXT,
                match_reason    TEXT,
                final_score     REAL,
                claude_score    REAL,
                claude_reason   TEXT
            );
            """
        )
        self.conn.commit()

    def store_resume_candidate(self, resume_id: str, posting: JobPosting) -> None:
        """INSERT OR IGNORE a posting into the resume-specific table."""
        table = self._resume_table(resume_id)
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            f"""
            INSERT OR IGNORE INTO {table}(
                id, company, title, url, source, source_priority, posted_at,
                first_seen, last_seen, location, remote, salary_min, salary_max,
                salary_inferred, tier_boost, description, embed_score, stage1_score,
                match_score, level_fit, competition, embedded_flag,
                tier, match_reason, final_score, alerted, stage2_scored
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0
            )
            """,
            (
                posting.id,
                posting.company,
                posting.title,
                posting.url,
                posting.source,
                posting.source_priority,
                posting.posted_at.isoformat(),
                now,
                now,
                posting.location,
                int(posting.remote),
                posting.salary_min,
                posting.salary_max,
                int(posting.salary_inferred),
                posting.tier_boost,
                (posting.description or "")[:8000],
                posting.embed_score,
                posting.stage1_score,
                posting.match_score,
                posting.level_fit,
                posting.competition,
                int(posting.embedded_flag),
                posting.tier,
                posting.match_reason,
                posting.final_score,
            ),
        )
        self.conn.commit()

    def save_resume_stage1_scores(self, resume_id: str, postings: list[JobPosting]) -> None:
        """Persist stage1_score and embed_score after Stage 1 scoring."""
        table = self._resume_table(resume_id)
        self.conn.executemany(
            f"UPDATE {table} SET stage1_score=?, embed_score=? WHERE id=?",
            [(p.stage1_score, p.embed_score, p.id) for p in postings],
        )
        self.conn.commit()

    def get_above_threshold(self, resume_id: str, threshold: float) -> list[JobPosting]:
        """Return all postings in resume table with stage1_score >= threshold."""
        table = self._resume_table(resume_id)
        rows = self.conn.execute(
            f"""
            SELECT * FROM {table}
            WHERE stage1_score >= ?
            AND claude_score IS NULL
            AND description IS NOT NULL AND description != ''
            ORDER BY stage1_score DESC
            """,
            (threshold,),
        ).fetchall()
        return [self._row_to_posting(row) for row in rows]

    def write_claude_scores(self, resume_id: str, results: list[dict]) -> None:
        """
        Write Claude ranking scores back to the resume table.
        Each result dict: {id, claude_score, claude_reason, tier, level_fit, match_reason}
        """
        table = self._resume_table(resume_id)
        for r in results:
            self.conn.execute(
                f"""
                UPDATE {table} SET
                    claude_score=?, claude_reason=?, tier=?, level_fit=?,
                    match_reason=?, stage2_scored=1
                WHERE id=?
                """,
                (
                    r.get("claude_score"),
                    r.get("claude_reason") or r.get("match_reason"),
                    r.get("tier"),
                    r.get("level_fit"),
                    r.get("match_reason"),
                    r["id"],
                ),
            )
        self.conn.commit()

    def get_top_n_by_claude(self, resume_id: str, n: int = 100) -> list[dict]:
        """Return top N postings sorted by claude_score DESC as plain dicts."""
        table = self._resume_table(resume_id)
        rows = self.conn.execute(
            f"""
            SELECT id, company, title, url, source, location, salary_min, salary_max,
                   posted_at, tier, level_fit, stage1_score, claude_score,
                   claude_reason, match_reason, tier_boost, competition
            FROM {table}
            WHERE claude_score IS NOT NULL
            ORDER BY claude_score DESC
            LIMIT ?
            """,
            (n,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_resume_stats(self, resume_id: str) -> dict:
        """Return counts for the resume postings table."""
        table = self._resume_table(resume_id)
        try:
            total = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            above = self.conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE stage1_score >= 0.35"
            ).fetchone()[0]
            ranked = self.conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE claude_score IS NOT NULL"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            return {"total": 0, "above_threshold": 0, "claude_ranked": 0}
        return {"total": total, "above_threshold": above, "claude_ranked": ranked}

    # ── Legacy postings table methods (unchanged) ─────────────────────────────

    def reset_unalerted_scores(self) -> None:
        """Reset stage2_scored=0 for all unalerted postings so they are re-scored this run."""
        self.conn.execute("UPDATE postings SET stage2_scored=0 WHERE alerted=0")
        self.conn.commit()

    def store_candidate(self, posting: JobPosting) -> None:
        """INSERT OR IGNORE — does not overwrite if posting already exists."""
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            INSERT OR IGNORE INTO postings(
                id, company, title, url, source, source_priority, posted_at,
                first_seen, last_seen, location, remote, salary_min, salary_max,
                salary_inferred, tier_boost, description, embed_score, stage1_score,
                match_score, level_fit, competition, embedded_flag,
                tier, match_reason, final_score, alerted, stage2_scored
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0
            )
            """,
            (
                posting.id,
                posting.company,
                posting.title,
                posting.url,
                posting.source,
                posting.source_priority,
                posting.posted_at.isoformat(),
                now,
                now,
                posting.location,
                int(posting.remote),
                posting.salary_min,
                posting.salary_max,
                int(posting.salary_inferred),
                posting.tier_boost,
                (posting.description or "")[:8000],
                posting.embed_score,
                posting.stage1_score,
                posting.match_score,
                posting.level_fit,
                posting.competition,
                int(posting.embedded_flag),
                posting.tier,
                posting.match_reason,
                posting.final_score,
            ),
        )
        self.conn.commit()

    def get_unscored(self) -> list[JobPosting]:
        rows = self.conn.execute(
            "SELECT * FROM postings WHERE stage2_scored=0 AND description IS NOT NULL AND description != ''"
        ).fetchall()
        return [self._row_to_posting(row) for row in rows]

    def mark_scored(self, posting: JobPosting) -> None:
        self.conn.execute(
            """
            UPDATE postings SET
                match_score=?, match_reason=?, level_fit=?, tier=?,
                embed_score=?, stage1_score=?, competition=?, stage2_scored=1
            WHERE id=?
            """,
            (
                posting.match_score,
                posting.match_reason,
                posting.level_fit,
                posting.tier,
                posting.embed_score,
                posting.stage1_score,
                posting.competition,
                posting.id,
            ),
        )
        self.conn.commit()

    def get_unalerted_scored(self) -> list[JobPosting]:
        rows = self.conn.execute(
            "SELECT * FROM postings WHERE alerted=0 AND stage2_scored=1"
        ).fetchall()
        return [self._row_to_posting(row) for row in rows]

    def save_stage1_scores(self, postings: list[JobPosting]) -> None:
        self.conn.executemany(
            "UPDATE postings SET stage1_score=?, embed_score=? WHERE id=?",
            [(p.stage1_score, p.embed_score, p.id) for p in postings],
        )
        self.conn.commit()

    def save_final_score(self, posting: JobPosting) -> None:
        self.conn.execute(
            "UPDATE postings SET final_score=? WHERE id=?",
            (posting.final_score, posting.id),
        )
        self.conn.commit()

    def mark_alerted(self, posting: JobPosting) -> None:
        self.conn.execute(
            "UPDATE postings SET alerted=1 WHERE id=?",
            (posting.id,),
        )
        self.conn.commit()

    def seen_recently(self, job_hash: str) -> bool:
        row = self.conn.execute(
            "SELECT hash FROM seen_hashes WHERE hash=?", (job_hash,)
        ).fetchone()
        return row is not None

    def mark_seen(self, job_hash: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT OR IGNORE INTO seen_hashes(hash, first_seen) VALUES(?, ?)",
            (job_hash, now),
        )
        self.conn.commit()

    def upsert_posting(self, posting: JobPosting) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            INSERT INTO postings(id, company, title, url, source, posted_at, first_seen, last_seen, final_score, tier, match_reason, alerted)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(id) DO UPDATE SET
                company=excluded.company,
                title=excluded.title,
                url=excluded.url,
                source=excluded.source,
                posted_at=excluded.posted_at,
                last_seen=excluded.last_seen,
                final_score=excluded.final_score,
                tier=excluded.tier,
                match_reason=excluded.match_reason
            """,
            (
                posting.id,
                posting.company,
                posting.title,
                posting.url,
                posting.source,
                posting.posted_at.isoformat(),
                now,
                now,
                posting.final_score,
                posting.tier,
                posting.match_reason,
            ),
        )
        self.conn.commit()

    def _row_to_posting(self, row: sqlite3.Row) -> JobPosting:
        posted_at_str = row["posted_at"]
        if posted_at_str:
            posted_at = datetime.fromisoformat(posted_at_str)
            if posted_at.tzinfo is None:
                posted_at = posted_at.replace(tzinfo=timezone.utc)
        else:
            posted_at = datetime.now(timezone.utc)

        return JobPosting(
            id=row["id"],
            company=row["company"] or "",
            title=row["title"] or "",
            url=row["url"] or "",
            source=row["source"] or "",
            source_priority=row["source_priority"] if row["source_priority"] is not None else 2,
            posted_at=posted_at,
            description=row["description"] or "",
            location=row["location"] or "",
            remote=bool(row["remote"]),
            salary_min=row["salary_min"],
            salary_max=row["salary_max"],
            salary_inferred=bool(row["salary_inferred"]),
            embed_score=row["embed_score"],
            stage1_score=row["stage1_score"],
            match_score=row["match_score"],
            tier=row["tier"],
            match_reason=row["match_reason"],
            level_fit=row["level_fit"],
            embedded_flag=bool(row["embedded_flag"]),
            final_score=row["final_score"],
            competition=row["competition"] or "medium",
            tier_boost=row["tier_boost"] if row["tier_boost"] is not None else 1.0,
        )

    def close(self) -> None:
        self.conn.close()
