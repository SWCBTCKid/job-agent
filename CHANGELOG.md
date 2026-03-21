# Changelog

All notable changes to this project are documented in this file.

## [2026-03-21] - Full Profile-Driven Scoring — tier2/tier3/skill_buckets decoupled

### Problem
The March 15 work decoupled `domain_tier1` and `role_zero_patterns` from the resume, but
`domain_tier2`, `domain_tier3`, and `_auto_bucket_skills` skill groups were still hardcoded
SWE defaults in `build_scoring_config`. Any non-engineering candidate (e.g. finance/budget
analyst) got incorrect tier2 boosts (`infrastructure`, `devops`) and their skills were
auto-bucketed into irrelevant groups (languages, systems, distributed, reliability) producing
near-zero skill overlap scores.

### Added
- **`profiles/<id>/filters.json`** — new per-profile file carrying all scoring and filtering
  config in one place. Loaded by `--import-profile` and written to `search_profiles.filters_json`.
  Supported keys:
  - `role_exclude_patterns` — hard-reject titles before Stage 1
  - `role_zero_patterns`    — Stage 1 multiplier=0 (empty list = allow all)
  - `target_levels`         — level filter for Stage 1 multiplier
  - `salary_floor`          — per-profile salary floor override
  - `domain_tier1_extra`    — additional tier1 terms merged with Haiku-extracted domains
  - `domain_tier2`          — replaces hardcoded SWE tier2 fallback
  - `domain_tier3`          — replaces hardcoded SWE tier3 fallback
  - `skill_buckets`         — replaces `_auto_bucket_skills` output (null = use auto-bucketing)
- **`profiles/finance_govt/filters.json`** — finance/budget analyst config:
  - Excludes engineering role titles, allows finance/accounting titles
  - tier1_extra: federal budget, PPBE, FP&A, funding execution terms
  - tier2: financial planning, budget management, FP&A, billing, GL, etc.
  - tier3: operations, analytics, business intelligence
  - skill_buckets: erp_systems, reporting_tools, finance_domain, program_mgmt, data_tools, federal_finance
- **`profiles/gpu_autonomous/filters.json`** — SWE config made explicit (was previously the
  system default); `role_zero_patterns: []` since `role_exclude_patterns` covers all exclusions

### Changed
- **`matcher/embedder.py`** `build_scoring_config` — accepts 4 new optional params:
  `domain_tier1_extra`, `domain_tier2`, `domain_tier3`, `skill_buckets`. When provided they
  override the hardcoded SWE fallbacks. When `None`, fallbacks are unchanged (backwards compat).
- **`main.py`** `run_resume_flow` — reads all 4 new keys from `profile_filters` and passes
  them to `build_scoring_config`. Nothing hardcoded in the flow.
- **`main.py`** `import_profile` — loads `filters.json` if present and calls
  `db.set_profile_filters`. Prints `filters loaded` confirmation.

### Result
Running `finance_govt` profile now produces:
- domain tier1: Haiku-extracted domains + profile `domain_tier1_extra` (PPBE, FP&A, etc.)
- domain tier2: finance-specific fallback terms (no more `infrastructure`/`devops` boosts)
- skill groups: erp_systems / reporting_tools / finance_domain / program_mgmt / data_tools / federal_finance

---

## [2026-03-15] - Decoupled Stage 1 Scorer — per-candidate ScoringConfig

### Added
- **`matcher/embedder.py`** — `ScoringConfig` dataclass:
  - Carries `query_expansion`, `domain_tiers`, `skill_groups`, `raw_skill_regex`
  - `to_dict()` / `from_dict()` for DB serialisation
  - `ORIGINAL_CONFIG` constant — the original hardcoded Sodiq/Meta config captured as a named config
  - `build_scoring_config(record: ResumeRecord) -> ScoringConfig` — generates per-candidate config
    from Haiku-extracted keywords: tier1 domains from `keywords.domains`, skill groups auto-bucketed
    from `keywords.skills`, query expansion from `keywords.terminology`
  - `_auto_bucket_skills()` — assigns Haiku skill strings into 6 groups (languages, systems,
    distributed, reliability, security, scale) + "other" bucket
- **`resume_ingest.py`** — Haiku prompt extended with `keywords.terminology` field:
  - Extracts internal/proprietary tool names and their industry-standard equivalents
  - Stored in `keywords["terminology"]` (JSON blob alongside existing keywords)
