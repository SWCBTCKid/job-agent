# Job Search Agent — Architecture

---

## System Overview

```
job-agent --resume /path/to/resume.txt
         │
         ▼
┌────────────────────────────────────────────┐
│  STEP 1 — RESUME INGEST  (sync, Haiku)    │
│  • sha256(content) → check resumes table  │
│  • Duplicate detected → reuse, skip Haiku │
│  • New → Haiku: quality score (0–10),     │
│    improvement suggestions, keyword tags  │
│  • Print score + improvements to terminal │
│  • resume_id stored in DB                 │
└────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────┐
│  STEP 2 — SCRAPE + FILTER + STAGE 1  (sync)     │
│  • Scrape all sources for this search profile   │
│    (profile_companies + profile_sources tables) │
│  • Hard filters: Bay Area, salary floor,        │
│    role type gate, clearance rejection          │
│  • Stage 1: embed each posting vs THIS resume   │
│  • Store all candidates in postings_{resume_id} │
│  • Select postings where                        │
│    stage1_score >= STAGE1_THRESHOLD (0.35)      │
└──────────────────────────────────────────────────┘
         │  candidates above threshold
         ▼
┌────────────────────────────────────────────────────┐
│  STEP 3 — CLAUDE RANKING  (background process)   │
│  • Claude Sonnet scores every posting above      │
│    threshold against THIS resume                │
│  • claude_score + match_reason written back to  │
│    postings_{resume_id}                         │
│  • Sort by claude_score DESC                    │
│  • Write top 100 → output/results_{id}_{ts}.json│
│  • Telegram: "Done — N ranked, top match:       │
│    {title} @ {company} ({score})"              │
│  • Any error → log stats, Telegram partial msg  │
└────────────────────────────────────────────────────┘
```

---

## File Structure

```
D:/Claude Trading Agent/job-agent/
│
├── main.py                      # Entry point — CLI arg parsing + flow orchestration
├── config.py                    # Settings: API keys, thresholds, weights
├── db.py                        # SQLite interface: all tables, resume + profile management
├── models.py                    # JobPosting dataclass
├── resume_ingest.py             # [NEW] Haiku resume scoring + keyword extraction
├── output_writer.py             # [NEW] Top-100 JSON output file writer
├── job_logging.py               # Per-run timestamped file logging
│
├── scrapers/
│   ├── base.py                  # Abstract base scraper
│   ├── greenhouse.py            # Greenhouse public API
│   ├── lever.py                 # Lever public API
│   ├── ashby.py                 # Ashby public API
│   ├── hn.py                    # HN "Who's Hiring" thread parser
│   ├── workday.py               # Workday HTML scraper
│   ├── wellfound.py             # Wellfound (AngelList Talent) scraper
│   ├── yc.py                    # workatastartup.com scraper
│   ├── pragmatic.py             # Pragmatic Engineer RSS feed
│   ├── builtinsf.py             # Builtin SF cross-company discovery
│   ├── icims.py                 # iCIMS ATS scraper (Joby Aviation)
│   ├── theirstack.py            # TheirStack API (JS-rendered/no-ATS companies)
│   ├── serp.py                  # SerpAPI Google Jobs
│   └── linkedin.py              # LinkedIn (last resort)
│
├── matcher/
│   ├── embedder.py              # Stage 1: multi-signal scoring (embed + skill + domain + freshness)
│   ├── claude_matcher.py        # Stage 2: Claude batch ranking against resume
│   └── ranker.py                # Composite final score formula
│
├── notifier.py                  # Telegram send helpers (digest + background completion alert)
├── salary.py                    # Salary inference (levels.fyi data)
│
├── data/
│   ├── resume.txt               # Active resume (used as fallback if no --resume flag)
│   ├── target_companies.json    # Legacy: seeded into profile_companies on first run
│   ├── source_seeds.json        # Legacy: seeded into profile_sources on first run
│   └── salary_data.json         # Salary lookup table
│
├── output/                      # [NEW] Results files: results_{resume_id}_{ts}.json
│
├── state/
│   ├── jobs.db                  # SQLite database (all tables)
│   └── logs/                    # Per-run timestamped logs
│
├── ARCHITECTURE.md              # This file
├── AGENT_REQUIREMENTS.md        # Requirements and design decisions
└── CHANGELOG.md                 # Change history
```

---

## Database Schema

### `resumes` — Resume instances (one row per unique resume content)

