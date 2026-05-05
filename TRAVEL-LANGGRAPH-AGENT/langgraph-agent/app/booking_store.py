import json
import os
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Optional

from app.config import logger


STORE_PATH = Path(os.getenv("BOOKING_STORE_PATH", "/tmp/travel_bookings.json"))
_lock = Lock()


def _read_store() -> Dict[str, Dict[str, Any]]:
    if not STORE_PATH.exists():
        return {}
    try:
        return json.loads(STORE_PATH.read_text())
    except Exception as exc:
        logger.warning("Could not read booking store %s: %s", STORE_PATH, exc)
        return {}


def _write_store(data: Dict[str, Dict[str, Any]]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STORE_PATH.write_text(json.dumps(data, indent=2, sort_keys=True))


def save_booking(reference: str, state: Dict[str, Any]) -> None:
    if not reference:
        return
    with _lock:
        data = _read_store()
        data[reference.upper()] = state
        _write_store(data)


def get_booking(reference: str) -> Optional[Dict[str, Any]]:
    if not reference:
        return None
    with _lock:
        return _read_store().get(reference.upper())
