# Job Search Agent — Requirements

> For AI assessment and validation. Paste into any AI to reason about the design.

---

## What This Is

An autonomous job search agent that ingests a resume, scrapes job postings from multiple
sources, semantically matches them against the resume, and delivers a ranked output file
of the top matches — avoiding the high-competition LinkedIn pipeline where 300+ people
have already applied.

The agent is designed to be multi-resume and multi-profile. A GPU/autonomous-vehicle
engineer has a different target company list than a backend engineer or a frontend engineer.
Each resume instance and each search profile is fully isolated in the database.

---

## MVP CLI Flow

```bash
python main.py --resume /path/to/resume.txt [--profile gpu_autonomous] [--dry-run]
```

**Step 1 — Resume Ingest (sync, Claude Haiku)**
- Hash resume content (sha256[:12]) → check `resumes` table
- Duplicate: reuse record, skip Haiku call
- New: Haiku produces quality score (0–10), improvement suggestions, and keyword tags
- Print score and improvements to terminal
- Resume stored in DB as `resume_id`

**Step 2 — Scrape + Filter + Stage 1 (sync)**
- Scrape all sources defined in the active search profile
  (`profile_companies` + `profile_sources` tables for this `profile_id`)
- Hard filters (binary gates — applied before embedding):
  - Bay Area location (or remote)
  - Salary floor $150K+
  - Role type gate (reject PM, recruiter, data scientist, etc.)
  - Clearance rejection (TS/SCI, polygraph, DoD required)
- Stage 1 embedding: score every passing posting against THIS resume
- Store all candidates with `stage1_score` in `postings_{resume_id}`
- Select candidates where `stage1_score >= STAGE1_THRESHOLD` (default 0.35)

**Step 3 — Claude Ranking (background subprocess, fire & forget)**
- Every posting above Stage 1 threshold gets a Claude Sonnet score
- `claude_score` (0–10) + `match_reason` + `risk` + `level_fit` written back to DB
- Sort by `claude_score` DESC
- Write top 100 (or all if < 100) to `output/results_{resume_id}_{ts}.json`
- Telegram when complete: "N ranked, top match: {title} @ {company} ({score})"
- On error: log, write partial output, Telegram error message

---

## Database Design

### Isolation Model

| Table | Scope | Purpose |
|-------|-------|---------|
| `resumes` | Global | One row per unique resume (by content hash) |
| `search_profiles` | Global | Named scraping configurations |
| `profile_companies` | Per profile | Target company list for this focus area |
| `profile_sources` | Per profile | Source seeds (TheirStack queries, SerpAPI queries, etc.) |
| `postings_{resume_id}` | Per resume | Candidates + scores specific to this resume |
| `seen_hashes` | Global | Cross-run dedup |

**Key principle:** Scores in `postings_{resume_id}` are specific to that resume. The same
job posting can rank #1 for one resume and #50 for another. Separating tables rather than
using a shared table with a `resume_id` column avoids cross-contamination and simplifies
all queries.

### Search Profile — Multi-User Design