```sql
CREATE TABLE IF NOT EXISTS resumes (
    id            TEXT PRIMARY KEY,   -- sha256(content)[:12]
    path          TEXT,               -- original file path provided at CLI
    created_at    TEXT,               -- ISO timestamp
    quality_score REAL,               -- Haiku quality rating 0–10
    improvements  TEXT,               -- JSON array of improvement suggestions
    keywords      TEXT                -- JSON: {titles[], skills[], domains[]}
);
```

**Deduplication:** `sha256(resume_content)[:12]` as primary key. Pointing to the same
file twice, or two files with identical content, reuses the existing record and skips
the Haiku call.

---

### `search_profiles` — Named search configurations (who to scrape for)

```sql
CREATE TABLE IF NOT EXISTS search_profiles (
    id          TEXT PRIMARY KEY,   -- e.g. "gpu_autonomous", "backend", "frontend"
    label       TEXT,               -- human-readable: "GPU / Autonomous Vehicles"
    created_at  TEXT,
    is_default  INTEGER DEFAULT 0   -- 1 = used when no --profile flag provided
);
```

Each profile has its own company list and source seeds. A backend engineer's profile
targets different companies than an autonomous-vehicle/AI chip engineer's profile.
Profiles are independent — same resume can run against multiple profiles.

---

### `profile_companies` — Target companies per profile

```sql
CREATE TABLE IF NOT EXISTS profile_companies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id  TEXT REFERENCES search_profiles(id),
    name        TEXT,
    ats         TEXT,               -- "greenhouse" | "lever" | "ashby" | "workday" | "icims"
    slug        TEXT,               -- ATS-specific identifier
    tier_boost  REAL DEFAULT 1.0,
    active      INTEGER DEFAULT 1
);
```

Replaces `data/target_companies.json`. On first run, the JSON is seeded into this table
under the default profile.

---

### `profile_sources` — Source seeds per profile

```sql
CREATE TABLE IF NOT EXISTS profile_sources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id  TEXT REFERENCES search_profiles(id),
    source_type TEXT,               -- "theirstack" | "serp" | "icims" | "workday" | "wellfound" | "yc" | "pragmatic"
    config      TEXT,               -- JSON blob matching existing source_seeds.json structure
    active      INTEGER DEFAULT 1
);
```

Replaces `data/source_seeds.json`. Allows a backend-focused profile to have different
TheirStack domain lists, SerpAPI queries, and company scraping targets than the
GPU/autonomous profile.

---

### `postings_{resume_id}` — Per-resume candidate postings

Dynamic table created on first use for each resume instance. Schema identical to
the current `postings` table with two additional columns:

```sql
CREATE TABLE IF NOT EXISTS postings_{resume_id} (
    -- All existing postings columns:
    id TEXT PRIMARY KEY, company TEXT, title TEXT, url TEXT,
    source TEXT, source_priority INTEGER, posted_at TEXT,
    first_seen TEXT, last_seen TEXT, location TEXT, remote INTEGER,
    salary_min INTEGER, salary_max INTEGER, salary_inferred INTEGER,
    tier_boost REAL, description TEXT, tier INTEGER,
    embed_score REAL, stage1_score REAL, match_score REAL,
    level_fit TEXT, competition TEXT, embedded_flag INTEGER,
    stage2_scored INTEGER DEFAULT 0, alerted INTEGER DEFAULT 0,
    applied INTEGER DEFAULT 0, applied_at TEXT, match_reason TEXT,
    -- New columns:
    claude_score  REAL,             -- Stage 2 final score (0–10)
    claude_reason TEXT              -- Claude's match rationale
);
```

Rationale for table-per-resume over a shared table with `resume_id` column:
- Same posting has entirely different scores against different resumes
- Queries and output are per-resume by default — no `WHERE resume_id = ?` everywhere
- Easy to drop/reset a resume's results without touching others

---

### `seen_hashes` — Cross-run deduplication

```sql
CREATE TABLE IF NOT EXISTS seen_hashes (
    hash       TEXT PRIMARY KEY,
    first_seen TEXT
);
```

---

## CLI

```bash
# Full run — ingest resume, scrape default profile, rank, write output
python main.py --resume /path/to/resume.txt

# Run with a specific search profile
python main.py --resume /path/to/resume.txt --profile backend

# Re-run with existing resume (auto-detects duplicate by hash, skips Haiku)
python main.py --resume /path/to/resume.txt

# Dry run — skip Telegram, skip marking records as alerted
python main.py --resume /path/to/resume.txt --dry-run

# Print machine-readable output to stdout
python main.py --resume /path/to/resume.txt --json

# List all resume instances
python main.py --list-resumes

# List available search profiles
python main.py --list-profiles
```

