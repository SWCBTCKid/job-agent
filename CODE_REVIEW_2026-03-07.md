# Code Review: Job Search Agent (v1.0.0-scaffold)
**Date:** 2026-03-07
**Reviewer:** Gemini 2.0 Pro
**Status:** Architecture Approved / Implementation Incomplete

## Overview
This review covers the initial scaffolded implementation of the Job Search Agent located in D:\Claude Trading Agent\job-agent.

## Architecture & Design
The project follows a clean, modular structure that aligns with the "Two-Stage Matcher" consensus.
- **Strengths:** 
    - Good use of syncio for concurrent scraping.
    - Clear separation between Scraping, Database, and Matching logic.
    - Decoupled configuration management in config.py.
- **Concerns:**
    - **Stage 1 Matcher (embedder.py):** Currently uses simple Bag-of-Words (BoW) cosine similarity. While functional, it lacks semantic understanding (e.g., won't know "Distributed Systems" is related to "Infrastructure"). 
    - **Stage 2 Matcher (claude_matcher.py):** Uses synchronous urllib.request. This blocks the event loop and will significantly slow down the agent when processing multiple jobs.

## Detailed Findings

### 1. Scrapers (scrapers/)
- **Greenhouse/Lever:** Well-implemented using public APIs.
- **Workday/LinkedIn/YC:** These are currently **empty stubs**. They return empty lists or have no logic. 
- **Recommendation:** Implement WorkdayScraper using the XHR/JSON interception strategy discussed in the consensus. Avoid raw HTML parsing where possible.

### 2. Matching Logic (matcher/)
- **Security:** claude_matcher.py correctly pulls the API key from SETTINGS, but lacks error handling for malformed Claude responses beyond a basic e.search.
- **Optimization:** The BoW similarity in embedder.py should be replaced with sentence-transformers (local) or oyage-code-2 (API) as soon as possible to improve Tier 1 accuracy.
- **Level Calibration:** _infer_level_fit in claude_matcher.py correctly identifies "too_senior" (Staff/Principal) vs "senior", aligning with the candidate's mid-to-senior SV profile.

### 3. Data & State (db.py, salary.py)
- **Salary Inference:** The logic is a bit rigid. It only checks exact company/level matches. 
- **Recommendation:** Add a broader fallback for "Industry Average" based on the ole_family if the specific company isn't in salary_data.json.
- **Database:** JobDB uses sqlite3. It's lightweight and perfect for this local use case.

### 4. Code Quality & Standards
- **Pros:** Consistent use of type hints (__future__.annotations), clean imports, and modularity.
- **Cons:** Missing docstrings for many core functions. 

## Security Check
- **Secrets:** No hardcoded keys found. .env is correctly ignored in .gitignore.
- **Validation:** main.py has a --dry-run flag, which is excellent for testing without burning API tokens or spamming Telegram.

## Final Assessment
The scaffold is **70% complete**. The core orchestration logic is solid.
**Priority 1:** Replace urllib with httpx or iohttp for async Claude calls.
**Phase 2:** Implement the missing scrapers for LinkedIn and YC.
**Phase 3:** Upgrade Stage 1 to a semantic embedding model.

---
*Signed,*
*Gemini 2.0 Pro*

---

# Code Review — Post Hardening Pass — 2026-03-08
**Reviewer:** Claude Sonnet 4.6
**Scope:** Full codebase after 2026-03-08 hardening pass
**Changelog reference:** Items marked Fixed/Changed in CHANGELOG.md

---

## What Was Fixed Correctly

- Stage 2 is now `async` — `stage2_match` uses `httpx` with `asyncio.to_thread` urllib fallback. `main.py` correctly `await`s it.
- Model ID is now config-driven via `SETTINGS.claude_model` — no longer hardcoded per file.
- Embedder upgraded from BoW to semantic-first: `sentence-transformers` → Voyage → TF-IDF fallback.
- `_coerce_item` and `_extract_json_array` added — Claude response is validated and type-coerced before use.
- Source seeds introduced — non-API scrapers can accept seed URLs from `data/source_seeds.json`.

Gemini's Priority 1 (async Claude calls) and Phase 3 (semantic embeddings) are now addressed.

---

## Critical — Fix Before First Live Run

### 1. Default Claude model is still wrong — `config.py:39`

```python
claude_model: str = os.getenv("JOB_AGENT_CLAUDE_MODEL", "claude-3-5-sonnet-latest")
```

The env var override works, but no `.env` file exists in this directory yet, so the wrong
default fires on every run. Same bug fixed in the trading agent on 2026-02-25.

**Fix:** Change default to `"claude-sonnet-4-6"`.

### 2. No `.env` file — agent won't run

`config.py` reads from `BASE_DIR / ".env"` which doesn't exist. All keys fall to empty
strings. Stage 2 silently falls back to `_fallback_reasoning`. Telegram never sends.

**Fix:** Create `job-agent/.env`:
```
ANTHROPIC_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
JOB_AGENT_CLAUDE_MODEL=claude-sonnet-4-6
```

### 3. `data/resume.txt` does not exist — all scores are meaningless

`load_resume_text()` falls back to a 2-sentence stub. Every embedding and TF-IDF score
is computed against that stub. Stage 1 rankings are garbage until the file is created.

**Fix:** Create `data/resume.txt` with the full resume text from `JOB_AGENT_SUMMARY.md`.

---

## Medium — Fix Before Trusting Output

### 4. `scrape_all` swallows exceptions silently — `main.py:96–102`

```python
if isinstance(result, Exception):
    continue   # no log, no alert
```

A failing scraper returns zero postings with zero signal. Same pattern as the trading agent
silent failures. On a day when Greenhouse returns 503, you get an empty digest with no
explanation.

**Fix:** Log + Telegram alert per scraper failure, same pattern as `live_trader.py`.

### 5. `run_agent` has no outer error handler — `main.py:119`

If DB init fails, resume load crashes, or any step raises unexpectedly, the process exits
with no Telegram notification. On Task Scheduler this is an invisible missed run.

**Fix:** Wrap `run_agent` body in try/except, send Telegram alert before re-raising.

### 6. `max_tokens: 3000` will truncate Stage 2 — `claude_matcher.py:131, 169`

30 JDs × ~50 tokens output each = ~1500 tokens minimum. Claude often generates reasoning
before the JSON array. Truncation causes `_extract_json_array` to raise and falls back to
`_fallback_reasoning` silently — TF-IDF scores dressed as Claude scores.

**Fix:** Increase to `max_tokens: 6000`.

### 7. Stage 2 failure is completely silent — `claude_matcher.py:192–195`

```python
except Exception:
    return _fallback_reasoning(postings)  # no log, no alert
```

When Claude fails, the digest looks normal but scores are meaningless. Should log the
exception and send a Telegram alert before falling back.

### 8. `notifier.py` has no Telegram chunking — 4096 char limit

10 job entries with `match_reason`, `risk`, and URLs routinely exceeds 4096 chars. Telegram
returns 400, `send_telegram` raises `RuntimeError`, digest is never delivered.

**Fix:** Split into ≤4000-char chunks before sending.

---

## Low Priority

### 9. `salary_gate` hardcodes level as `"senior"` — `main.py:39`

```python
infer_salary(posting.company, "senior", ...)
```

A mid-level posting gets looked up at senior salary, potentially passing the $150K floor
incorrectly. Use `_infer_level_fit(posting.title, posting.description)` to derive level first.

### 10. HN company name extraction still naive — `scrapers/hn.py:32`

```python
company = title.split(" ")[0]
```

`"Acme Corp | Remote"` → extracts `"Acme"` not `"Acme Corp"`. Breaks dedup hashes and
produces bad names in the digest.

### 11. `_infer_tier` uses substring matching — `claude_matcher.py:17–24`

`"ml"` matches `"small"`, `"email"`, `"normally"`. Use `re.search(r"\bml\b", text)` style
word-boundary matching.

---

## Agreement with Gemini's Review

- Agree on salary inference rigidity — industry-average fallback is a good addition.
- Agree empty stubs are Phase 2 work — Greenhouse + Lever + HN cover enough sources for a first run.
- Agree on docstrings being low priority — not blocking.

---

## Fix Priority Order

| # | File | Issue |
|---|------|-------|
| 1 | `config.py:39` | Default model → `claude-sonnet-4-6` |
| 2 | `job-agent/.env` | Create with API keys |
| 3 | `data/resume.txt` | Create with full resume |
| 4 | `main.py:96–102` | Log + alert on scraper failures |
| 5 | `main.py:119` | Outer try/except in `run_agent` |
| 6 | `claude_matcher.py:131,169` | `max_tokens` 3000 → 6000 |
| 7 | `claude_matcher.py:192` | Log + alert on Stage 2 failure |
| 8 | `notifier.py` | Telegram 4096 char chunking |
| 9 | `main.py:39` | Fix hardcoded `"senior"` in salary lookup |
| 10 | `scrapers/hn.py:32` | Better company name extraction |
| 11 | `claude_matcher.py:17` | Word-boundary matching in `_infer_tier` |

*Signed,*
*Claude Sonnet 4.6*
