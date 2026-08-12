"""
Хранилище снимков прогноза.

Снимок = результат первого успешного расчёта месяца. После сохранения не меняется:
прошлые месяцы показываются из файла, 1С не опрашивается.

Файлы: /app/data/snapshots/YYYY-MM.json  (том /opt/hulkfit-forecast/data на хосте)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("storage")

DATA_DIR = Path(os.environ.get("FORECAST_DATA_DIR", "/app/data")) / "snapshots"


def _ensure_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _path(month_key: str) -> Path:
    return DATA_DIR / f"{month_key}.json"


def exists(month_key: str) -> bool:
    return _path(month_key).is_file()


def save_if_absent(month_key: str, forecast: dict[str, Any]) -> bool:
    """Сохраняет снимок, если его ещё нет. True — если записали."""
    if exists(month_key):
        return False
    try:
        _ensure_dir()
        payload = {
            "month": month_key,
            "saved_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
            "forecast": forecast,
        }
        tmp = _path(month_key).with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_path(month_key))
        log.info("Snapshot saved: %s", month_key)
        return True
    except Exception as e:
        log.error("Snapshot save failed for %s: %s", month_key, e)
        return False


def load(month_key: str) -> dict[str, Any] | None:
    """Возвращает {month, saved_at, forecast} или None."""
    p = _path(month_key)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.error("Snapshot read failed for %s: %s", month_key, e)
        return None


def list_months() -> list[str]:
    """Все сохранённые месяцы, по возрастанию."""
    if not DATA_DIR.is_dir():
        return []
    return sorted(p.stem for p in DATA_DIR.glob("*.json"))


def save_fact(month_key: str, fact: dict[str, Any]) -> None:
    """Дописывает факт в уже сохранённый снимок (прогноз не трогает)."""
    data = load(month_key)
    if not data:
        return
    data["fact"] = fact
    try:
        tmp = _path(month_key).with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_path(month_key))
        log.info("Fact saved: %s", month_key)
    except Exception as e:
        log.error("Fact save failed for %s: %s", month_key, e)


def is_past(month_key: str, today: date | None = None) -> bool:
    today = today or date.today()
    y, m = (int(x) for x in month_key.split("-"))
    return (y, m) < (today.year, today.month)
