from __future__ import annotations

from job_logging import ensure_process_logging

ensure_process_logging(__file__)

import json
import logging
import math
import re
from collections import Counter
from typing import Any
import urllib.request

from config import SETTINGS
from models import JobPosting

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Query expansion: map internal Meta/Hitachi vocabulary → industry JD terms
# ---------------------------------------------------------------------------

_QUERY_EXPANSION: dict[str, str] = {
    "tupperware": "kubernetes containerized deployment container orchestration",
    "conveyor": "ci/cd automated rollouts deployment pipelines continuous delivery",
    "scuba": "metrics aggregation time-series observability platform monitoring",
    "onedetection": "alerting incident detection fleet alerting monitoring",
    "thrift": "rpc microservices service mesh grpc remote procedure call",
    "cbtc": "safety-critical control plane state machine interlocking",
    "ota": "over-the-air fleet management automated deployment",
    "acl": "access control list authorization enforcement policy",
    "hil": "hardware in the loop testing simulation validation",
    "rtos": "real-time operating system embedded linux systems programming",
}


def _expand_resume(resume_text: str) -> str:
    """Append industry-standard equivalents for internal terminology before embedding."""
    text_lower = resume_text.lower()
    expansions = [exp for term, exp in _QUERY_EXPANSION.items() if term in text_lower]
    if not expansions:
        return resume_text
    return resume_text + "\n\nSKILL TRANSLATIONS: " + ". ".join(expansions)


# ---------------------------------------------------------------------------
# Skill overlap — 6 groups; score = matched_groups / 6
# ---------------------------------------------------------------------------

_SKILL_GROUPS: dict[str, list[str]] = {
    "languages": ["\\bc\\b", "c\\+\\+", "cpp", "rust", "python", "\\bgo\\b", "golang"],
    "systems": ["embedded linux", "\\blinux\\b", "\\brtos\\b", "\\bunix\\b", "systems programming", "\\bkernel\\b"],
    "distributed": [
        "distributed systems", "kubernetes", "\\bk8s\\b", "\\bdocker\\b", "microservices",
        "\\brpc\\b", "\\bgrpc\\b", "\\bthrift\\b", "\\bkafka\\b", "\\betcd\\b", "service mesh",
    ],
    "reliability": [
        "\\bsre\\b", "site reliability", "observability", "\\bmetrics\\b", "\\balerting\\b",
        "\\bmonitoring\\b", "high availability", "on-call", "oncall", "incident response",
    ],
    "security": [
        "authorization", "authentication", "access control", "\\bacl\\b", "security enforcement",
        "policy enforcement", "zero trust", "\\biam\\b",
    ],
    "scale": [
        "large.scale", "hyperscale", "millions of", "\\bfleet\\b", "\\bproduction\\b", "infrastructure",
    ],
}

# Pre-compile per group
_SKILL_PATTERNS: dict[str, re.Pattern] = {
    group: re.compile("|".join(kws), re.IGNORECASE)
    for group, kws in _SKILL_GROUPS.items()
}


def _skill_overlap(jd_text: str) -> float:
    """Fraction of skill groups (0–6) present in the JD."""
    matched = sum(1 for pat in _SKILL_PATTERNS.values() if pat.search(jd_text))
    return matched / len(_SKILL_GROUPS)


# ---------------------------------------------------------------------------
# Domain score — how well does the JD domain match target work areas
# ---------------------------------------------------------------------------

def _compile_domain_patterns(tiers: dict[str, list[str]]) -> tuple[re.Pattern, re.Pattern, re.Pattern]:
    def _pat(terms: list[str]) -> re.Pattern:
        return re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE)
    return _pat(tiers.get("tier1", [])), _pat(tiers.get("tier2", [])), _pat(tiers.get("tier3", []))


# Defaults — overridden when a profile passes domain_tiers to stage1_select
_DEFAULT_DOMAIN_TIERS: dict[str, list[str]] = {
    "tier1": [
        "observability platform", "security enforcement", "authorization system",
        "safety.critical", "fleet management", "distributed systems", "control plane",
        "production engineering", "platform infrastructure", "site reliability",
        "security observability",
    ],
    "tier2": [
        "infrastructure", "developer tooling", "backend systems", "cloud infrastructure",
        "devops", "platform engineering", "developer productivity",
    ],
    "tier3": ["machine learning infrastructure", "mlops", "ml platform", "ai infrastructure"],
}

_DOMAIN_TIER1, _DOMAIN_TIER2, _DOMAIN_TIER3 = _compile_domain_patterns(_DEFAULT_DOMAIN_TIERS)


def _domain_score(jd_text: str, tier1: re.Pattern = _DOMAIN_TIER1, tier2: re.Pattern = _DOMAIN_TIER2, tier3: re.Pattern = _DOMAIN_TIER3) -> float:
    if tier1.search(jd_text):
        return 1.0
    if tier2.search(jd_text):
        return 0.7
    if tier3.search(jd_text):
        return 0.4
    return 0.1


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------

