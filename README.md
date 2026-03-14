# Job Agent

Multi-stage AI job scraping and ranking pipeline. Scrapes 13 ATS sources, scores candidates with a multi-signal Stage 1 filter, then ranks with Claude Haiku. Results delivered as JSON and Telegram notification.

---

## Setup

**1. Install dependencies**
```bash
pip install -r requirements.txt
```

**2. Configure environment**
```bash
cp .env.example .env
# Fill in your keys in .env
```

**3. Add your resume**

Drop your resume as a plain text file at:
```
data/resume.txt
```

---

## Commands

### Full pipeline

```bash
# Ingest resume → scrape → Stage 1 filter → launch Haiku ranker in background
python main.py --resume data/resume.txt

# Dry run — scrape and score but do not write results or launch ranker
python main.py --resume data/resume.txt --dry-run

# Use a specific search profile (default: gpu_autonomous)
python main.py --resume data/resume.txt --profile gpu_autonomous

# Print run summary as JSON
python main.py --resume data/resume.txt --dry-run --json
```

### Profile management

```bash
# List all loaded profiles
python main.py --list-profiles

# List companies in the active profile
python main.py --list-companies

# List all ingested resumes with stats
python main.py --list-resumes

# Add a company to a profile
python main.py --add-company

# Import a profile from JSON
python main.py --import-profile <path>
```

### Ranker (normally launched automatically by main.py)

```bash
# Rank all candidates for a resume
python ranker_worker.py --resume-id <12-char-hash> --profile-id gpu_autonomous

# Limit to first N postings (for testing)
python ranker_worker.py --resume-id <id> --profile-id gpu_autonomous --limit 10

# Set rate limit (default: 20 RPM)
python ranker_worker.py --resume-id <id> --profile-id gpu_autonomous --rpm 10
```

Get `--resume-id` from `python main.py --list-resumes`.

---

## Pipeline

```
data/resume.txt
  └── Stage 0: Haiku ingests resume → quality score, keywords, content hash (resume_id)
        └── Stage 1: 13 scrapers → role/salary/location/clearance filters → multi-signal scorer
              └── Stage 2: Haiku ranks all above threshold → claude_score (0–10), level_fit, tier, risk
                    └── output/results_{resume_id}_{timestamp}.json + Telegram notification
```

**Stage 1 scoring formula:**
```
stage1_score = max(0, base × role_multiplier − anti_pattern_penalty)
base = embedding_sim×0.45 + skill_overlap×0.25 + domain_score×0.20 + freshness×0.10
```

**Level weights** (applied to final score):

| level_fit   | weight |
|-------------|--------|
| mid         | 1.00   |
| senior      | 0.90   |
| staff       | 0.50   |
| too_senior  | 0.30   |
| too_junior  | 0.10   |

---

## Scrapers

| Source       | Type             | Notes                              |
|--------------|------------------|------------------------------------|
| Greenhouse   | ATS API          | Targeted company list              |
| Lever        | ATS API          | Targeted company list              |
| Ashby        | ATS API          | Targeted company list              |
| iCIMS        | ATS HTML         | Joby Aviation                      |
| Workday      | HTML parser      | SpaceX, CrowdStrike, Nvidia, etc.  |
| TheirStack   | API              | Companies without ATS coverage     |
| SerpAPI      | Google Jobs API  | Broad search queries               |
| BuiltinSF    | HTML parser      | Cross-company Bay Area discovery   |
| HN           | Algolia API      | Who's Hiring threads               |
| Wellfound    | HTML parser      | Best-effort                        |
| YC           | HTML parser      | workatastartup.com                 |
| Pragmatic    | RSS feed         | Pragmatic Engineer job board       |
| LinkedIn     | HTML parser      | Last resort                        |

---

## Environment Variables

| Variable                  | Required | Description                              |
|---------------------------|----------|------------------------------------------|
| `ANTHROPIC_API_KEY`       | Yes      | Claude API key                           |
| `TELEGRAM_BOT_TOKEN`      | No       | Telegram bot token for notifications     |
| `TELEGRAM_CHAT_ID`        | No       | Telegram chat ID for notifications       |
| `THEIRSTACK_API_KEY`      | No       | TheirStack API (200 credits/month free)  |
| `SERPAPI_KEY`             | No       | SerpAPI Google Jobs (250 searches/month free) |
| `VOYAGE_API_KEY`          | No       | Voyage embeddings (Stage 1 fallback)     |
| `JOB_AGENT_SALARY_FLOOR`  | No       | Minimum salary filter (default: 150000) |
| `JOB_AGENT_STAGE1_THRESHOLD` | No    | Stage 1 cutoff score (default: 0.35)    |
| `JOB_AGENT_TOP_N_OUTPUT`  | No       | Results in output JSON (default: 100)   |
| `JOB_AGENT_HAIKU_MODEL`   | No       | Haiku model ID override                  |
| `JOB_AGENT_CLAUDE_MODEL`  | No       | Sonnet model ID override (Stage 2 fallback) |
