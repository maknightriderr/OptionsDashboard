"""
Centralised logging configuration.

One call to ``configure_logging`` at process start wires up:
  * a console handler (human-readable), and
  * a rotating file handler under the configured log dir.

Module code should just do ``logger = logging.getLogger(__name__)`` and log
normally; it must not configure handlers itself.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

_CONFIGURED = False
_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"


def configure_logging(level: str = "INFO", log_dir: str = "logs") -> None:
    """Idempotently configure root logging. Safe to call more than once."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    Path(log_dir).mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)

    formatter = logging.Formatter(_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    # 10 MB per file, keep 10 backups -> ~100 MB ceiling for logs.
    file_handler = logging.handlers.RotatingFileHandler(
        Path(log_dir) / "option_terminal.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Quieten chatty third-party libraries.
    logging.getLogger("websocket").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    _CONFIGURED = True