def _freshness_score(age_days: int) -> float:
    if age_days <= 3:
        return 1.0
    if age_days <= 7:
        return 0.7
    if age_days <= 14:
        return 0.5
    return 0.2


# ---------------------------------------------------------------------------
# Role multiplier
# ---------------------------------------------------------------------------

_ROLE_ZERO = re.compile(
    r"\b(recruiter|recruiting|talent acquisition|people ops|human resources|"
    r"\bhr\b|finance|accounting|legal counsel|marketing manager|sales manager|"
    r"account executive|customer success)\b",
    re.IGNORECASE,
)
_ROLE_PM = re.compile(
    r"\b(product manager|program manager|project manager|technical program manager|"
    r"\btpm\b)\b",
    re.IGNORECASE,
)
_ROLE_EM = re.compile(
    r"\b(engineering manager|manager of engineering|director of engineering|"
    r"vp of engineering|head of engineering)\b",
    re.IGNORECASE,
)
_ROLE_TL = re.compile(r"\btech(nical)? lead\b", re.IGNORECASE)
_ROLE_MANAGER_WORD = re.compile(r"\bmanager\b", re.IGNORECASE)


def _role_multiplier(title: str) -> float:
    if _ROLE_ZERO.search(title):
        return 0.0
    if _ROLE_PM.search(title):
        return 0.1
    if _ROLE_EM.search(title):
        return 0.6
    if _ROLE_TL.search(title) and not _ROLE_MANAGER_WORD.search(title):
        return 0.9
    return 1.0


# ---------------------------------------------------------------------------
# Anti-pattern penalty
# ---------------------------------------------------------------------------

_PHD_RE = re.compile(r"\bphd required\b|\bphd or equivalent\b|\bdoctorate required\b", re.IGNORECASE)
_ML_SIGNAL_RE = re.compile(
    r"\b(machine learning|deep learning|large language model|llm|neural network|"
    r"\bnlp\b|computer vision|pytorch|tensorflow|hugging face|transformers)\b",
    re.IGNORECASE,
)
_INFRA_SIGNAL_RE = re.compile(
    r"\b(infrastructure|reliability|observability|security|distributed|platform|"
    r"sre|production engineering)\b",
    re.IGNORECASE,
)
_FIRMWARE_RE = re.compile(
    r"\b(firmware|fpga|bootloader|bsp|bare metal microcontroller|device driver|"
    r"kernel module|yocto|buildroot|autosar|\\bmcu\\b|\\bhal\\b)\b",
    re.IGNORECASE,
)
_MGMT_LANG_RE = re.compile(
    r"manage a team|manage teams|direct reports|people management|"
    r"build and lead a team|organizational leadership",
    re.IGNORECASE,
)


def _anti_pattern_penalty(description: str) -> float:
    penalty = 0.0
    if _PHD_RE.search(description):
        penalty += 0.10
    ml_count = len(_ML_SIGNAL_RE.findall(description))
    infra_count = len(_INFRA_SIGNAL_RE.findall(description))
    if ml_count >= 3 and infra_count < 2:
        penalty += 0.15
    if _FIRMWARE_RE.search(description):
        penalty += 0.10
    if _MGMT_LANG_RE.search(description):
        penalty += 0.10
    return min(penalty, 0.25)


# ---------------------------------------------------------------------------
# Tokenisation / TF-IDF fallback
# ---------------------------------------------------------------------------

def tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def tfidf_similarity(a: str, b: str) -> float:
    """Deterministic lexical fallback when semantic embedding providers are unavailable."""
    docs = [tokenize(a), tokenize(b)]
    if not docs[0] or not docs[1]:
        return 0.0

    df: Counter[str] = Counter()
    for doc in docs:
        for token in set(doc):
            df[token] += 1

    vectors: list[dict[str, float]] = []
    n_docs = len(docs)
    for doc in docs:
        tf = Counter(doc)
        vec: dict[str, float] = {}
        for token, freq in tf.items():
            idf = math.log((1 + n_docs) / (1 + df[token])) + 1
            vec[token] = freq * idf
        vectors.append(vec)

    keys = set(vectors[0]) | set(vectors[1])
    dot = sum(vectors[0].get(k, 0.0) * vectors[1].get(k, 0.0) for k in keys)
    mag_a = math.sqrt(sum(v * v for v in vectors[0].values()))
    mag_b = math.sqrt(sum(v * v for v in vectors[1].values()))
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)


# ---------------------------------------------------------------------------
# Embedding providers
# ---------------------------------------------------------------------------

def _cosine(vec_a: list[float], vec_b: list[float]) -> float:
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    mag_a = math.sqrt(sum(a * a for a in vec_a))
    mag_b = math.sqrt(sum(b * b for b in vec_b))
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)