- **`db.py`** — `scoring_configs` table:
  - `store_scoring_config(id, label, source, config_dict)` — INSERT OR IGNORE
  - `get_scoring_config(id) -> dict | None`
  - `list_scoring_configs() -> list[dict]`
  - `save_stage1_score_original(resume_id, postings)` — persists original config scores
  - Migration: adds `stage1_score_original REAL` column to all existing per-resume tables
  - Schema: new tables include `stage1_score_original` column
- **`main.py`** `run_resume_flow` — Stage 1 runs twice:
  - Haiku config scores stored in `stage1_score` (active, used for downstream ranking)
  - Original config scores stored in `stage1_score_original` (for comparison only)
  - `_print_scoring_comparison()` — terminal table showing top-15 postings with both scores
    and delta, plus above-threshold counts for each config

### Changed
- `stage1_select` — accepts `scoring_config: ScoringConfig | None` (defaults to `ORIGINAL_CONFIG`)
- `_expand_resume` — accepts `query_expansion` dict parameter
- `_skill_overlap` — accepts `skill_groups` dict + `raw_regex` flag; falls back to pre-compiled
  `_SKILL_PATTERNS` when `skill_groups` is None (original config fast path unchanged)
- `matcher/__init__.py` — exports `ScoringConfig`, `ORIGINAL_CONFIG`, `build_scoring_config`

### Why
Stage 1 scoring was hardcoded for one candidate (Sodiq/Meta): domain tiers referenced
observability/safety-critical/security enforcement; skill groups were weighted for security +
reliability; query expansion translated Meta-internal tool names. This made the scorer
useless for any other background. Now each resume drives its own config via Haiku, with the
original config stored in DB for controlled comparison.

---

## [2026-03-14] - MVP Resume Pipeline + GitHub Prep

### Added
- **`resume_ingest.py`** — Haiku-based resume ingestion:
  - sha256[:12] content hash for deduplication (no re-ingest on repeat runs)
  - Quality score (0–10), improvement suggestions, keyword extraction (titles/skills/domains)
  - ASCII quality bar printed to terminal on first ingest, cached result on repeat
- **`ranker_worker.py`** — Background Claude Sonnet final ranker:
  - Reads all postings above Stage 1 threshold from `postings_{resume_id}`
  - Calls Claude Sonnet per posting: `claude_score` (0–10), `match_reason`, `level_fit`, `risk`, `tier`
  - Flushes scores to DB in batches of 25
  - Writes `output/results_{resume_id}_{YYYYMMDD_HHMMSS}.json` sorted by `claude_score DESC`
  - Sends Telegram notification on completion or error
  - `--limit N` flag for testing (rank first N only)
  - `--rpm` flag for rate control (default 20 RPM)
- **`db.py`** — New tables and methods for resume-driven pipeline:
  - `resumes` table — stores resume record keyed by content hash
  - `search_profiles` / `profile_companies` / `profile_sources` tables — multi-profile support
  - `postings_{resume_id}` dynamic per-resume tables — scores never cross-contaminate
  - Methods: `store_resume`, `get_resume`, `create_resume_postings_table`, `save_resume_stage1_scores`, `get_above_threshold`, `write_claude_scores`, `get_top_n_by_claude`, `get_resume_stats`
- **`profiles/gpu_autonomous/`** — Profile-based company and source config:
  - `companies.json` — Greenhouse/Lever/Ashby company targets with tier boosts
  - `sources.json` — Workday, iCIMS, TheirStack, SerpAPI, Ashby, and other source seeds
- **`.gitignore`** — Excludes `.env`, `data/`, `state/`, `output/`, `__pycache__`
- **`.env.example`** — Template for all required and optional environment variables
- **`data/resume.txt.example`** — Template for new users

### Changed
- **`main.py`** — Full MVP CLI wired in:
  - `--resume <path>` triggers `run_resume_flow()`: ingest → scrape → Stage 1 → launch ranker subprocess
  - `--profile <id>` selects search profile (default: `gpu_autonomous`)
  - `--import-profile`, `--list-profiles`, `--list-companies`, `--list-resumes`, `--add-company` profile management commands
  - `build_scrapers()` now reads from DB profile tables instead of JSON files
  - `db.close()` ordering fixed — was closing before `args.resume` branch causing `sqlite3.ProgrammingError`
