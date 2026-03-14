# Job Search Agent — Architecture & Code Review

> This file is for AI reviewers to critique the architecture defined in ARCHITECTURE.md
> and any code produced during implementation.
> Append your review below following the format at the bottom of this file.
> Identify yourself, include UTC timestamp, be direct.

---

## Claude Sonnet 4.6 — Architecture Review — 2026-03-08 06:30 UTC

### Overall Assessment
The architecture is sound and implementation-ready. No blocking design flaws.
Three areas need tightening before the first line of code is written.

---

### Issue 1 — Scraper Failure Isolation (High Priority)

**Problem:** The orchestrator in `main.py` will call all scrapers in parallel. If one scraper
raises an unhandled exception (Workday blocks the request, HN thread not found this month,
Wellfound changes their HTML structure), the entire daily run fails silently or crashes.

**Fix:** Each scraper must be wrapped in an isolated try/except at the orchestrator level.
A failed scraper logs a warning and returns an empty list — it never kills the run.

```python
async def safe_fetch(scraper) -> list[JobPosting]:
    try:
        return await scraper.fetch()
    except Exception as e:
        log.warning(f"[{scraper.__class__.__name__}] fetch failed: {e}")
        await telegram_alert(f"[JobAgent] Scraper {scraper.__class__.__name__} failed: {e}")
        return []

results = await asyncio.gather(*[safe_fetch(s) for s in scrapers])
```

This mirrors the pattern already proven in the trading agent (`live_trader.py`) where silent
scraper failures were a major source of data gaps.

---

### Issue 2 — Resume Vector Caching (Medium Priority)

**Problem:** The architecture says "embed resume once at startup, cache the vector." If the
process is stateless (runs via Task Scheduler and exits), "startup" means every daily run
re-embeds the resume. At voyage-code-2 pricing this is negligible cost, but it adds ~500ms
latency and an unnecessary API call on every run.

**Fix:** Persist the resume embedding to disk as a `.npy` file alongside a hash of the resume
text. On startup, check if the hash matches — if yes, load from disk; if no, re-embed and
save. Resume changes are rare, so the cache will almost always hit.

```python
RESUME_CACHE = Path("state/resume_vector.npy")
RESUME_HASH_FILE = Path("state/resume_hash.txt")

def load_resume_vector(resume_text: str) -> np.ndarray:
    current_hash = hashlib.md5(resume_text.encode()).hexdigest()
    if RESUME_CACHE.exists() and RESUME_HASH_FILE.read_text() == current_hash:
        return np.load(RESUME_CACHE)
    vector = embed(resume_text)
    np.save(RESUME_CACHE, vector)
    RESUME_HASH_FILE.write_text(current_hash)
    return vector
```

---

### Issue 3 — Claude Stage 2 Batch Size Risk (Medium Priority)

**Problem:** Sending 30 full job descriptions + the resume in a single Claude call is ~31K
input tokens. This is fine for cost but risks hitting the output token limit if all 30 JDs
return verbose reasoning. It also means if Claude's JSON output is malformed (missing bracket,
truncated), the entire batch fails and we get zero results for that run.

**Fix:** Batch in groups of 10, not 30. Three calls of 10 JDs each:
- Smaller output per call = lower truncation risk
- If one batch fails, the other two still succeed
- Easier to debug malformed JSON (isolated to one batch)
- Total cost difference: negligible

```python
BATCH_SIZE = 10

async def stage2_match(postings: list[JobPosting]) -> list[JobPosting]:
    results = []
    for batch in chunked(postings, BATCH_SIZE):
        batch_results = await claude_score_batch(batch)
        results.extend(batch_results)
    return results
```

---

### Issue 4 — Salary Inference Table Maintenance (Low Priority)

**Problem:** `salary_data.json` is a static lookup table. Salary data goes stale within
6–12 months. If a company has a funding round or layoffs, the inferred salary could be
significantly wrong. The architecture says "flag inferred salary clearly" which is correct,
but the table needs a `last_updated` field so the agent can warn when data is >6 months old.

**Fix:** Add metadata to the salary lookup:

```json
{
  "last_updated": "2026-03-08",
  "entries": [
    {"company": "Datadog", "level": "senior", "role_family": "infra",
     "p50_base": 195000, "p75_base": 215000, "source": "levels.fyi"}
  ]
}
```

Agent warns via Telegram if `last_updated` is >180 days old.

---

### Structural Observations