def _embed_with_sentence_transformers(texts: list[str]) -> list[list[float]] | None:
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except Exception as exc:
        LOGGER.info("sentence-transformers unavailable, falling back: %s", exc)
        return None

    try:
        model = SentenceTransformer("all-MiniLM-L6-v2")
        vectors = model.encode(texts, normalize_embeddings=True)
        return [list(map(float, row)) for row in vectors]
    except Exception as exc:
        LOGGER.warning("sentence-transformers model load/encode failed, falling back: %s", exc)
        return None


def _embed_with_voyage(texts: list[str]) -> list[list[float]] | None:
    if not SETTINGS.voyage_api_key:
        return None
    body = {
        "model": SETTINGS.voyage_embed_model,
        "input": texts,
    }
    req = urllib.request.Request("https://api.voyageai.com/v1/embeddings", method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {SETTINGS.voyage_api_key}")
    payload = json.dumps(body).encode("utf-8")

    try:
        with urllib.request.urlopen(req, data=payload, timeout=45) as resp:
            raw = json.loads(resp.read().decode("utf-8", errors="ignore"))
    except Exception as exc:
        LOGGER.warning("Voyage embedding call failed, falling back: %s", exc)
        return None

    data = raw.get("data")
    if not isinstance(data, list):
        return None
    vectors: list[list[float]] = []
    for row in data:
        emb = row.get("embedding")
        if not isinstance(emb, list):
            return None
        vectors.append([float(x) for x in emb])
    return vectors


def _semantic_vectors(texts: list[str]) -> list[list[float]] | None:
    mode = SETTINGS.stage1_embedder.lower().strip()
    if mode in {"auto", "sentence-transformers", "sentence_transformers", "st"}:
        vectors = _embed_with_sentence_transformers(texts)
        if vectors:
            return vectors
    if mode in {"auto", "voyage"}:
        vectors = _embed_with_voyage(texts)
        if vectors:
            return vectors
    return None


def _score_similarity(
    expanded_resume: str,
    posting: JobPosting,
    embedding_map: dict[str, list[float]] | None,
) -> float:
    if embedding_map and "__resume__" in embedding_map and posting.id in embedding_map:
        return _cosine(embedding_map["__resume__"], embedding_map[posting.id])
    return tfidf_similarity(expanded_resume, posting.description)


# ---------------------------------------------------------------------------
# Stage 1 selection — multi-signal scorer
# ---------------------------------------------------------------------------

def stage1_select(postings: list[JobPosting], resume_text: str, top_n: int = 30, domain_tiers: dict | None = None) -> list[JobPosting]:
    """Stage 1 selection using multi-signal scoring.

    score = max(0, base × role_multiplier − anti_pattern_penalty)
    base  = embedding_sim×0.45 + skill_overlap×0.25 + domain_score×0.20 + freshness×0.10
    """
    expanded_resume = _expand_resume(resume_text)

    t1, t2, t3 = _compile_domain_patterns(domain_tiers) if domain_tiers else (_DOMAIN_TIER1, _DOMAIN_TIER2, _DOMAIN_TIER3)

    embedding_map: dict[str, list[float]] | None = None
    texts = [expanded_resume] + [p.description for p in postings]
    vectors = _semantic_vectors(texts)
    if vectors and len(vectors) == len(texts):
        embedding_map = {"__resume__": vectors[0]}
        for idx, posting in enumerate(postings, start=1):
            embedding_map[posting.id] = vectors[idx]

    for posting in postings:
        embed_sim  = _score_similarity(expanded_resume, posting, embedding_map)
        skill_ov   = _skill_overlap(posting.description)
        domain_sc  = _domain_score(posting.description, t1, t2, t3)
        fresh      = _freshness_score(posting.age_days)

        base_score = (
            embed_sim * 0.45
            + skill_ov  * 0.25
            + domain_sc * 0.20
            + fresh     * 0.10
        )

        role_mult = _role_multiplier(posting.title)
        penalty   = _anti_pattern_penalty(posting.description)

        # Keep penalty_multiplier field for downstream consumers (embedded-heavy flag)
        has_firmware = bool(re.search(r"\b(firmware|fpga)\b", posting.description, re.IGNORECASE))
        posting.penalty_multiplier = 0.4 if has_firmware else 1.0
        posting.embedded_flag = has_firmware
        if posting.embedded_flag:
            posting.reason_codes.append("DOWNRANK_EMBEDDED_HEAVY")

        posting.embed_score  = embed_sim
        posting.stage1_score = max(0.0, base_score * role_mult - penalty)

        if role_mult == 0.0:
            posting.reason_codes.append("ROLE_MULTIPLIER_ZERO")
        elif role_mult < 0.5:
            posting.reason_codes.append("ROLE_MULTIPLIER_LOW")

    return sorted(postings, key=lambda p: p.stage1_score or 0.0, reverse=True)[:top_n]