- **`config.py`** — Added `haiku_model`, `stage1_threshold`, `top_n_output` settings
- **Profile `gpu_autonomous`** — Company list updates:
  - Disabled: Waymo, Nuro (`active=false`)
  - Added: Cerebras Systems (Greenhouse, tier_boost 1.2)
  - Added: Nvidia (Workday — `nvidia.wd5.myworkdayjobs.com`, tier_boost 1.2)
  - Added: Microsoft (Workday — `microsoft.wd1.myworkdayjobs.com`, tier_boost 1.0)

### Verified
- `py -3 main.py --resume data/resume.txt --dry-run`:
  - Resume cached (id=`4ca85c675bad`, quality 8.2/10)
  - Scraped: 9,696 → filtered: 1,985 → Stage 1 above threshold: 813
- `py -3 ranker_worker.py --resume-id 4ca85c675bad --profile-id gpu_autonomous --limit 3`:
  - 3/3 scored, 0 failed, output JSON written, Telegram delivered
  - Top match: Nuro — Software Update Infrastructure (8.2/10, OTA pipeline alignment)

### Status
- **Ready for GitHub** — repo initialized, `.gitignore` confirmed clean (no secrets, no personal data)
- **Pending**: push to remote once GitHub CLI PATH issue resolved after session restart

## [2026-03-11] - AI Chip / Accelerator Company Expansion

### Added
- **4 new AI chip companies** verified against live ATS APIs:
  - Greenhouse: Tenstorrent (`tenstorrent`, tier_boost 1.2), SambaNova Systems (`sambanovasystems`, tier_boost 1.1)
  - Ashby: d-Matrix (`d-matrix`, tier_boost 1.1), Etched (`etched`, tier_boost 1.1)

- **6 AI chip companies added to TheirStack seed** (JS-rendered career pages, no accessible ATS):
  - Groq (Mountain View — LPU inference chips)
  - Rivos (Mountain View — RISC-V AI chips)
  - Enfabrica (Sunnyvale — AI networking silicon)
  - Ampere Computing (Santa Clara — cloud-native CPUs)
  - Esperanto Technologies (Mountain View — RISC-V ML chips)
  - Recogni (San Jose — inference chips for autonomy)
  - All 6 added to `theirstack` seed under new `"AI chip companies without ATS coverage"` batch
  - All 6 + confirmed chip companies added to the broad TheirStack domain sweep

### Changed
- `data/target_companies.json`: 4 new entries (Tenstorrent, SambaNova, d-Matrix, Etched)
- `data/source_seeds.json`:
  - New `theirstack` batch: `"AI chip companies without ATS coverage"` targeting 6 domains
  - Broad sweep domain list expanded from 15 → 23 domains to include all chip companies

## [2026-03-11] - Company Reach Expansion + iCIMS / TheirStack / SerpAPI Scrapers

### Added
- **8 new target companies** verified against live ATS APIs (`data/target_companies.json`):
  - Greenhouse: Kodiak Robotics (`kodiak`), Figure AI (`figure`), Zipline (`flyzipline`),
    Nimble Robotics (`nimblerobotics`), Intrinsic / Alphabet (`intrinsicrobotics`)
  - Ashby: Physical Intelligence (`physicalintelligence`), Serve Robotics (`serverobotics`)
  - Workday: Wisk Aero (`Wisk_Careers`) — added to `data/source_seeds.json`

- **`scrapers/icims.py`** — new iCIMS ATS scraper:
  - Fetches `https://careers-{company}.icims.com/jobs/search?ss=1&searchCategory=0&in_iframe=1`
    (iframe variant returns parseable HTML without JS rendering)
  - Extracts absolute job URLs via regex; decodes URL-encoded titles from slugs
  - ID scheme: `icims::{job_id}` (numeric iCIMS job ID)
  - Seeded with Joby Aviation (`careers-jobyaviation.icims.com`, tier_boost 1.1)

