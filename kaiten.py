"""
Публикация плана выручки карточкой в Kaiten.

Доска: 653513 «Задачи Ольги» (пространство 278576)
Колонка: 2355695 «Очередь»
Ответственный: 510026 Ольга Устюгова

Логика: если карточка с тем же заголовком уже есть на доске — обновляем описание,
иначе создаём новую.
"""

from __future__ import annotations

import logging
import os
from calendar import monthrange
from datetime import date
from typing import Any

import httpx

log = logging.getLogger("kaiten")

API_URL = os.environ.get("KAITEN_API_URL", "https://hulk.kaiten.ru/api/latest").rstrip("/")
API_TOKEN = os.environ.get("KAITEN_API_TOKEN", "")

BOARD_ID = int(os.environ.get("KAITEN_FORECAST_BOARD_ID", 653513))
COLUMN_ID = int(os.environ.get("KAITEN_FORECAST_COLUMN_ID", 2355695))
OWNER_ID = int(os.environ.get("KAITEN_FORECAST_OWNER_ID", 510026))

CARD_BASE_URL = "https://hulk.kaiten.ru"
DASHBOARD_URL = "https://dashboard.hulk.fit"

# Порядок вывода направлений в таблице плана
DEPT_ORDER = [
    "Фитнес-услуги",
    "Бар",
    "СПА-услуги",
    "Солярий",
    "Доп. услуги",
    "Товары",
]


def _fmt(v: float) -> str:
    """1657.4 → '1 657'"""
    return f"{round(v):,}".replace(",", " ")


def build_title(forecast: dict[str, Any]) -> str:
    return f"План выручки — {forecast['target_month_display']}"


def build_description(forecast: dict[str, Any], uplift_pct: float = 0.0) -> str:
    """Тело карточки: план по направлениям и отделам."""
    k = 1 + uplift_pct / 100.0

    rows: list[tuple[str, float]] = [("Клубные карты", forecast["cards_total_k"] * k)]

    depts: dict[str, float] = forecast.get("depts", {}) or {}
    seen = set()
    for name in DEPT_ORDER:
        if name in depts:
            rows.append((name, depts[name] * k))
            seen.add(name)
    for name, value in depts.items():
        if name not in seen:
            rows.append((name, value * k))

    total = sum(v for _, v in rows)

    lines = [
        f"**План на {forecast['target_month_display']}**",
        "",
        f"Итого: **{_fmt(total)} тыс. ₽**",
        "",
        "| Направление | План, тыс. ₽ |",
        "| --- | ---: |",
    ]
    for name, value in rows:
        lines.append(f"| {name} | {_fmt(value)} |")
    lines.append(f"| **Итого** | **{_fmt(total)}** |")

    lines += [
        "",
        "---",
        "",
        "**Из чего складываются клубные карты**",
        "",
        f"- Продления: {_fmt(forecast['comp_a']['revenue_k'] * k)} тыс. ₽ "
        f"({forecast['comp_a']['total_renewals']} чел. из {forecast['comp_a']['total_expiring']} истекающих, "
        f"retention {forecast['comp_a']['renewal_rate'] * 100:.1f}%)",
        f"- Реактивация: {_fmt(forecast['comp_b']['revenue_k'] * k)} тыс. ₽ "
        f"({forecast['comp_b']['total_returns']} чел.)",
        f"- Новые клиенты: {_fmt(forecast['comp_c']['revenue_k'] * k)} тыс. ₽",
        "",
    ]

    if uplift_pct:
        lines.append(
            f"> Цифры содержат надбавку **+{uplift_pct:g}%** к базовому прогнозу модели."
        )
        lines.append("")

    lines += [
        f"_Источник: [дашборд прогноза]({DASHBOARD_URL}/forecast/?month={forecast['target_month']})"
        f" · расчёт {forecast['computed_at']}_",
    ]

    return "\n".join(lines)


def _due_date(target_month: str) -> str:
    y, m = (int(x) for x in target_month.split("-"))
    last = monthrange(y, m)[1]
    return f"{date(y, m, last).isoformat()}T18:00:00.000Z"


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {API_TOKEN}",
        "Content-Type": "application/json",
    }


async def publish(forecast: dict[str, Any], uplift_pct: float = 0.0) -> dict[str, Any]:
    """Создаёт или обновляет карточку плана. Возвращает {ok, id, url, updated}."""
    if not API_TOKEN:
        return {"ok": False, "error": "KAITEN_API_TOKEN не задан в .env"}

    title = build_title(forecast)
    description = build_description(forecast, uplift_pct)

    async with httpx.AsyncClient(timeout=20.0) as client:
        # 1. Ищем существующую карточку с тем же заголовком на доске
        existing_id: int | None = None
        try:
            r = await client.get(
                f"{API_URL}/cards",
                headers=_headers(),
                params={"board_id": BOARD_ID, "condition": 1, "limit": 100},
            )
            r.raise_for_status()
            for card in r.json() or []:
                if (card.get("title") or "").strip() == title:
                    existing_id = card.get("id")
                    break
        except Exception as e:
            log.warning("Kaiten: поиск существующей карточки не удался: %s", e)

        payload: dict[str, Any] = {
            "title": title,
            "description": description,
            "owner_id": OWNER_ID,
            "due_date": _due_date(forecast["target_month"]),
        }

        try:
            if existing_id:
                r = await client.patch(
                    f"{API_URL}/cards/{existing_id}",
                    headers=_headers(),
                    json={"description": description, "due_date": payload["due_date"]},
                )
                r.raise_for_status()
                card_id = existing_id
                updated = True
            else:
                r = await client.post(
                    f"{API_URL}/cards",
                    headers=_headers(),
                    json={**payload, "board_id": BOARD_ID, "column_id": COLUMN_ID},
                )
                r.raise_for_status()
                card_id = (r.json() or {}).get("id")
                updated = False
        except httpx.HTTPStatusError as e:
            log.error("Kaiten API %s: %s", e.response.status_code, e.response.text[:400])
            return {"ok": False, "error": f"Kaiten API {e.response.status_code}: {e.response.text[:200]}"}
        except Exception as e:
            log.error("Kaiten API error: %s", e)
            return {"ok": False, "error": str(e)}

    return {
        "ok": True,
        "id": card_id,
        "url": f"{CARD_BASE_URL}/{card_id}",
        "updated": updated,
        "title": title,
    }