**What's solid and should not change:**
- Two-stage pipeline architecture — correct call, well reasoned by all three AIs
- Soft penalty multipliers over hard blacklist — avoids false negatives on mixed JDs
- SQLite for state — right tool, no over-engineering
- Dedup on (company + normalized_title) — correct hash key
- Source priority ordering — direct ATS > aggregator > LinkedIn is the right call
- Freshness decay curve — sensible, not too aggressive

**One naming concern:**
`ranker.py` contains the composite scoring formula, but `embedder.py` also produces scores
(stage1_score). The boundary between these two files will blur during implementation.
Recommend: `embedder.py` outputs raw cosine similarity only. All scoring, weighting, and
formula application lives exclusively in `ranker.py`. Keep the data transformation pipeline
strictly linear: scrape → embed → score → rank → notify.

**Test coverage recommendation:**
Before shipping, at minimum test:
1. `compute_penalty()` with edge cases (zero matches, one match, 3+ matches)
2. `final_score()` with a known set of postings to verify ranking order is intuitive
3. SQLite dedup — confirm a re-scraped posting with the same hash is not re-alerted
4. Claude JSON parse failure — confirm a malformed response doesn't crash the run

---

## Instructions for Gemini

Read `ARCHITECTURE.md` fully before reviewing. Then append your review below using this format:

```
---

## [Model Name] — [Review Type] — [YYYY-MM-DD HH:MM UTC]

### Overall Assessment
[Pass / Pass with concerns / Needs revision]

### Critical Issues
[Anything that would cause incorrect behavior or data loss]

### Design Concerns
[Architectural choices you'd do differently]

### Validation Questions
[Things you'd want verified before implementation proceeds]

### Approved Components
[What looks solid and should not be changed]
```

Be direct. Flag real issues only — this is used to make implementation decisions.

---

---

## Claude Sonnet 4.6 — Code Review (Actual Code) — 2026-03-08 07:00 UTC

### Overall Assessment
**Pass with critical fixes required.** The structure is clean and follows the architecture well.
Three bugs will cause silent failures or wrong results in production. Fix those before first run.

---

### Critical Issues

**1. `embedder.py` uses bag-of-words, not embeddings (Architecture Violation)**

`stage1_select` computes `bow_similarity` using a `Counter`-based cosine over word tokens.
This is keyword overlap, not semantic similarity. "Safety enforcement" will not match
"production reliability" even though they mean the same thing for this candidate.

The architecture specified `voyage-code-2`. Either integrate it or explicitly document that
Stage 1 is keyword-based as a temporary measure. As-is, it will miss roles that describe
the candidate's work using different vocabulary — exactly the typecast problem we're solving.

```python
# embedder.py — current
def bow_similarity(a: str, b: str) -> float:
    ca = Counter(tokenize(a))   # keyword overlap only
    ...
```

Fix: integrate `voyageai` client for real embeddings, or at minimum expand the resume text
to include all synonym vocabulary (observability = monitoring = metrics = telemetry, etc.)
so BOW hits more broadly until proper embeddings are added.

---

**2. `claude_matcher.py` uses wrong model ID**

```python
# claude_matcher.py line ~60
body = {
    "model": "claude-3-5-sonnet-latest",  # WRONG
    ...
}
```

Should be `claude-sonnet-4-6` — the project standard established in the trading agent
CHANGELOG and the Hydra config. `claude-3-5-sonnet-latest` resolves to an older model and
will eventually return 400 errors (same bug that was fixed in the trading agent Feb 25).

Fix:
```python
"model": "claude-sonnet-4-6",
```

---

**3. `stage2_match` blocks the asyncio event loop**

`main.py` calls `stage2_match(...)` without `await`. The function is not `async` and uses
`urllib.request.urlopen` (synchronous). During the Claude API call (~10–30 seconds), the
entire event loop is blocked. On Windows this will cause Task Scheduler timeout issues
and means no other async work can run concurrently.

Fix — wrap the blocking call in `asyncio.to_thread`:

```python
# claude_matcher.py
async def stage2_match(postings, resume_text, anthropic_api_key=""):
    ...
    try:
        response = await asyncio.to_thread(_call_claude, api_key, resume_text, postings)
    except Exception:
        return _fallback_reasoning(postings)
```

And update `main.py`:
```python
stage2 = await stage2_match(stage1, resume_text, SETTINGS.anthropic_api_key)
```

---

### Medium Issues

**4. `scrape_all` swallows exceptions with no logging or Telegram alert**

```python
# main.py
async def scrape_all(scrapers):
    results = await asyncio.gather(*(s.fetch() for s in scrapers), return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            continue   # silent failure — no log, no alert
```