Right now the agent is configured for a GPU/autonomous-vehicle/AI-chip engineer. If a
backend engineer or frontend engineer uses the agent, they need a completely different:
- Target company list (Stripe, Notion, Linear vs Anduril, Waymo, Tenstorrent)
- TheirStack domain filters (fintech, developer-tooling vs AI chip, AV)
- SerpAPI query terms ("backend engineer fintech" vs "platform engineer robotics")
- Tier boosts (a backend engineer doesn't care about tier_boost on Figure AI)

This is solved by `search_profiles` + `profile_companies` + `profile_sources` tables.
The JSON files (`target_companies.json`, `source_seeds.json`) are seeded into the DB
on first run under the default profile and are no longer the source of truth at runtime.

---

## Candidate Background (Default Profile)

### Contact
Sodiq Lawal | San Francisco, CA | (650) 405-2039 | sodiqlawal6@gmail.com

### Experience Summary

**Meta Platforms — Software Engineer** | Menlo Park, CA | Oct 2024 – Oct 2025
- Security enforcement observability platform monitoring 30M+ services, 10M+ hosts
- Dual-phased rollout of security validation layer across 13 production host types
- Containerized security validation service across 200K+ core data services, 19 regions
- Extended observability from containerized to bare-metal (320M service instances)

**Hitachi Rail — Senior Software Engineer** | Toronto, ON | Feb 2022 – Sept 2024
- Onboard controller for Doha driverless metro fleet (safety-critical)
- Redundant CPU failover for display subsystem
- OTA update pipeline replacing 30 manual installs/hour with automated fleet delivery
- Dual-failure scenario correctness fix (coupler + comms link loss)

**Hitachi Rail — Software Engineer** | Toronto, ON | May 2015 – Feb 2022
- Unstable wheel speed sensor mitigation (500m → 400km MTBE)
- Configurable speed-sensor subsystem in C (2/3/4-sensor architectures)
- CBTC boundary-condition bottleneck fix on Edmonton LRT Capital Line
- Linux-based RTOS migration

**Education:** Queen's University — BASc Electrical Engineering, 2014

**Skills:** C/C++, Rust, Python | Embedded Linux, Linux RTOS | Distributed systems,
Thrift/RPC, Kubernetes (Tupperware), CI/CD (Conveyor), Observability (Scuba),
Alerting (OneDetection), Chef, UDP/networking, VectorCast, gUnit, HIL testing

---

### The Typecast Problem

Companies classify this candidate as an "embedded engineer." Root causes:
1. Resume summary reads "safety-critical embedded software" — misleading framing
2. One RTOS migration bullet in a 9-year tenure
3. Hitachi Rail brand signals "hardware-adjacent" to automated screeners

**The reality:** Work was at the application / control logic layer:
- CBTC = rules engine governing train movement authority and interlocks — not firmware
- OTA pipelines, fleet-wide config management — not device drivers
- Meta: hyperscale distributed observability and security enforcement

The agent must match on what the candidate **actually did**, not on company name or
industry vertical.

### Actual Strengths to Match Against
1. **Hyperscale distributed systems** — 30M services, 10M hosts, 19 regions, 320M instances
2. **Safety-critical correctness** — zero-defect tolerance, redundancy, failover, dual-failure
3. **Fleet-scale automation** — OTA delivery, automated rollouts, ACL enforcement coverage
4. **Security observability** — authorization regression detection, enforcement monitoring
5. **End-to-end ownership** — design through live deployment
6. **Strong language profile** — C/C++, Rust, Python across safety and hyperscale contexts

### Target Role Categories (priority order)
1. **Distributed Systems Engineer** — control plane, orchestration, consensus, fault tolerance
2. **Platform / Infrastructure Engineer** — internal platforms, fleet management
3. **Security Engineering** — authorization systems, enforcement infrastructure
4. **Observability / Production Engineering** — metrics/alerting platforms, fleet diagnostics
5. **Safety-Critical Software Engineer** — aerospace, automotive ADAS, defense, robotics
6. **SRE** — Senior level at companies with serious reliability requirements

---

## Stage 1 Multi-Signal Scoring Formula

```
stage1_score = max(0.0, base_score × role_multiplier − anti_pattern_penalty)

base_score = embedding_sim × 0.45
           + skill_overlap × 0.25
           + domain_score  × 0.20
           + freshness     × 0.10
```

**Purpose:** Reduce the full scraped pool (800–1000 postings) to a manageable set for
Claude. Stage 1 is a precision gate, not the final ranking. Everything above
`STAGE1_THRESHOLD` (0.35) goes to Claude regardless of volume.

**Component 1 — embedding_sim (0.45 weight)**
Cosine similarity between expanded resume vector and JD vector.
Query expansion maps internal Meta/Hitachi terms to industry-standard equivalents:
Tupperware → kubernetes, Conveyor → ci/cd, Scuba → observability platform, etc.

**Component 2 — skill_overlap (0.25 weight)**
6 skill groups: languages, systems, distributed, reliability, security, scale.
`skill_overlap = matched_groups / 6`

**Component 3 — domain_score (0.20 weight)**

| Score | Keywords |
|-------|----------|
| 1.0 | observability platform, security enforcement, authorization system, safety-critical, fleet management, distributed systems, control plane, production engineering, platform infrastructure |
| 0.7 | infrastructure, developer tooling, backend systems, cloud infrastructure, devops, platform engineering |
| 0.4 | machine learning infrastructure, mlops, ml platform |
| 0.1 | no match |

**Component 4 — freshness (0.10 weight)**
1.0 (0–3d) → 0.7 (4–7d) → 0.5 (8–14d) → 0.2 (>14d)

**Role multiplier**
1.0 (IC engineer) / 0.9 (tech lead) / 0.6 (EM) / 0.3 (TPM) / 0.1 (PM) / 0.0 (recruiter/sales)

**Anti-pattern penalty (capped at −0.25)**
−0.10 PhD required | −0.15 pure ML (≥3 ML keywords, <2 infra) | −0.10 firmware/FPGA | −0.10 management language

---

## Stage 2 — Claude Ranking

Claude Sonnet 4.6 scores every posting that passes the Stage 1 threshold.
This is the authoritative ranking — `claude_score` is the final sort key in the output.

**Scoring guide:**
- 8–10: Direct match — prioritise immediately
- 6–7: Strong match — worth applying
- 4–5: Partial match — apply if volume is low
- 0–3: Poor match — skip

**Output per posting:** `claude_score`, `tier`, `match_reason`, `risk`, `level_fit`

---

## Output File

`output/results_{resume_id}_{YYYYMMDD_HHMMSS}.json`

Sorted by `claude_score` DESC. Top 100 (or all ranked if < 100).
Each result includes: rank, company, title, url, claude_score, stage1_score, tier,
level_fit, match_reason, risk, location, salary_min, salary_max, posted_at, source.

---

## Search Requirements (Default Profile)

### Salary
- Minimum **$150,000 USD** base
- Include if no salary listed but company is known to pay $150K+ at senior level

### Location
- Preferred: San Francisco Bay Area (on-site, hybrid, or remote)
- Acceptable: Fully remote

### Company Stage
- Series A and above
- Big tech not excluded
- Avoid: pre-seed, seed-only, no-revenue startups

### Industries
- All industries considered — rail not excluded
- Priority targets: defense tech, aerospace, autonomous vehicles, robotics, fintech infra,
  AI chip/accelerator, observability/security infra

---

## Hard Filters (Applied Before Stage 1)

| Filter | Logic |
|--------|-------|
| Bay Area | location field or description contains Bay Area city tokens |
| Salary floor | salary_min >= 150000, or salary unknown but company known-high-pay |
| Role type gate | Regex rejects PM, recruiter, data scientist, research scientist, sales, HR, legal, finance |
| Clearance rejection | Rejects TS/SCI, Top Secret, active clearance required, polygraph, DoD clearance |
| Junior filter | Rejects intern, entry-level, junior, engineer I |

---

## Data Sources (Default Profile)

| Source | Rationale |
|--------|-----------|
| Greenhouse API | Direct ATS — 1–2 weeks before aggregators |
| Lever API | Direct ATS |
| Ashby API | Direct ATS (growing startup adoption) |
| iCIMS | Direct ATS — Joby Aviation |
| Workday (HTML) | Direct ATS — SpaceX, CrowdStrike, Wisk Aero |
| TheirStack API | Covers JS-rendered career pages (Groq, Rivos, Covariant, Gatik) |
| SerpAPI Google Jobs | Broad sweep for companies without accessible ATS |
| Builtin SF | Cross-company Bay Area discovery |
| HN Who's Hiring | Very low competition, direct company posts, monthly |
| YC job board | Vetted Series A+ startups |
| Pragmatic Engineer | RSS feed, curated senior engineering roles |
| Wellfound | Best-effort (partially gated) |
| LinkedIn | Last resort — flag crosspostings |

---

## Assessment Questions for Other AIs

1. **Stage 1 threshold:** Is 0.35 the right starting threshold given the multi-signal
   formula, or would a different value reduce false negatives more effectively?

2. **Background process design:** subprocess.Popen vs asyncio background task —
   any robustness concerns with the subprocess approach on Windows?

3. **Profile seeding:** Best way to let a new user specify their own company list
   at the CLI without requiring direct DB edits?

4. **Stage 1 for non-SWE profiles:** The domain_score and skill_overlap components
   are tuned for Sodiq's background. How should these adapt for a backend or frontend
   engineer without requiring code changes?

5. **TheirStack + SerpAPI cost at threshold scale:** If threshold=0.35 passes 200
   postings to Claude instead of 30, what's the realistic token cost per run?
