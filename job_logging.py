from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

_ENV_LOG_FILE = "JOB_AGENT_LOG_FILE"


def _find_project_root(start_file: str) -> Path:
    p = Path(start_file).resolve().parent
    for candidate in [p, *p.parents]:
        if (candidate / "config.py").exists() and (candidate / "main.py").exists():
            return candidate
    return p


def ensure_process_logging(start_file: str) -> str:
    root_logger = logging.getLogger()
    existing = os.getenv(_ENV_LOG_FILE, "").strip()
    if existing:
        return existing

    project_root = _find_project_root(start_file)
    log_dir = project_root / "state" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = log_dir / f"job-agent-{ts}.log"

    handlers: list[logging.Handler] = []
    has_stream = any(isinstance(h, logging.StreamHandler) for h in root_logger.handlers)
    if not has_stream:
        handlers.append(logging.StreamHandler())
    handlers.append(logging.FileHandler(log_path, encoding="utf-8"))

    if handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            handlers=handlers,
            force=False,
        )

    os.environ[_ENV_LOG_FILE] = str(log_path)
    return str(log_path)
