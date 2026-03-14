import sqlite3
from pathlib import Path

db_path = Path("state/jobs.db")
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

cur.execute("""
    SELECT company, title, location, salary_min, salary_max, source,
           stage1_score, match_score, final_score, competition,
           posted_at, url, alerted, stage2_scored
    FROM postings
    ORDER BY COALESCE(final_score, stage1_score, 0) DESC
""")
rows = cur.fetchall()
conn.close()

lines = []
lines.append(f"Total postings in DB: {len(rows)}")
lines.append("=" * 100)

for i, r in enumerate(rows, 1):
    if r["salary_min"] and r["salary_max"]:
        sal = f"${r['salary_min']:,}-${r['salary_max']:,}"
    elif r["salary_min"]:
        sal = f"${r['salary_min']:,}+"
    elif r["salary_max"]:
        sal = f"up to ${r['salary_max']:,}"
    else:
        sal = "unknown"

    s1 = f"{r['stage1_score']:.3f}" if r["stage1_score"] else "-"
    s2 = f"{r['match_score']:.3f}" if r["match_score"] else "-"
    sf = f"{r['final_score']:.3f}" if r["final_score"] else "-"
    scored = "scored" if r["stage2_scored"] else "unscored"
    alerted = "| alerted" if r["alerted"] else ""

    lines.append(f"{i:4}. [{r['company']}] {r['title']}")
    lines.append(f"      loc={r['location'] or 'unknown'} | sal={sal} | src={r['source']} | {scored} {alerted}")
    lines.append(f"      stage1={s1} | stage2={s2} | final={sf} | competition={r['competition'] or '-'}")
    lines.append(f"      {r['url']}")
    lines.append("")

out_path = Path("state/all_postings.txt")
out_path.write_text("\n".join(lines), encoding="utf-8")
print(f"Written {len(rows)} postings to {out_path}")