- **`scrapers/theirstack.py`** — TheirStack API scraper:
  - POST `https://api.theirstack.com/v1/jobs/search` with Bearer auth
  - Supports full filter set: `company_domain_or`, `company_name_case_insensitive_or`,
    `job_title_pattern_or`, `job_location_pattern_or`, `min_salary_usd`, `max_age_days`
  - Returns rich job objects: title, URL, location, description, salary min/max, remote flag
  - ID scheme: `theirstack::{id}`
  - Gracefully no-ops when `THEIRSTACK_API_KEY` is not set
  - Two seed batches configured:
    1. Covariant, Gatik, Pony.ai, Cruise — companies with no accessible ATS (max 14 days)
    2. Broad domain sweep across all 15+ target company domains (max 7 days, limit 50)

- **`scrapers/serp.py`** — SerpAPI Google Jobs scraper:
  - GET `https://serpapi.com/search?engine=google_jobs` with `api_key` param
  - Filters: `q`, `location`, `date_posted` (hour/day/week/month), `gl`, `hl`
  - Parses salary from `detected_extensions.salary` string (e.g. `$150K–$200K a year`)
  - Gracefully no-ops when `SERPAPI_KEY` is not set
  - Three seed queries: AV/robotics Bay Area, platform/robotics SF, company-specific
    Covariant/Gatik/Pony.ai/Cruise targeted query

- **`config.py`**: added `theirstack_api_key` and `serpapi_key` fields to `Settings`
- **`.env`**: placeholder entries for `THEIRSTACK_API_KEY` and `SERPAPI_KEY`

### Changed
- `scrapers/__init__.py`: exports `ICIMSScraper`, `TheirStackScraper`, `SerpScraper`
- `main.py` `build_scrapers()`: wires in all three new scrapers; `TheirStackScraper` and
  `SerpScraper` receive their API keys from `SETTINGS`
- `data/source_seeds.json`: added `icims`, `theirstack`, and `serp` sections

### Notes
- TheirStack free tier: 200 API credits/month (1 per job returned) — daily runs at limit
  25+50 per cycle leave comfortable headroom
- SerpAPI free tier: 250 searches/month — 3 queries/run allows ~80 daily runs
- Companies still unresolved (JS-rendered, no accessible ATS detected): Covariant, Gatik,
  Pony.ai — TheirStack and SerpAPI seeds are the primary coverage path for these

## [2026-03-09] - BuiltinSF Scraper + Stronger Job IDs + Search Term Tuning

### Added
- `scrapers/builtinsf.py` — new cross-company discovery scraper for Builtin SF.
  - Searches 19 terms across 3 pages each (up to 75 listings per term).
  - Listing pages: matches absolute `https://builtin.com/job/...` URLs directly in HTML.
  - Detail pages: parses plain `<script>` blocks containing `@context: schema.org` +
    `@graph: [JobPosting]` (no `type="application/ld+json"` attribute — Builtin omits it).
  - Extracts company, title, description, salary (min/max USD), datePosted, location.
  - Location picker prefers Bay Area city when job has multiple locations.
  - Dedupes by URL hash within a single run; DB `INSERT OR IGNORE` handles cross-run dedup.
  - Wired into `main.py` `build_scrapers()` and `scrapers/__init__.py`.
- `utils.make_url_id(url)` — stable `sha256(url)[:24]` ID for scrapers that lack an ATS ID.

### Changed
- **Job ID scheme hardened** — scraper-level title collision risk eliminated:
  - Greenhouse: `gh::{job_id}` (numeric ATS ID from Greenhouse API)
  - Lever: `lv::{uuid}` (UUID from Lever API)
  - Ashby: `ashby::{id}` (namespaced ATS ID)
  - BuiltinSF: `make_url_id(url)` — URL-unique per posting
  - HN: `hn::{objectID}` — comment ID from Algolia
  - Workday: `make_url_id(url)` — URL path contains job ID
  - Fallback: `make_job_id(company, title)` retained only where no better ID exists.
- **BuiltinSF search terms** updated from 7 weak terms to 19 role-aligned terms:
  - Removed: `backend engineer`, `devops engineer`, `software engineer` (too broad/weak fit)
  - Added: `production engineer`, `security engineer`, `observability engineer`,
    `systems engineer`, `reliability engineer`, `embedded engineer`, `control plane`,
    `safety critical`, `mission critical`, `fleet management`, `RTOS`,
    `distributed systems reliability`, `low latency`, `autonomous systems`,
    `firmware engineer`
