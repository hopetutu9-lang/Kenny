"""Secret-conscious structured logging and audit helpers."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from config import LOG_PATH

_SENSITIVE_PATTERNS = (
    re.compile(r"(?i)(api[_ -]?key|api[_ -]?secret|bot[_ -]?token)\s*[:=]\s*[^,\s]+"),
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"\b[A-Za-z0-9]{32,}\b"),
)


def redact(value: object) -> str:
    text = str(value)
    for pattern in _SENSITIVE_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            rendered = "[unrenderable log message]"
        record.msg = redact(rendered)
        record.args = ()
        if record.exc_info:
            # Avoid serializing provider exceptions that may include request data.
            record.exc_info = None
            record.exc_text = None
        return True


def get_logger(name: str = "binance_telegram_bot") -> logging.Logger:
    logger = logging.getLogger(name)
    if getattr(logger, "_configured", False):
        return logger
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    stream.addFilter(RedactionFilter())
    logger.addHandler(stream)
    try:
        Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            LOG_PATH, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(RedactionFilter())
        logger.addHandler(file_handler)
    except OSError:
        logger.warning("Persistent log file unavailable; using console logging only")
    logger.propagate = False
    logger._configured = True  # type: ignore[attr-defined]
    return logger


def safe_error_name(error: BaseException) -> str:
    """Return an exception type only; messages can contain provider data."""
    return type(error).__name__


def safe_event_text(name: str, fields: dict[str, Any] | None = None) -> str:
    details = " ".join(
        f"{redact(key)}={redact(value)}"
        for key, value in (fields or {}).items()
        if key not in {"api_key", "api_secret", "bot_token", "secret", "token"}
    )
    return f"{redact(name)} {details}".strip()


logger = get_logger()

