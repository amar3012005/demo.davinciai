import json
import logging
import time
from typing import Any, Dict


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def emit_event(logger: logging.Logger, event_name: str, *, level: int = logging.INFO, **fields: Dict[str, Any]) -> None:
    payload = {
        "event_name": event_name,
        "ts": time.time(),
        **fields,
    }
    logger.log(level, json.dumps(payload, ensure_ascii=True))
