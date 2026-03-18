"""
dashboard.py — Local job search dashboard.

Serves a web UI to browse, filter, and track application status for ranked jobs.

Usage:
    python dashboard.py
    python dashboard.py --port 8080
    python dashboard.py --resume-id 4ca85c675bad
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from config import STATE_DIR

DB_PATH = STATE_DIR / "jobs.db"

app = Flask(__name__)


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _migrate_dashboard_columns(conn: sqlite3.Connection, table: str) -> None:
    """Add status and notes columns if missing."""
    for col, defn in [("status", "TEXT DEFAULT 'new'"), ("notes", "TEXT DEFAULT ''")]:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
        except sqlite3.OperationalError:
            pass
    conn.commit()


def _resume_table(resume_id: str) -> str:
    return f"postings_{resume_id}"


# ── API ───────────────────────────────────────────────────────────────────────

@app.get("/api/resumes")
def api_resumes():
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, path, quality_score, created_at FROM resumes ORDER BY created_at DESC"
    ).fetchall()
    result = []
    for r in rows:
        table = _resume_table(r["id"])
        # Check if table exists
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        stats = {"total": 0, "ranked": 0, "applied": 0}
        if exists:
            _migrate_dashboard_columns(conn, table)
            stats["total"] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            stats["ranked"] = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE claude_score IS NOT NULL"
            ).fetchone()[0]
            stats["applied"] = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE status NOT IN ('new', 'skipped')"
            ).fetchone()[0]
        result.append({
            "id": r["id"],
            "path": Path(r["path"] or "").name,
            "quality_score": r["quality_score"],
            "created_at": (r["created_at"] or "")[:10],
            **stats,
        })
    conn.close()
    return jsonify(result)


@app.get("/api/jobs")
def api_jobs():
    resume_id = request.args.get("resume_id", "")
    if not resume_id:
        return jsonify([])

    # Filters
    min_score   = float(request.args.get("min_score", 0))
    levels      = request.args.getlist("level")       # e.g. ["senior","mid"]
    tiers       = request.args.getlist("tier")        # e.g. ["1","2"]
    statuses    = request.args.getlist("status")      # e.g. ["new","applied"]
    posted_after = request.args.get("posted_after", "")  # YYYY-MM-DD
    search      = request.args.get("q", "").strip().lower()
    sort_by     = request.args.get("sort", "claude_score")
    sort_dir    = request.args.get("dir", "desc").upper()
    limit       = int(request.args.get("limit", 500))

    allowed_sorts = {"claude_score", "stage1_score", "posted_at", "company", "title", "status"}
    if sort_by not in allowed_sorts:
        sort_by = "claude_score"
    if sort_dir not in ("ASC", "DESC"):
        sort_dir = "DESC"

    table = _resume_table(resume_id)
    conn = get_conn()

    # Ensure table exists
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not exists:
        conn.close()
        return jsonify([])

    _migrate_dashboard_columns(conn, table)

    wheres = ["claude_score IS NOT NULL"]
    params: list = []

    if min_score > 0:
        wheres.append("claude_score >= ?")
        params.append(min_score)

    if levels:
        placeholders = ",".join("?" * len(levels))
        wheres.append(f"level_fit IN ({placeholders})")
        params.extend(levels)

    if tiers:
        placeholders = ",".join("?" * len(tiers))
        wheres.append(f"tier IN ({placeholders})")
        params.extend([int(t) for t in tiers])

    if statuses:
        placeholders = ",".join("?" * len(statuses))
        wheres.append(f"COALESCE(status,'new') IN ({placeholders})")
        params.extend(statuses)

    if posted_after:
        wheres.append("posted_at >= ?")
        params.append(posted_after)

    where_clause = " AND ".join(wheres)

    rows = conn.execute(
        f"""
        SELECT id, company, title, url, location, salary_min, salary_max,
               posted_at, tier, level_fit, stage1_score, claude_score,
               match_reason, status, notes, source
        FROM {table}
        WHERE {where_clause}
        ORDER BY {sort_by} {sort_dir}
        LIMIT {limit}
        """,
        params,
    ).fetchall()
    conn.close()

    results = []
    for r in rows:
        row = dict(r)
        # Apply search filter in Python (simpler than LIKE for multi-field)
        if search:
            haystack = f"{row['company']} {row['title']} {row['location'] or ''}".lower()
            if search not in haystack:
                continue
        # Format salary
        if row["salary_min"] and row["salary_max"]:
            row["salary"] = f"${row['salary_min']:,}–${row['salary_max']:,}"
        elif row["salary_min"]:
            row["salary"] = f"${row['salary_min']:,}+"
        elif row["salary_max"]:
            row["salary"] = f"up to ${row['salary_max']:,}"
        else:
            row["salary"] = ""
        row["posted_at"] = (row["posted_at"] or "")[:10]
        row["status"] = row["status"] or "new"
        results.append(row)

    return jsonify(results)


@app.get("/api/stats")
def api_stats():
    resume_id = request.args.get("resume_id", "")
    if not resume_id:
        return jsonify({})
    table = _resume_table(resume_id)
    conn = get_conn()
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not exists:
        conn.close()
        return jsonify({})
    _migrate_dashboard_columns(conn, table)
    stats = {}
    for status in ("new", "applied", "interviewing", "offer", "rejected", "skipped"):
        stats[status] = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE COALESCE(status,'new')=? AND claude_score IS NOT NULL",
            (status,),
        ).fetchone()[0]
    stats["total_ranked"] = conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE claude_score IS NOT NULL"
    ).fetchone()[0]
    conn.close()
    return jsonify(stats)


@app.patch("/api/jobs/<job_id>/status")
def api_update_status(job_id: str):
    resume_id = request.args.get("resume_id", "")
    if not resume_id:
        return jsonify({"error": "resume_id required"}), 400
    data = request.get_json(force=True)
    status = data.get("status", "new")
    valid = {"new", "applied", "interviewing", "offer", "rejected", "skipped"}
    if status not in valid:
        return jsonify({"error": f"invalid status: {status}"}), 400

    table = _resume_table(resume_id)
    conn = get_conn()
    _migrate_dashboard_columns(conn, table)
    conn.execute(f"UPDATE {table} SET status=? WHERE id=?", (status, job_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.patch("/api/jobs/<job_id>/notes")
def api_update_notes(job_id: str):
    resume_id = request.args.get("resume_id", "")
    if not resume_id:
        return jsonify({"error": "resume_id required"}), 400
    data = request.get_json(force=True)
    notes = str(data.get("notes", ""))[:500]
    table = _resume_table(resume_id)
    conn = get_conn()
    _migrate_dashboard_columns(conn, table)
    conn.execute(f"UPDATE {table} SET notes=? WHERE id=?", (notes, job_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.get("/")
def index():
    return render_template("index.html")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Job search dashboard")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    print(f"\n  Dashboard running at http://{args.host}:{args.port}\n")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
