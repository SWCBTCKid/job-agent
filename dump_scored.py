import sqlite3
from pathlib import Path

conn = sqlite3.connect("state/jobs.db")
conn.row_factory = sqlite3.Row
rows = conn.execute("""
    SELECT company, title, location, salary_min, salary_max,
           match_score, match_reason, level_fit, tier, url
    FROM postings
    WHERE stage2_scored=1
    ORDER BY match_score DESC, tier ASC
""").fetchall()
conn.close()

lines = []
lines.append(f"Haiku Stage 2 Results — {len(rows)} scored jobs")
lines.append("Sorted by match_score DESC (scores stored as 0-10, Haiku rated 0-100 then /10)")
lines.append("=" * 110)

for i, r in enumerate(rows, 1):
    sal = ""
    if r["salary_min"] and r["salary_max"]:
        sal = f"${r['salary_min']:,}-${r['salary_max']:,}"
    elif r["salary_min"]:
        sal = f"${r['salary_min']:,}+"
    elif r["salary_max"]:
        sal = f"up to ${r['salary_max']:,}"

    score_pct = int((r["match_score"] or 0) * 10)  # back to 0-100 for readability

    lines.append(f"\n{i:3}. [{score_pct}/100] [{r['company']}] {r['title']}")
    lines.append(f"     tier={r['tier']} | level={r['level_fit']} | {sal or 'salary unknown'} | {r['location'] or 'location unknown'}")
    lines.append(f"     {r['url']}")
    lines.append(f"     {r['match_reason'] or 'No reasoning stored.'}")

out = Path("state/scored_results.txt")
out.write_text("\n".join(lines), encoding="utf-8")
print(f"Written {len(rows)} results to {out}")
print(f"\nTop 20 preview:")
for r in rows[:20]:
    score_pct = int((r["match_score"] or 0) * 10)
    print(f"  [{score_pct}/100] [{r['company']}] {r['title']}")
