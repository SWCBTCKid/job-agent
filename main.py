from __future__ import annotations

from job_logging import ensure_process_logging

ensure_process_logging(__file__)

import argparse
import asyncio
import json
import logging
import re
import subprocess
import sys
from pathlib import Path

from config import DATA_DIR, SETTINGS, STATE_DIR, load_resume_text, load_source_seeds, load_target_companies
from db import JobDB
from resume_ingest import ingest_resume

BASE_DIR = Path(__file__).resolve().parent
PROFILES_DIR = BASE_DIR / "profiles"

from matcher import rank_postings, stage1_select
from models import JobPosting
from notifier import format_digest, send_telegram, send_telegram_safe
from salary import infer_salary, load_salary_table
from scrapers import (
    AshbyScraper,
    BuiltinSFScraper,
    GreenhouseScraper,
    HNScraper,
    ICIMSScraper,
    LeverScraper,
    LinkedInScraper,
    PragmaticScraper,
    SerpScraper,
    TheirStackScraper,
    WellfoundScraper,
    WorkdayScraper,
    YCScraper,
)

LOGGER = logging.getLogger(__name__)


def is_bay_area(posting: JobPosting) -> bool:
    text = f"{posting.location} {posting.title} {posting.description}".lower()
    bay_area_tokens = [
        "bay area",
        "san francisco",
        "sf, ca",
        "oakland",
        "berkeley",
        "san jose",
        "palo alto",
        "mountain view",
        "sunnyvale",
        "redwood city",
        "san mateo",
        "fremont",
        "santa clara",
        "menlo park",
        "cupertino",
        "south san francisco",
        "foster city",
        "hayward",
        "emeryville",
        "burlingame",
        "san carlos",
        "belmont",
    ]
    return any(token in text for token in bay_area_tokens)


def requires_clearance(posting: JobPosting) -> bool:
    text = f"{posting.title} {posting.description}".lower()
    patterns = [
        r"\bts\s*/\s*sci\b",
        r"\btop\s+secret\b",
        r"\bsecret\s+clearance\b",
        r"\bsecurity\s+clearance\s+required\b",
        r"\brequires?\s+(an?\s+)?active\s+clearance\b",
        r"\bactive\s+(u\.?s\.?\s+)?security\s+clearance\b",
        r"\bdod\s+clearance\b",
        r"\bclearance\s+eligible\b",
        r"\bmust\s+(hold|have|obtain)\s+(a\s+)?(secret|top\s+secret|ts|clearance)\b",
        r"\bpolygraph\b",
    ]
    return any(re.search(pattern, text) for pattern in patterns)


def requires_us_citizenship(posting: JobPosting) -> bool:
    text = f"{posting.title} {posting.description}".lower()
    patterns = [
        r"\bu\.?s\.?\s+citizenship\s+required\b",
        r"\brequires?\s+u\.?s\.?\s+citizenship\b",
        r"\bmust\s+be\s+(a\s+)?u\.?s\.?\s+citizen\b",
        r"\bmust\s+have\s+u\.?s\.?\s+citizenship\b",
        r"\bmust\s+be\s+(a\s+)?u\.?s\.?\s+person\b",
        r"\bu\.?s\.?\s+person\s+required\b",
    ]
    return any(re.search(pattern, text) for pattern in patterns)


NON_ENGINEERING_TITLE_PATTERNS = [
    r"\bprogram manager\b",
    r"\bproduct manager\b",
    r"\bproject manager\b",
    r"\bsolutions architect\b",      # pre-sales; solutions engineer is fine
    r"\bdata scientist\b",
    r"\bresearch scientist\b",
    r"\bstaff\b",                    # no staff-level roles
    r"\bjunior\b",
    r"\bentry.?level\b",
    r"engineer\s+i\b",               # "Engineer I" but not "Engineer II/III"
    r"\bintern\b",
    r"\brecruiter\b",
    r"\brecruiting\b",
    r"\bpeople ops\b",
    r"\bhuman resources\b",
    r"\bfinance\b",
    r"\baccounting\b",
    r"\blegal counsel\b",
    r"\bmarketing manager\b",
    r"\bsales manager\b",
    r"\baccount executive\b",
    r"\bcustomer success\b",
    r"\btechnical writer\b",
    r"\bdesign manager\b",
]

