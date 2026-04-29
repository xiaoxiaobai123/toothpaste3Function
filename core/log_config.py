"""Logging configuration: rotating file handler with millisecond timestamps.

Format is grep-friendly:
    YYYY-MM-DD HH:MM:SS.mmm | LEVEL | message
Tags like [Cam1], [PLC], [LargeCircle] are inserted by callers, allowing
quick filtering of dual-camera concurrent logs.
"""

import logging
from logging.handlers import RotatingFileHandler

LOG_FILE = "my_app.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("toothpaste3function")
    logger.setLevel(logging.DEBUG)

    # Idempotent: avoid duplicate handlers when called from multiple modules.
    if not any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
        handler = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)
        formatter = logging.Formatter(
            fmt="%(asctime)s.%(msecs)03d | %(levelname)-5s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger
