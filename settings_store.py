"""
Настройки отборов прогноза: сегменты номенклатуры 1С и подразделения.

Хранение — JSON в FORECAST_DATA_DIR/settings (bind-каталог сервера, бэкап daily),
журнал смен — JSONL рядом. Нет файла или он битый — действуют DEFAULTS, и ответ
говорит об этом явно (source="default"). Сегменты хранятся по наименованию:
execute_1c_query не умеет передать ссылку по UUID (проверено 03.11.2026),
поэтому дубли имён в 1С отсекаются при сохранении и перед каждым расчётом.
По образцу backend/settings_store.py дашборда операционки.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

# Решение Павла 04.11.2026 (рекомендованные сегменты, разбор fitclub-1c-expert)
DEFAULTS: dict[str, Any] = {
    "cards": ["Все КК (кроме разовых и детских)"],   # база продлений, отток, средний чек
    "pt": ["Персональные тренировки в ТЗ"],          # буфер и расход ПТ
    "dept_cards": "Клубные карты",                   # подразделение клубных карт (Q3)
    "dept_fitness": "Фитнес-услуги",                 # подразделение с особой формулой (Q3)
}
LIST_KINDS = ("cards", "pt")
DEPT_KINDS = ("dept_cards", "dept_fitness")
KINDS = LIST_KINDS + DEPT_KINDS

LABELS = {
    "cards": "Клубные карты: продления, отток, средний чек",
    "pt": "Персональные тренировки: буфер и расход сессий",
    "dept_cards": "Подразделение клубных карт",
    "dept_fitness": "Подразделение фитнес-услуг",
}

_TZ = timezone(timedelta(hours=5))   # Екатеринбург, время клуба


def _dir() -> Path:
    return Path(os.environ.get("FORECAST_DATA_DIR", "/app/data")) / "settings"


def _file() -> Path:
    return _dir() / "settings.json"


def _log_file() -> Path:
    return _dir() / "settings-log.jsonl"


def _clean(data: dict) -> dict:
    """Проверяет и нормализует значения; бросает ValueError при неверном формате."""
    out: dict[str, Any] = {}
    for k in LIST_KINDS:
        names = data.get(k)
        if not isinstance(names, list):
            raise ValueError(f"{k}: ожидается список")
        names = sorted({str(n) for n in names if str(n).strip()})
        if not names:
            raise ValueError(f"{k}: пустой список")
        out[k] = names
    for k in DEPT_KINDS:
        name = data.get(k)
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{k}: пусто")
        out[k] = name
    if out["dept_cards"] == out["dept_fitness"]:
        raise ValueError("подразделения карт и фитнес-услуг совпадают")
    return out


def load() -> dict:
    """Текущие настройки + source ("file" | "default")."""
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
        out = _clean(data)
        out.update(source="file", updated_by=data.get("updated_by"), updated_at=data.get("updated_at"))
        return out
    except Exception:
        # Нет файла или он битый — дашборд не падает, ответ покажет source=default
        out = _clean(DEFAULTS)
        out.update(source="default", updated_by=None, updated_at=None)
        return out


def fingerprint(cfg: dict) -> str:
    """Короткий хеш отборов — часть ключа кэша прогноза."""
    raw = json.dumps({k: cfg[k] for k in KINDS}, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(raw.encode()).hexdigest()[:10]


def segment_names(cfg: dict) -> list[str]:
    return sorted({n for k in LIST_KINDS for n in cfg[k]})


def save(new: dict, user: str, ip: Optional[str]) -> dict:
    """Атомарно записывает настройки и добавляет строку в журнал (межпроцессный замок)."""
    clean = _clean(new)
    now = datetime.now(_TZ).isoformat(timespec="seconds")
    _dir().mkdir(parents=True, exist_ok=True)
    with open(_dir() / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = load()
        data = {**clean, "updated_by": user, "updated_at": now}
        fd, tmp = tempfile.mkstemp(dir=_dir(), prefix=".settings-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, _file())
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        entry = {
            "at": now, "user": user, "ip": ip,
            "before": {k: old[k] for k in KINDS},
            "after": {k: clean[k] for k in KINDS},
        }
        with open(_log_file(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
    return load()


def read_log(limit: int = 50) -> list[dict]:
    try:
        lines = _log_file().read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in reversed(lines[-limit:]):
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out