_NON_ENG_RE = re.compile("|".join(NON_ENGINEERING_TITLE_PATTERNS), re.IGNORECASE)


def is_engineering_role(posting: JobPosting) -> bool:
    """Return False for PM, data science, recruiting, and other non-engineering roles."""
    return _NON_ENG_RE.search(posting.title) is None


def role_family_for(posting: JobPosting) -> str:
    text = f"{posting.title} {posting.description}".lower()
    if "security" in text or "auth" in text:
        return "security"
    if "platform" in text:
        return "platform"
    return "infrastructure"


def level_for_salary_lookup(posting: JobPosting) -> str:
    text = f"{posting.title} {posting.description}".lower()
    if any(k in text for k in ["principal", "staff", "distinguished"]):
        return "senior"
    if re.search(r"\bii\b", text) or "engineer ii" in text or "mid-level" in text or "mid level" in text:
        return "mid"
    return "senior"


def salary_gate(posting: JobPosting, salary_table: list[dict], floor: int) -> bool:
    if posting.salary_min is None and posting.salary_max is None:
        low, high, confidence = infer_salary(
            posting.company,
            level_for_salary_lookup(posting),
            role_family_for(posting),
            salary_table,
        )
        posting.salary_min = low
        posting.salary_max = high
        posting.salary_confidence = confidence
        posting.salary_inferred = confidence.startswith("inferred")

    if posting.salary_min is None and posting.salary_max is None:
        posting.reason_codes.append("UNKNOWN_SALARY")
        return True

    max_salary = posting.salary_max or posting.salary_min or 0
    if max_salary < floor:
        posting.reason_codes.append("REJECT_SALARY_FLOOR")
        return False
    return True


def classify_competition(posting: JobPosting) -> str:
    text = f"{posting.title} {posting.description}".lower()
    if posting.source == "linkedin":
        posting.linkedin_crosspost = True
        return "high"
    if posting.age_days <= 3 and posting.source_priority == 1:
        return "low"
    if "easy apply" in text or posting.age_days > 20:
        return "high"
    return "medium"


# ── Profile management ────────────────────────────────────────────────────────

def ensure_default_profile(db: JobDB) -> None:
    """On first run, seed the default profile from the existing JSON files."""
    if db.profile_exists("gpu_autonomous"):
        return
    LOGGER.info("Seeding default profile 'gpu_autonomous' from JSON files...")
    companies = load_target_companies()
    seeds = load_source_seeds()
    db.create_profile("gpu_autonomous", "GPU / Autonomous Vehicles", is_default=True)
    db.seed_profile_companies("gpu_autonomous", companies)
    db.seed_profile_sources("gpu_autonomous", seeds)
    n_companies = len(companies)
    n_sources = sum(len(v) for v in seeds.values())
    print(f"Default profile 'gpu_autonomous' seeded — {n_companies} companies, {n_sources} sources")


def import_profile(profile_id: str, db: JobDB) -> None:
    """Import a profile from profiles/{profile_id}/companies.json + sources.json."""
    profile_dir = PROFILES_DIR / profile_id
    companies_path = profile_dir / "companies.json"
    sources_path = profile_dir / "sources.json"

    if not profile_dir.exists():
        print(f"Error: profiles/{profile_id}/ directory not found")
        print(f"Create it with companies.json and sources.json in {PROFILES_DIR}")
        return

    companies: list[dict] = []
    sources: dict = {}

    if companies_path.exists():
        companies = json.loads(companies_path.read_text(encoding="utf-8"))
    else:
        print(f"Warning: {companies_path} not found — no companies will be imported")

    if sources_path.exists():
        sources = json.loads(sources_path.read_text(encoding="utf-8"))
    else:
        print(f"Warning: {sources_path} not found — no sources will be imported")

    # Create profile if new, otherwise just re-seed (add without replacing)
    if not db.profile_exists(profile_id):
        is_default = profile_id == "gpu_autonomous"
        label = profile_id.replace("_", " ").title()
        db.create_profile(profile_id, label, is_default=is_default)
        print(f"Created profile '{profile_id}'")
    else:
        print(f"Profile '{profile_id}' already exists — adding companies/sources")

    db.seed_profile_companies(profile_id, companies)
    db.seed_profile_sources(profile_id, sources)

    n_sources = sum(len(v) for v in sources.values())
    print(f"Profile '{profile_id}' imported — {len(companies)} companies, {n_sources} sources")