- `_PAGES_PER_TERM` bumped from 2 → 3 (75 listings per term before URL dedup).

### Notes
- `\bstaff\b` and `\bjunior\b` already present in `NON_ENGINEERING_TITLE_PATTERNS` —
  staff and junior roles are filtered before DB insertion.

## [2026-03-09] - Score Persistence + Clearance Filter + Junior Filter

### Added
- `requires_clearance(posting)` hard filter in `main.py` — rejects postings requiring
  TS/SCI, Top Secret, active security clearance, DoD clearance, polygraph, etc. Applied
  alongside citizenship filter, removed ~464 postings from the pipeline.
- `db.save_stage1_scores(postings)` — persists `stage1_score` and `embed_score` for
  ALL postings evaluated by `stage1_select`, not just the top N sent to Claude. Called
  immediately after `stage1_select` so every candidate in the DB has a stage1 score.
- `db.save_final_score(posting)` — persists `final_score` after `rank_postings` for
  ALL scored candidates (not just ranked top 10). Called on `all_candidates` so the
  full scored pool is queryable by score at any time.
- `db.reset_unalerted_scores()` — resets `stage2_scored=0` for all unalerted postings
  at the start of each run, forcing re-scoring of the full pool each cycle.

### Changed
- `mark_alerted` simplified to only set `alerted=1` (final_score now saved separately
  by `save_final_score`).
- `NON_ENGINEERING_TITLE_PATTERNS` expanded: added `\bstaff\b`, `\bjunior\b`,
  `\bentry.?level\b`, `engineer\s+i\b`, `\bintern\b`.
- `save_final_score` now called on `all_candidates` (all scored unalerted postings)
  instead of only the digest top N.

### Known Issues
- Stage 1 role filter has gaps: "Area Sales Director", "Senior Director Marketing",
  ML Engineer titles, and similar variants pass `is_engineering_role` due to narrow
  regex patterns. Needs broader director/ML filter pass.
- Stage 1 embedding scores ML-heavy infra roles similarly to platform/SRE roles due
  to shared vocabulary. Needs further signal tuning.

## [2026-03-08] - Persistent Candidate Pool

### Problem
`dedupe_new` marked every scraped posting as seen immediately and forever. Only the
top 30 from Stage 1 got Claude-scored; anything outside that window was marked seen
but never scored or shown. Good postings that lost Stage 1 to a flood of weaker ones
from the same company were permanently invisible.

### Fix — Persistent Candidate Pool with Per-Run Re-Ranking (`db.py`, `main.py`)

**DB schema migration** (`_migrate_schema`): 15 new columns added to `postings` via
try/except `ALTER TABLE` (safe on existing DBs): `source_priority`, `location`,
`remote`, `salary_min`, `salary_max`, `salary_inferred`, `tier_boost`, `description`,
`embed_score`, `stage1_score`, `match_score`, `level_fit`, `competition`,
`embedded_flag`, `stage2_scored`.

**New DB methods**:
- `store_candidate(posting)` — `INSERT OR IGNORE` with full posting incl. description
  truncated to 8000 chars. Sets `stage2_scored=0, alerted=0`. Never overwrites.
- `get_unscored()` — returns all `stage2_scored=0` rows with non-empty description.
- `mark_scored(posting)` — writes match scores back, sets `stage2_scored=1`.
- `get_unalerted_scored()` — returns all `alerted=0 AND stage2_scored=1`.
- `mark_alerted(posting)` — sets `alerted=1, final_score=?`.
- `_row_to_posting(row)` — converts `sqlite3.Row` → `JobPosting` with NULL defaults
  and timezone-aware `posted_at` parsing.

**New main.py flow**:
```
filtered_role
  → classify_competition(p) + db.store_candidate(p) for each
  → unscored = db.get_unscored()
  → stage1_select(unscored) → stage2_match → db.mark_scored(p)
  → all_candidates = db.get_unalerted_scored()
  → rank_postings(all_candidates)
  → db.mark_alerted(p) [skipped on --dry-run]
```

Dry-run does not call `mark_alerted`, so the same scored pool re-appears each dry-run.

**New summary stats**: `Unscored in DB (sent to stage1): N` and
`All unalerted scored (ranked from): N`.

### Verification
- `python -m compileall db.py main.py` — no errors.

## [2026-03-09] - Multi-Signal Stage 1 Scorer

### Problem
Single cosine similarity from `all-MiniLM-L6-v2` cannot discriminate role types when
domain vocabulary overlaps. A Waymo PM role shares "safety/systems/reliability" vocabulary
with the resume, so it embeds close to the resume vector despite being the wrong role type.
Root causes: (1) no query expansion for internal Meta/Hitachi vocabulary, (2) single signal
can't separate domain match from role fit.

### Fix — Multi-Signal Stage 1 Formula (`matcher/embedder.py`)

Replaced single cosine score with:
```
stage1_score = max(0, base × role_multiplier − anti_pattern_penalty)
base = embedding_sim×0.45 + skill_overlap×0.25 + domain_score×0.20 + freshness×0.10
```

**Query expansion**: Before embedding, appends industry-standard equivalents for internal
terms (Tupperware→kubernetes, Conveyor→ci/cd, Scuba→observability platform, etc.) under a
`SKILL TRANSLATIONS:` header. Bridges vocabulary gap so meta/hitachi JD terms embed closer
to their actual equivalents.

**skill_overlap**: 6 skill groups (languages, systems, distributed, reliability, security,
scale) — scores fraction of groups present in JD.

**domain_score**: Tier 1 (1.0) = observability/security enforcement/control plane/fleet
management/platform infrastructure. Tier 2 (0.7) = general infrastructure/devops/platform.
Tier 3 (0.4) = ML infra. None (0.1).

**freshness**: 0-3d=1.0, 4-7d=0.7, 8-14d=0.5, >14d=0.2.

**role_multiplier**: IC=1.0, TechLead=0.9, EM=0.6, TPM=0.3, PM=0.1, HR/Sales/Recruiter=0.0.
Multiplicative — PM role scoring 0.8 embedding becomes 0.08 overall.

**anti_pattern_penalty**: PhD=-0.10, pure ML (≥3 ML signals, <2 infra)=-0.15,
firmware/FPGA=-0.10, management language=-0.10. Capped at -0.25.

Worked example: Waymo PM, Safety → 0.000 (floored). Waymo SWE Onboard Infra → 0.884.

### Also Fixed — `matcher/__init__.py`
Removed export of `compute_penalty` (deleted function, was only referenced in docs).

## [2026-03-09] - Match Quality Pass

### Problem
Top 10 digest was dominated by Waymo PM/data science roles with no real fit. Root causes:
1. No role type filter — PM, Data Scientist, Research Scientist roles passed all filters
   and flooded Stage 1 by sharing domain vocabulary ("safety", "systems", "reliability").
2. Target companies had 6/15 entries returning 404. Waymo alone flooded Stage 1 top 30.
3. Resume lacked explicit role-family keywords, weakening Stage 1 embedding similarity.

### Fix 1 — Role type gate (`main.py`)
Added `is_engineering_role(posting)` filter applied after salary gate. Rejects PM,
Product Manager, Solutions Architect, Data Scientist, Research Scientist, Recruiter,
HR, Finance, Legal, Marketing, Sales, Customer Success by title regex.
Added `after_role_filter` to run summary output.

### Fix 2 — Target companies expanded + slugs corrected (`data/target_companies.json`)
Fixed broken slugs: shieldai, joby, chronosphereio, grafanalabs, honeycombio (all greenhouse).
Added: Archer Aviation, Aurora Innovation, Zoox (safety-critical), CrowdStrike, Wiz,
Snyk, Lacework (security infra), HashiCorp, Cockroach Labs, Temporal Technologies (platform).
15 → 25 companies. tier_boost updated to reflect Tier 1 priority.

### Fix 3 — Resume keyword enrichment (`data/resume.txt`)
Added TARGET ROLES section with explicit role titles and domain keywords (SRE, production
engineering, platform infrastructure, fleet management, reliability) to improve Stage 1
cosine similarity for jobs that use these terms.

## [2026-03-09] - Reliability + Review Closure

### Added
- Full candidate resume content persisted to `data/resume.txt`.
- Runtime warning logs across scraper fallbacks and embedding-provider fallback paths.
- Safe Telegram send helper (`send_telegram_safe`) for non-fatal alert paths.
- Telegram message chunking support (<= 4000 chars per message chunk).
- Timestamped per-run file logging across Python modules via `job_logging.py` (`state/logs/job-agent-YYYYMMDD-HHMMSS.log`).