---

## Component Detail

### 1. Resume Ingest (`resume_ingest.py`)

Called at the start of every run. Fast path if resume already seen.

```python
def ingest_resume(path: str, db: JobDB) -> ResumeRecord:
    content = Path(path).read_text(encoding="utf-8")
    resume_id = sha256(content.encode())[:12]

    existing = db.get_resume(resume_id)
    if existing:
        print(f"Resume already known (id={resume_id}) — skipping Haiku call")
        return existing

    result = _call_haiku(content)   # quality score + improvements + keywords
    db.store_resume(resume_id, path, result)
    _print_improvements(result)
    return result
```

**Haiku prompt produces:**
```json
{
  "quality_score": 7.2,
  "improvements": [
    "Add quantified impact to Meta authorization work",
    "Skills section missing explicit language list",
    "Lead with scale numbers — bury the Hitachi brand, lead with 30M services"
  ],
  "keywords": {
    "titles": ["software engineer", "platform engineer", "SRE", "production engineer"],
    "skills": ["distributed systems", "kubernetes", "observability", "rust", "c++"],
    "domains": ["infrastructure", "platform", "reliability", "security", "safety-critical"]
  }
}
```

Keywords are stored in the `resumes` table and used to enrich the embedding query vector
at Stage 1 — not as hard keyword filters.

---

### 2. Search Profile Resolution

```python
def build_scrapers(profile_id: str, db: JobDB) -> list[BaseScraper]:
    companies = db.get_profile_companies(profile_id)
    sources   = db.get_profile_sources(profile_id)
    # Build scraper instances using profile data instead of JSON files
    ...
```

First run auto-seeds default profile from `target_companies.json` + `source_seeds.json`.
Subsequent runs use DB values. New profiles can be added via direct DB insert or a
future `--add-profile` CLI command.

---

### 3. Stage 1 — Multi-Signal Embedding Filter

Run in Step 2 (sync). Scores every posting that passes hard filters. All scores written
to `postings_{resume_id}`.

```
stage1_score = max(0.0, base_score × role_multiplier − anti_pattern_penalty)

base_score = embedding_sim × 0.45
           + skill_overlap × 0.25
           + domain_score  × 0.20
           + freshness     × 0.10
```

Postings with `stage1_score >= STAGE1_THRESHOLD` (default 0.35, env: `JOB_AGENT_STAGE1_THRESHOLD`)
advance to Stage 2 Claude ranking. The threshold is the primary lever controlling Claude spend.

---

### 4. Stage 2 — Claude Ranking (Background Process)

Runs as a background subprocess after Step 2 completes. Parent process returns immediately
after launching the subprocess.

```python
# parent (main.py)
proc = subprocess.Popen([sys.executable, "ranker_worker.py",
                         "--resume-id", resume_id,
                         "--profile-id", profile_id])
print(f"Ranking started in background (pid={proc.pid})")
print("You will be notified via Telegram when complete.")

# ranker_worker.py
candidates = db.get_above_threshold(resume_id, SETTINGS.stage1_threshold)
results    = await claude_rank(resume_text, candidates)
db.write_claude_scores(resume_id, results)
write_output_file(resume_id, results)
await telegram_notify(resume_id, results)
```

On any error: log exception, write partial output file, send Telegram error message.
Never silently drops results.

---

### 5. Claude Ranking Prompt

```
CANDIDATE RESUME:
{resume_text}

JOB POSTINGS (JSON array, {N} items):
{postings}

For each posting return a JSON array with one object per posting:
{
  "id": "<posting id>",
  "claude_score": <0.0–10.0>,
  "tier": <1|2|3>,
  "match_reason": "<one sentence — why this fits the candidate specifically>",
  "risk": "<one sentence — biggest concern or mismatch>",
  "level_fit": "<senior|mid|staff|too_junior|too_senior>"
}

Scoring guide:
- 8–10: Direct match — candidate should prioritise immediately
- 6–7:  Strong match — worth applying
- 4–5:  Partial match — apply if volume is low
- 0–3:  Poor match — skip

Do NOT be generous. Score honestly. 7+ means candidate would likely pass a screen.
```

---

### 6. Output File

Written to `output/results_{resume_id}_{YYYYMMDD_HHMMSS}.json`.