def cmd_list_profiles(db: JobDB) -> None:
    profiles = db.list_profiles()
    if not profiles:
        print("No profiles found. Run: python main.py --import-profile gpu_autonomous")
        return
    print(f"\n{'ID':<20} {'Label':<35} {'Companies':>10}  Default")
    print("-" * 72)
    for p in profiles:
        default_marker = " *" if p["is_default"] else ""
        print(f"{p['id']:<20} {p['label']:<35} {p['company_count']:>10}{default_marker}")
    print()


def cmd_list_companies(profile_id: str, db: JobDB) -> None:
    if not db.profile_exists(profile_id):
        print(f"Profile '{profile_id}' not found. Run --list-profiles to see available profiles.")
        return
    companies = db.list_profile_companies(profile_id)
    if not companies:
        print(f"No companies in profile '{profile_id}'")
        return
    print(f"\nProfile: {profile_id} — {len(companies)} companies\n")
    print(f"{'Name':<35} {'ATS':<12} {'Slug':<30} {'Boost':>6}  Active")
    print("-" * 90)
    for c in companies:
        active = "yes" if c["active"] else "no"
        print(f"{c['name']:<35} {c['ats']:<12} {c['slug']:<30} {c['tier_boost']:>6.1f}  {active}")
    print()


def cmd_add_company(
    profile_id: str, name: str, ats: str, slug: str, tier_boost: float, db: JobDB
) -> None:
    if not db.profile_exists(profile_id):
        print(f"Profile '{profile_id}' not found.")
        return
    db.add_profile_company(profile_id, name, ats, slug, tier_boost)
    print(f"Added '{name}' ({ats}/{slug}, boost={tier_boost}) to profile '{profile_id}'")


def cmd_list_resumes(db: JobDB) -> None:
    resumes = db.list_resumes()
    if not resumes:
        print("No resumes ingested yet. Run: python main.py --resume /path/to/resume.txt")
        return
    print(f"\n{'ID':<14} {'Quality':>8}  {'Path'}")
    print("-" * 60)
    for r in resumes:
        score = f"{r.quality_score:.1f}/10" if r.quality_score is not None else "  n/a"
        print(f"{r.id:<14} {score:>8}  {r.path}")
    print()


def build_scrapers(db: JobDB, profile_id: str) -> list:
    """Build scraper list from DB profile — reads profile_companies + profile_sources."""
    companies = db.get_profile_companies(profile_id)
    sources   = db.get_profile_sources(profile_id)

    # Group sources by type into the same format scrapers expect
    def _sources_of(source_type: str) -> list[dict]:
        return [s["config"] for s in sources if s["source_type"] == source_type]

    greenhouse = [
        (c["name"], c["slug"], float(c.get("tier_boost", 1.0)))
        for c in companies if c.get("ats") == "greenhouse"
    ]
    lever = [
        (c["name"], c["slug"], float(c.get("tier_boost", 1.0)))
        for c in companies if c.get("ats") == "lever"
    ]

    LOGGER.info(
        "build_scrapers: profile=%s  greenhouse=%d  lever=%d  total_companies=%d",
        profile_id, len(greenhouse), len(lever), len(companies),
    )

    return [
        GreenhouseScraper(greenhouse),
        LeverScraper(lever),
        AshbyScraper(_sources_of("ashby")),
        ICIMSScraper(_sources_of("icims")),
        TheirStackScraper(SETTINGS.theirstack_api_key, _sources_of("theirstack")),
        SerpScraper(SETTINGS.serpapi_key, _sources_of("serp")),
        BuiltinSFScraper(tier_boost=1.0),
        HNScraper(),
        WorkdayScraper(_sources_of("workday")),
        WellfoundScraper(_sources_of("wellfound")),
        YCScraper(_sources_of("yc")),
        PragmaticScraper(_sources_of("pragmatic")),
        LinkedInScraper(_sources_of("linkedin")),
    ]


async def scrape_all(scrapers: list, alert_cb=None) -> list[JobPosting]:
    results = await asyncio.gather(*(s.fetch() for s in scrapers), return_exceptions=True)
    merged: list[JobPosting] = []
    for scraper, result in zip(scrapers, results):
        if isinstance(result, Exception):
            msg = f"[JobAgent] Scraper {scraper.__class__.__name__} failed: {result}"
            LOGGER.error(msg, exc_info=result)
            if alert_cb:
                await alert_cb(msg)
            continue
        merged.extend(result)
    return merged


def dedupe_new(postings: list[JobPosting], db: JobDB) -> list[JobPosting]:
    out: list[JobPosting] = []
    seen_ids: set[str] = set()
    for p in postings:
        if p.id in seen_ids:
            continue
        seen_ids.add(p.id)
        if db.seen_recently(p.id):
            continue
        db.mark_seen(p.id)
        out.append(p)
    return out


async def run_agent(dry_run: bool = False) -> dict:
    """Run one full search/match/rank/notify cycle."""
    async def alert(msg: str) -> None:
        if dry_run:
            LOGGER.warning(msg)
            return
        send_telegram_safe(SETTINGS.telegram_bot_token, SETTINGS.telegram_chat_id, msg)

    db = JobDB(STATE_DIR / "jobs.db")
    try:
        resume_text = load_resume_text()
        salary_table = load_salary_table(DATA_DIR / "salary_data.json")

        db.reset_unalerted_scores()

        profile_id = db.get_default_profile_id() or "gpu_autonomous"
        scrapers = build_scrapers(db, profile_id)
        raw_postings = await scrape_all(scrapers, alert_cb=alert)
        new_postings = dedupe_new(raw_postings, db)
        filtered_citizenship = []
        for p in new_postings:
            if requires_us_citizenship(p):
                p.reason_codes.append("REJECT_US_CITIZENSHIP_REQUIRED")
                continue
            if requires_clearance(p):
                p.reason_codes.append("REJECT_CLEARANCE_REQUIRED")
                continue
            filtered_citizenship.append(p)

        filtered_location = []
        for p in filtered_citizenship:
            if not is_bay_area(p):
                p.reason_codes.append("REJECT_NON_BAY_AREA")
                continue
            filtered_location.append(p)

        filtered_salary = [p for p in filtered_location if salary_gate(p, salary_table, SETTINGS.salary_floor)]

        filtered_role = []
        for p in filtered_salary:
            if not is_engineering_role(p):
                p.reason_codes.append("REJECT_NON_ENGINEERING_ROLE")
                continue
            filtered_role.append(p)

        # Classify competition and store all hard-filtered candidates
        for p in filtered_role:
            p.competition = classify_competition(p)
            db.store_candidate(p)

        # Score all unscored postings from DB
        unscored = db.get_unscored()
        stage1 = stage1_select(unscored, resume_text, SETTINGS.top_n_stage2)
        db.save_stage1_scores(unscored)  # persist stage1+embed scores for all, not just top N

        # Launch Haiku background scorer — fire and forget, does not block main flow
        if not dry_run:
            scorer_path = Path(__file__).resolve().parent / "scorer_worker.py"
            subprocess.Popen(
                [sys.executable, str(scorer_path), "--rpm", "50"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            LOGGER.info("Launched scorer_worker.py in background")

        # Rank across ALL unalerted scored postings
        all_candidates = db.get_unalerted_scored()
        ranked = rank_postings(all_candidates, SETTINGS.digest_count)

        for p in all_candidates:
            db.save_final_score(p)

        if not dry_run:
            for p in ranked:
                db.mark_alerted(p)

        digest = format_digest(ranked)
        if not dry_run and ranked:
            send_telegram(SETTINGS.telegram_bot_token, SETTINGS.telegram_chat_id, digest)

        return {
            "raw_count": len(raw_postings),
            "new_count": len(new_postings),
            "after_citizenship": len(filtered_citizenship),
            "after_location": len(filtered_location),
            "after_salary": len(filtered_salary),
            "after_role_filter": len(filtered_role),
            "unscored_count": len(unscored),
            "all_candidates_count": len(all_candidates),
            "ranked_count": len(ranked),
            "digest": digest,
        }
    except Exception as exc:
        msg = f"[JobAgent] Fatal run failure: {exc}"
        LOGGER.exception(msg)
        await alert(msg)
        raise
    finally:
        db.close()

async def run_resume_flow(
    resume_path: str,
    profile_id: str,
    db: JobDB,
    dry_run: bool = False,
) -> dict:
    """
    MVP resume-driven pipeline — Steps 1 and 2 (sync).
    Step 3 (Claude ranking) is launched as a background subprocess.
    """
    # ── Step 1: Resume ingest ─────────────────────────────────────
    record = ingest_resume(resume_path, db)
    resume_id = record.id
    resume_text = Path(resume_path).read_text(encoding="utf-8")

    # Ensure per-resume postings table exists
    db.create_resume_postings_table(resume_id)

    # ── Step 2: Scrape + filter + Stage 1 ────────────────────────
    print(f"Scraping with profile: {profile_id}")
    salary_table = load_salary_table(DATA_DIR / "salary_data.json")
    scrapers = build_scrapers(db, profile_id)
    raw_postings = await scrape_all(scrapers)
    print(f"Scraped: {len(raw_postings)} raw postings")

    # Hard filters (same logic as legacy flow)
    after_citizenship = [
        p for p in raw_postings
        if not requires_us_citizenship(p) and not requires_clearance(p)
    ]
    after_location = [p for p in after_citizenship if is_bay_area(p)]
    after_salary   = [p for p in after_location if salary_gate(p, salary_table, SETTINGS.salary_floor)]
    after_role     = [p for p in after_salary if is_engineering_role(p)]

    print(f"After hard filters:  {len(after_role)}  "
          f"(citizenship/clearance: -{len(raw_postings) - len(after_citizenship)}, "
          f"location: -{len(after_citizenship) - len(after_location)}, "
          f"salary: -{len(after_location) - len(after_salary)}, "
          f"role: -{len(after_salary) - len(after_role)})")

    # Classify competition, store in resume-specific table
    for p in after_role:
        p.competition = classify_competition(p)

    # Stage 1: embed all filtered postings against this resume
    domain_tiers_path = PROFILES_DIR / profile_id / "domain_tiers.json"
    domain_tiers = json.loads(domain_tiers_path.read_text(encoding="utf-8")) if domain_tiers_path.exists() else None
    if domain_tiers:
        LOGGER.info("Loaded domain tiers from profiles/%s/domain_tiers.json", profile_id)
    print("Running Stage 1 embedding...")
    stage1_scored = stage1_select(after_role, resume_text, len(after_role), domain_tiers=domain_tiers)  # score all, no top-N cut
    db.save_resume_stage1_scores(resume_id, stage1_scored)

    # Store all scored candidates in resume table
    for p in stage1_scored:
        db.store_resume_candidate(resume_id, p)

    above_threshold = [
        p for p in stage1_scored
        if (p.stage1_score or 0) >= SETTINGS.stage1_threshold
    ]
    print(f"Stage 1 scored: {len(stage1_scored)}  |  "
          f"Above threshold ({SETTINGS.stage1_threshold}): {len(above_threshold)}")

    if not above_threshold:
        print("No candidates above threshold — nothing to rank.")
        return {
            "resume_id": resume_id,
            "profile_id": profile_id,
            "raw": len(raw_postings),
            "after_filters": len(after_role),
            "above_threshold": 0,
            "ranker_pid": None,
        }

    # ── Step 3: Launch background Claude ranker ───────────────────
    if dry_run:
        print(f"\n[dry-run] Skipping background ranker — {len(above_threshold)} candidates ready.")
        print(f"[dry-run] Output would be written to: output/results_{resume_id}_<ts>.json")
        ranker_pid = None
    else:
        ranker_path = BASE_DIR / "ranker_worker.py"
        proc = subprocess.Popen(
            [sys.executable, str(ranker_path),
             "--resume-id", resume_id,
             "--profile-id", profile_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        ranker_pid = proc.pid
        print(f"\nBackground ranker started (pid={ranker_pid})")
        print("You will be notified via Telegram when ranking is complete.")

    return {
        "resume_id": resume_id,
        "profile_id": profile_id,
        "raw": len(raw_postings),
        "after_filters": len(after_role),
        "above_threshold": len(above_threshold),
        "ranker_pid": ranker_pid,
    }


def main() -> None:
    log_path = ensure_process_logging(__file__)
    LOGGER.info("Run log file: %s", log_path)
    parser = argparse.ArgumentParser(description="Job search agent")

    # Legacy run args
    parser.add_argument("--dry-run", action="store_true", help="Do not send Telegram or mark alerts")
    parser.add_argument("--json", action="store_true", help="Print machine-readable output")

    # MVP resume-driven flow
    parser.add_argument("--resume", metavar="PATH", help="Path to resume (.txt or .md) — triggers MVP flow")
    parser.add_argument("--profile", metavar="PROFILE_ID", default=None,
                        help="Search profile to use (default: gpu_autonomous)")

    # Profile management commands (early-exit, no scraping)
    parser.add_argument("--import-profile", metavar="PROFILE_ID",
                        help="Import profile from profiles/{id}/companies.json + sources.json")
    parser.add_argument("--list-profiles", action="store_true", help="List all search profiles")
    parser.add_argument("--list-companies", action="store_true",
                        help="List companies in a profile (use with --profile)")
    parser.add_argument("--list-resumes", action="store_true", help="List all ingested resumes")
    parser.add_argument("--add-company", action="store_true",
                        help="Add a company to a profile (requires --profile --name --ats --slug)")
    parser.add_argument("--name", help="Company name (for --add-company)")
    parser.add_argument("--ats", help="ATS type: greenhouse|lever|ashby|workday|icims (for --add-company)")
    parser.add_argument("--slug", help="ATS slug (for --add-company)")
    parser.add_argument("--tier-boost", type=float, default=1.0,
                        help="Tier boost multiplier (for --add-company, default 1.0)")

    args = parser.parse_args()

    db = JobDB(STATE_DIR / "jobs.db")

    # Always ensure default profile exists on first run
    ensure_default_profile(db)

    # ── Profile management commands (early exit) ──────────────────────────────
    if args.import_profile:
        import_profile(args.import_profile, db)
        db.close()
        return

    if args.list_profiles:
        cmd_list_profiles(db)
        db.close()
        return

    if args.list_companies:
        profile_id = args.profile or db.get_default_profile_id() or "gpu_autonomous"
        cmd_list_companies(profile_id, db)
        db.close()
        return

    if args.list_resumes:
        cmd_list_resumes(db)
        db.close()
        return

    if args.add_company:
        if not all([args.profile, args.name, args.ats, args.slug]):
            print("--add-company requires: --profile --name --ats --slug")
            parser.print_help()
            db.close()
            return
        cmd_add_company(args.profile, args.name, args.ats, args.slug, args.tier_boost, db)
        db.close()
        return

    # ── Resume-driven MVP flow ────────────────────────────────────────────────
    if args.resume:
        profile_id = args.profile or db.get_default_profile_id() or "gpu_autonomous"
        db.close()
        db = JobDB(STATE_DIR / "jobs.db")
        summary = asyncio.run(run_resume_flow(args.resume, profile_id, db, dry_run=args.dry_run))
        db.close()
        print(f"\nResume ID:  {summary['resume_id']}")
        print(f"Profile:    {summary['profile_id']}")
        print(f"Scraped:    {summary['raw']}")
        print(f"Filtered:   {summary['after_filters']}")
        print(f"To Claude:  {summary['above_threshold']}")
        if summary.get("ranker_pid"):
            print(f"Ranker PID: {summary['ranker_pid']}")
        return

    # ── Legacy daily run flow ─────────────────────────────────────────────────
    db.close()
    summary = asyncio.run(run_agent(dry_run=args.dry_run))
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"Raw postings: {summary['raw_count']}")
        print(f"New postings: {summary['new_count']}")
        print(f"After citizenship filter: {summary['after_citizenship']}")
        print(f"After Bay Area filter: {summary['after_location']}")
        print(f"After salary filter: {summary['after_salary']}")
        print(f"After role filter: {summary['after_role_filter']}")
        print(f"Unscored in DB (sent to stage1): {summary['unscored_count']}")
        print(f"All unalerted scored (ranked from): {summary['all_candidates_count']}")
        print(f"Ranked postings: {summary['ranked_count']}")
        print()
        print(summary["digest"])


if __name__ == "__main__":
    main()