### Changed
- Default Claude model switched to `claude-sonnet-4-6`:
  - `config.py`
  - `.env.example`
  - local `.env` seeded with model value
- Stage 2 Claude `max_tokens` increased from `3000` to `6000`.
- Stage 2 Claude network timeout increased from `60s` to `120s` for both `httpx` and `urllib` paths.
- `main.py` now routes scraper and Stage 2 failures to explicit alerts/logging hooks.
- `run_agent` wrapped with fatal error guard + alert path.
- Salary inference call in `main.py` now derives level from posting text instead of hardcoding `senior`.
- HN company extraction improved from first-token parsing to delimiter-aware extraction.
- `_infer_tier` improved to avoid accidental `ml` substring matches (uses word-boundary for `ml`).
- Added hard filters before ranking:
  - reject postings requiring U.S. citizenship / U.S. person status
  - keep Bay Area postings only
- Embedded-role penalty keywords narrowed to `firmware` and `fpga` (removed RTOS/microcontroller penalties).
- Fallback scoring now downranks pure-ML postings unless infra/security signals are also present.

### Fixed
- Eliminated key silent-failure paths identified in reviews:
  - scraper exception swallow with no visibility
  - Stage 2 fallback without logging/alert signal
  - digest drop risk from Telegram 4096-char limit
  - Stage 1 crash path when sentence-transformers model download fails

### Verification
- `python -m compileall .` succeeded after reliability changes.
- `python main.py --dry-run --json` succeeded after fallback crash fix.
- `python main.py` completed without crashing in restricted-network environment and emitted explicit source failure logs (expected under sandbox/network policy).
- Observed Stage 2 Claude fallback trigger under `httpx.ReadTimeout`; diagnostics now persisted in per-run logs.

### Diagnostics Notes
- Claude connectivity issue currently manifests as request timeout rather than auth/model validation failure:
  - `httpx.ReadTimeout` in `matcher/claude_matcher.py` call to `https://api.anthropic.com/v1/messages`.
- Stage 2 token budget per Claude call (current code):
  - Input prompt includes `resume_text[:6000]` plus up to `JOB_AGENT_TOP_N_STAGE2` jobs (default `30`), each with `description[:5000]`.
  - Practical upper bound is roughly `~40k-50k input tokens` (character-to-token approximation, includes JSON/prompt overhead).
  - Output cap is `max_tokens=6000`.
  - Total worst-case per call is therefore roughly `~46k-56k tokens`.

## [2026-03-08] - Build + Hardening Pass

### Added
- Initial runnable job-agent pipeline with orchestrator, DB, matcher, ranking, notifier, and data files.
- Scraper implementations for Greenhouse, Lever, HN, Workday (best-effort), Wellfound (best-effort), YC (best-effort), Pragmatic (RSS), and LinkedIn (best-effort).
- Source seed configuration file for non-API scrapers: `data/source_seeds.json`.
- Stage 1 semantic matching path with provider fallback strategy.
- Async Stage 2 Claude matching path with strict output validation/coercion.
- Salary inference fallback tiers including industry-average baselines.

### Changed
- Stage 1 matcher upgraded from BoW-only scoring to semantic-first matching:
  - `sentence-transformers` local embeddings (preferred when installed)
  - Voyage embeddings API fallback
  - TF-IDF fallback when semantic embeddings are unavailable
- Stage 2 matcher upgraded from blocking `urllib` flow to async `httpx` flow (with safe urllib fallback).
- `main.py` now awaits async Stage 2 matching and loads source seeds.
- Config expanded with runtime model/provider selection knobs.

### Fixed
- Review findings from `CODE_REVIEW_2026-03-07.md`:
  - Removed empty scraper stubs.
  - Reduced event-loop blocking risk in Stage 2.
  - Improved malformed Claude response handling.
  - Improved salary lookup coverage for unknown companies.

### Verification
- `python -m compileall .` succeeded.
- `python main.py --dry-run --json` succeeded.

## [2026-03-07] - Scaffold

### Added
- Initial architecture documents and requirements:
  - `ARCHITECTURE.md`
  - `AGENT_REQUIREMENTS.md`
- Baseline code scaffold and first dry-run flow.
