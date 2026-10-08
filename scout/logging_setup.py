"""Logging: rotating file handler plus stderr, so cron output and the log agree."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)-7s %(name)-14s %(message)s"
DATEFMT = "%Y-%m-%dT%H:%M:%S%z"


def setup_logging(
    log_path: Path,
    level: str = "INFO",
    max_bytes: int = 5_000_000,
    backup_count: int = 5,
    *,
    verbose: bool = False,
) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else getattr(logging, level.upper(), logging.INFO))

    # Idempotent: repeated calls (tests, eval script) must not duplicate handlers.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(FORMAT, datefmt=DATEFMT)

    file_handler = RotatingFileHandler(
        log_path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    # The SDK and urllib3 are chatty at INFO; keep them at WARNING unless -v.
    for noisy in ("anthropic", "httpx", "httpx2", "urllib3", "arxiv"):
        logging.getLogger(noisy).setLevel(logging.DEBUG if verbose else logging.WARNING)

    return logging.getLogger("scout")