Same pattern that caused silent failures in the trading agent. A broken scraper will silently
return zero results. Over time this erodes coverage without any visibility.

Fix — log and alert per the established pattern:
```python
for scraper, result in zip(scrapers, results):
    if isinstance(result, Exception):
        log.warning(f"[{scraper.__class__.__name__}] failed: {result}")
        await _tg(f"[JobAgent] Scraper {scraper.__class__.__name__} failed: {result}")
```

---

**5. `max_tokens: 3000` will truncate for 30-posting batches**

30 job descriptions × ~50 tokens output each = ~1500 output tokens minimum. But Claude also
generates reasoning text before the JSON array in many responses. At 3000 tokens the JSON
will truncate mid-array, causing `json.loads` to fail and fall back to `_fallback_reasoning`
silently. The fallback uses BOW scores mapped to match scores — not what we want.

Fix: increase to `max_tokens: 6000`, and/or implement the batch-of-10 approach from the
architecture review.

---

**6. `notifier.py` will hit Telegram's 4096 character limit**

10 job postings with full match_reason, risk, and URL strings will routinely exceed 4096
characters. Telegram will return a 400 error and the digest is silently lost.

Fix — split into chunks of ≤4000 chars:
```python
def send_telegram(token, chat_id, text):
    for chunk in _split_message(text, 4000):
        # send each chunk
```

Or truncate `match_reason` and `risk` fields in `format_digest` to 120 chars each.

---

**7. HN company name extraction is naive**

```python
# scrapers/hn.py
company = title.split(" ")[0] if title else "HNCompany"
```

HN "Who's Hiring" posts follow formats like:
- `Acme Corp | Remote | Senior SWE | $180K`
- `Acme Corp (YC S24) - Full-time - San Francisco`

The first word (`Acme`) will be extracted, not `Acme Corp`. This produces garbage `company`
names and broken deduplication hashes.

Fix:
```python
def extract_company(text: str) -> str:
    # Split on common delimiters: |, -, (, newline
    for delim in ["|", " - ", "(", "\n"]:
        if delim in text:
            return text.split(delim)[0].strip()
    return text[:40].strip()
```

---

### Low Priority

**8. `resume.txt` placeholder in config.py**

`load_resume_text()` falls back to a 2-sentence stub if `data/resume.txt` doesn't exist.
The actual resume text needs to be saved to `data/resume.txt` before first run. If the file
is missing, Stage 1 BOW similarity runs against the stub — match scores will be meaningless.

Action: create `data/resume.txt` with the full resume text from `JOB_AGENT_SUMMARY.md`.

---

**9. `salary_data.json` missing `last_updated` field**

As flagged in the architecture review. Add metadata before the salary data goes stale:
```json
{
  "last_updated": "2026-03-08",
  "entries": [...]
}
```

Update `salary.py` to warn via Telegram if `last_updated` is >180 days old.

---

**10. No `.env` file in `job-agent/`**

The config reads from `BASE_DIR / ".env"` but no `.env` file exists in the directory.
Keys are also in the trading agent `.env` — they need to be copied or symlinked.

Minimum required:
```
ANTHROPIC_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

---

### What's Solid — Do Not Change

- `models.py` — clean dataclass, `reason_codes` list is a good debuggability hook
- `db.py` — correct schema, proper upsert with `ON CONFLICT`, `seen_hashes` dedup is correct
- `ranker.py` — formula matches architecture exactly, clean separation from embedder
- `utils.py` — `normalize_title` stripping level words before hashing is correct (prevents `Senior SWE` and `Staff SWE` deduping as the same role)
- `config.py` — frozen dataclass with env fallbacks, clean pattern
- `scrapers/base.py` — `parse_timestamp` handles int/float/ISO string correctly
- `scrapers/greenhouse.py` and `scrapers/lever.py` — correct public API usage, per-company exception handling

---

### Fix Priority Order

1. Model ID (`claude-sonnet-4-6`) — 2 min fix, breaks production if left
2. `stage2_match` async fix — prevents event loop blocking
3. `max_tokens: 3000` → `6000` — prevents silent JSON truncation
4. Create `data/resume.txt` with actual resume — meaningless scores without it
5. Create `.env` — agent won't run without API keys
6. Scraper failure logging — Telegram alert on scraper exceptions
7. Telegram message chunking — prevents digest loss on long outputs
8. HN company name extraction — fix before HN becomes a real data source
9. BOW → embeddings — needed for semantic matching to actually work

---

## Gemini Review

<!-- Append here -->