```json
{
  "resume_id": "abc123def456",
  "resume_path": "/path/to/resume.txt",
  "profile_id": "gpu_autonomous",
  "generated_at": "2026-03-14T10:00:00Z",
  "stats": {
    "total_scraped": 842,
    "after_hard_filters": 310,
    "above_stage1_threshold": 87,
    "claude_ranked": 87,
    "output_count": 87
  },
  "results": [
    {
      "rank": 1,
      "company": "Anduril",
      "title": "Senior Platform Engineer",
      "url": "https://...",
      "claude_score": 8.4,
      "stage1_score": 0.884,
      "tier": 1,
      "level_fit": "senior",
      "match_reason": "Fleet-scale OTA and safety enforcement maps directly to mission systems reliability.",
      "risk": "May require US citizenship for clearance roles.",
      "location": "San Francisco, CA",
      "salary_min": 180000,
      "salary_max": 240000,
      "posted_at": "2026-03-12",
      "source": "greenhouse"
    }
  ]
}
```

---

### 7. Telegram Notifications

Two notification types:

**Completion (background process):**
```
✅ Job ranking complete
Resume: abc123 | Profile: gpu_autonomous
87 jobs ranked | Top match: Anduril — Senior Platform Engineer (8.4)
Output: output/results_abc123_20260314_100000.json
```

**Error (partial completion):**
```
⚠️ Job ranking partial — error encountered
Ranked 43/87 before failure: <error message>
Partial output written to output/results_abc123_20260314_100000.json
```

---

## Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Embedding model | voyage-code-2 | Better than OpenAI for technical/systems JDs |
| Stage 1 LLM | Claude Haiku | Resume ingest only — fast and cheap |
| Stage 2 LLM | Claude Sonnet 4.6 | Best structured output, understands technical context |
| Database | SQLite | No external deps, consistent with trading agent |
| Table-per-resume | `postings_{resume_id}` | Scores are resume-specific; avoids cross-contamination |
| Profile-per-use-case | `search_profiles` + `profile_companies` | Backend/frontend/AI engineer each get their own target list |
| Stage 1 as threshold | score >= 0.35 | Variable volume to Claude vs. fixed top-N — adapts to search quality |
| Background ranking | subprocess fire-and-forget | Scrape step gives fast feedback; ranking runs while user does other work |
| Output to file | JSON, top-100 by claude_score | Durable, queryable, no 4096-char Telegram limit |
| Resume dedup | sha256(content)[:12] | Same file or same content → reuse record, skip Haiku |

---

## Configuration (`config.py` / `.env`)

```
ANTHROPIC_API_KEY=...
VOYAGE_API_KEY=...             # voyage-code-2 embeddings
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
THEIRSTACK_API_KEY=...
SERPAPI_KEY=...

JOB_AGENT_CLAUDE_MODEL=claude-sonnet-4-6
JOB_AGENT_HAIKU_MODEL=claude-haiku-4-5-20251001
JOB_AGENT_STAGE1_THRESHOLD=0.35   # min stage1_score to send to Claude
JOB_AGENT_TOP_N_OUTPUT=100        # max results in output file
JOB_AGENT_SALARY_FLOOR=150000
```

---

## Search Profile Examples

### `gpu_autonomous` (default — current configuration)
- **Companies:** Anduril, Waymo, Aurora, Figure AI, Tenstorrent, SambaNova, etc.
- **TheirStack:** AI chip domains, AV/robotics domains
- **SerpAPI:** "software engineer autonomous vehicle Bay Area", "platform engineer robotics SF"
- **Tier focus:** Safety-critical, AI inference, autonomous systems

### `backend` (example — different user)
- **Companies:** Stripe, Airbnb, Notion, Linear, PlanetScale, Cockroach Labs, etc.
- **TheirStack:** fintech, developer tooling domains
- **SerpAPI:** "senior backend engineer SF", "distributed systems engineer fintech"
- **Tier focus:** API platforms, data infrastructure, developer experience

### `frontend` (example — different user)
- **Companies:** Figma, Vercel, Linear, Shopify, etc.
- **TheirStack:** product-led growth companies
- **SerpAPI:** "senior frontend engineer React SF", "staff engineer web platform"
- **Tier focus:** web platform, design systems, performance

---

## Open Items

1. **`ranker_worker.py`** — background subprocess entry point to be created
2. **Profile seeding CLI** — `--add-company`, `--add-profile` for managing profiles without direct DB edits
3. **Wellfound auth** — partially gated; public search page scraping as fallback
4. **Workday reliability** — blocks scrapers aggressively; treat as best-effort
5. **HN thread timing** — "Who's Hiring" posts first business day of month; agent should detect and prioritise
6. **Stage 1 threshold tuning** — 0.35 is the starting value; adjust based on observed Claude input volume after first runs
