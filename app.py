"""
Дашборд прогноза выручки Hulk Fit — FastAPI + Jinja2 SSR.

Маршруты:
  GET  /                  → прогноз на текущий или выбранный месяц
  GET  /forecast/         → то же (алиас для доступа через под-путь nginx)
  POST /api/kaiten-card   → создать/обновить карточку плана в Kaiten
  GET  /health            → проверка работоспособности

Параметры запроса:
  ?month=YYYY-MM → прогнозируемый месяц (по умолчанию — текущий)
"""

from __future__ import annotations
import asyncio
import logging
import os
from calendar import monthrange
from datetime import date
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

import queries
import calculator as calc
import ai_helpers as ai
import kaiten
import storage

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("app")

MCP_URL = os.environ.get("MCP_1C_URL", "https://mcp1c.hulk.fit/mcp/")
queries.set_mcp_url(MCP_URL)

app = FastAPI(title="Hulk Fit Forecast", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory="templates")


def _parse_month(month_str: str | None) -> date:
    today = date.today()
    if month_str:
        try:
            parts = month_str.split("-")
            y, m = int(parts[0]), int(parts[1])
            if 2020 <= y <= 2030 and 1 <= m <= 12:
                return date(y, m, 1)
        except Exception:
            pass
    return date(today.year, today.month, 1)


def _label(month_key: str) -> str:
    y, m = (int(x) for x in month_key.split("-"))
    return f"{calc.MONTH_NAMES_RU[m]} {y}"


def _month_options(current: date) -> list[dict]:
    """Архивные месяцы (есть снимок) + текущий + два вперёд."""
    today = date.today()
    keys: list[str] = []

    for key in storage.list_months():
        if storage.is_past(key, today):
            keys.append(key)

    for delta in range(3):
        m = today.month + delta
        y = today.year
        if m > 12:
            m -= 12; y += 1
        key = date(y, m, 1).strftime("%Y-%m")
        if key not in keys:
            keys.append(key)

    current_key = current.strftime("%Y-%m")
    if current_key not in keys:
        keys.append(current_key)

    keys.sort()
    return [
        {
            "value": k,
            "label": _label(k) + (" · архив" if storage.is_past(k, today) else ""),
            "selected": k == current_key,
        }
        for k in keys
    ]


# ── Факт по отделам за завершённый месяц ─────────────────────────────────────

async def _fetch_fact(month_key: str) -> dict[str, Any]:
    y, m = (int(x) for x in month_key.split("-"))
    start = date(y, m, 1)
    end = date(y, m, monthrange(y, m)[1])

    rows = await queries.get_sales_by_dept(start, end)

    cards = 0.0
    depts: dict[str, float] = {}
    for row in rows:
        rm = row.get("month")
        if not rm or rm.year != y or rm.month != m:
            continue
        dept = row.get("dept") or "—"
        value = (row.get("revenue") or 0) / 1000
        if "карт" in dept.lower():
            cards += value
        else:
            depts[dept] = depts.get(dept, 0.0) + value

    total = cards + sum(depts.values())
    return {
        "cards_total_k": round(cards, 1),
        "depts": {k: round(v, 1) for k, v in depts.items()},
        "total_k": round(total, 1),
    }


# ── Простой in-memory кэш (10 мин) ───────────────────────────────────────────
_cache: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 600  # секунд


def _cache_get(key: str) -> Any | None:
    import time
    if key in _cache:
        ts, val = _cache[key]
        if time.time() - ts < _CACHE_TTL:
            return val
        del _cache[key]
    return None


def _cache_set(key: str, val: Any) -> None:
    import time
    _cache[key] = (time.time(), val)


# ── Сборка контекста прогноза (используется страницей и API Kaiten) ──────────

async def _build_context(target_month: date, refresh: bool = False) -> dict[str, Any]:
    """Возвращает ctx для шаблона. Бросает исключение при отказе 1С."""
    cache_key = f"forecast:{target_month.strftime('%Y-%m')}"

    if not refresh:
        cached = _cache_get(cache_key)
        if cached:
            log.info("Cache hit: %s", cache_key)
            return cached
    else:
        log.info("Force refresh: %s", cache_key)
        _cache.pop(cache_key, None)

    log.info("Building forecast for %s", target_month)

    # Параллельные запросы к 1С (6 штук)
    base, churn_pool, sales, pt_rows, avg_check, pt_consumption = await queries.fetch_all(target_month)

    m = target_month.month
    month_name = calc.MONTH_NAMES_RU[m]

    # Предварительный расчёт для AI (без AI-коррекций)
    comp_a_prelim = calc.calc_component_a(base, m, avg_check.get("Продление") or None)
    comp_b_prelim = calc.calc_component_b(churn_pool, target_month)
    depts_prelim = calc.calc_other_depts(sales, target_month)
    pt_prelim = calc.calc_pt_buffer(pt_rows, consumption=pt_consumption)

    fitness_avg = depts_prelim.get("Фитнес-услуги", 0)
    fitness_prelim = calc.calc_fitness_seasonal(fitness_avg, m, pt_prelim["runway"])
    depts_prelim["Фитнес-услуги"] = fitness_prelim

    # Сезонный коэффициент на прочие отделы (согласовано с build_forecast)
    _s = calc.SEASON_COEFF_A[m]
    for _d in list(depts_prelim.keys()):
        if _d != "Фитнес-услуги":
            depts_prelim[_d] = round(depts_prelim[_d] * _s, 1)

    comp_c_k = calc.COMP_V_BASELINE[m]
    total_prelim = (
        comp_a_prelim["revenue_k"] + comp_b_prelim["revenue_k"] + comp_c_k
        + sum(depts_prelim.values())
    )

    # AI-анализ (синхронный, не блокирует основной поток надолго — ~3-5 сек)
    ai_results = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: ai.run_ai_analysis(
            avg_check=avg_check,
            comp_a_data={**comp_a_prelim, "base": base},
            comp_b_data=comp_b_prelim,
            comp_c_k=comp_c_k,
            depts=depts_prelim,
            pt=pt_prelim,
            month=m,
            month_name=month_name,
            total_k=total_prelim,
        )
    )

    # Финальный расчёт (с AI-коррекциями)
    forecast_data = calc.build_forecast(
        target_month=target_month,
        base=base,
        churn_pool=churn_pool,
        sales=sales,
        pt_rows=pt_rows,
        avg_check=avg_check,
        ai_results=ai_results,
        pt_consumption=pt_consumption,
    )

    month_key = target_month.strftime("%Y-%m")

    # Снимок: сохраняем первый успешный расчёт месяца, дальше не трогаем
    if forecast_data.get("other_total_k", 0) > 0:
        storage.save_if_absent(month_key, forecast_data)

    ctx = {
        "forecast": forecast_data,
        "month_options": _month_options(target_month),
        "current_month": month_key,
        "is_archive": False,
        "saved_at": None,
    }

    # Не кешируем если отделы пустые (Q3 упал, services=0)
    if forecast_data.get("other_total_k", 0) > 0:
        _cache_set(cache_key, ctx)

    return ctx


# ── Архивный контекст (из снимка, без обращения к 1С за прогнозом) ───────────

def _fact_rows(forecast: dict[str, Any], fact: dict[str, Any]) -> list[dict]:
    rows: list[dict] = []

    def add(name: str, plan: float, actual: float) -> None:
        delta = (actual - plan) / plan * 100 if plan else None
        rows.append({
            "name": name,
            "plan": round(plan, 1),
            "fact": round(actual, 1),
            "delta": round(delta, 1) if delta is not None else None,
        })

    add("Клубные карты", forecast.get("cards_total_k", 0), fact.get("cards_total_k", 0))

    plan_depts: dict[str, float] = forecast.get("depts", {}) or {}
    fact_depts: dict[str, float] = fact.get("depts", {}) or {}
    for name in list(plan_depts) + [d for d in fact_depts if d not in plan_depts]:
        add(name, plan_depts.get(name, 0), fact_depts.get(name, 0))

    add("Итого", forecast.get("total_k", 0), fact.get("total_k", 0))
    return rows


async def _archive_context(month_key: str) -> dict[str, Any] | None:
    data = storage.load(month_key)
    if not data:
        return None

    forecast = dict(data["forecast"])
    fact = data.get("fact")

    if not fact:
        try:
            fetched = await _fetch_fact(month_key)
            if fetched.get("total_k", 0) > 0:
                storage.save_fact(month_key, fetched)
                fact = fetched
        except Exception as e:
            log.warning("Факт за %s не получен: %s", month_key, e)

    if fact:
        forecast["fact_rows"] = _fact_rows(forecast, fact)
        forecast["fact_total_k"] = fact.get("total_k", 0)

    y, m = (int(x) for x in month_key.split("-"))
    return {
        "forecast": forecast,
        "month_options": _month_options(date(y, m, 1)),
        "current_month": month_key,
        "is_archive": True,
        "saved_at": data.get("saved_at"),
    }


# ── Основной маршрут ──────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def forecast_page(
    request: Request,
    month: str | None = Query(default=None, description="YYYY-MM"),
    refresh: int = Query(default=0, description="1 = сбросить кэш"),
):
    target_month = _parse_month(month)
    month_key = target_month.strftime("%Y-%m")

    # Прошлый месяц — только сохранённый снимок, пересчёта нет
    if storage.is_past(month_key):
        ctx = await _archive_context(month_key)
        if ctx is None:
            return HTMLResponse(
                content=_error_page(
                    f"Снимок прогноза за {_label(month_key)} не сохранён — "
                    f"этот месяц не рассчитывался, пока работало сохранение."
                ),
                status_code=404,
            )
        return templates.TemplateResponse("forecast.html", {"request": request, **ctx})

    try:
        ctx = await _build_context(target_month, refresh=bool(refresh))
    except Exception as e:
        log.error("1C data fetch failed: %s", e)
        return HTMLResponse(
            content=_error_page(f"Ошибка получения данных из 1С: {e}"),
            status_code=503,
        )

    # Список месяцев строим при рендере: кэш ctx не должен «замораживать» архив
    ctx = {**ctx, "month_options": _month_options(target_month)}

    return templates.TemplateResponse("forecast.html", {"request": request, **ctx})


@app.get("/forecast/", response_class=HTMLResponse)
async def forecast_page_prefixed(
    request: Request,
    month: str | None = Query(default=None, description="YYYY-MM"),
    refresh: int = Query(default=0, description="1 = сбросить кэш"),
):
    return await forecast_page(request, month, refresh)


# ── Публикация плана в Kaiten ────────────────────────────────────────────────

@app.get("/api/kaiten-card")
async def get_kaiten_card(month: str | None = Query(default=None, description="YYYY-MM")):
    """Есть ли уже карточка плана на этот месяц — для ссылки на дашборде."""
    target_month = _parse_month(month)
    result = await kaiten.find(target_month.strftime("%Y-%m"))
    return JSONResponse(result)


@app.post("/api/kaiten-card")
async def create_kaiten_card(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}

    target_month = _parse_month(body.get("month"))

    try:
        uplift = float(body.get("uplift_pct") or 0)
    except (TypeError, ValueError):
        uplift = 0.0
    uplift = max(-50.0, min(100.0, uplift))

    month_key = target_month.strftime("%Y-%m")

    if storage.is_past(month_key):
        ctx = await _archive_context(month_key)
        if ctx is None:
            return JSONResponse(
                {"ok": False, "error": f"Снимок за {_label(month_key)} не сохранён"},
                status_code=404,
            )
    else:
        try:
            ctx = await _build_context(target_month)
        except Exception as e:
            log.error("Kaiten card: 1C fetch failed: %s", e)
            return JSONResponse({"ok": False, "error": f"Нет данных из 1С: {e}"}, status_code=503)

    result = await kaiten.publish(ctx["forecast"], uplift_pct=uplift)
    return JSONResponse(result, status_code=200 if result.get("ok") else 502)


@app.get("/health")
async def health():
    return JSONResponse({"ok": True, "service": "hulkfit-forecast"})


def _error_page(msg: str) -> str:
    return f"""<!doctype html><html lang="ru" id="html-root"><head>
<meta charset="utf-8"><title>Ошибка — Hulk Fit Forecast</title>
<style>body{{background:#0a0a0a;color:#e8e8e8;font-family:sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh;}}
.card{{background:#111;border:1px solid #333;border-top:3px solid #f55;
border-radius:12px;padding:40px;max-width:560px;}}
h2{{color:#f55;margin-bottom:16px;}}pre{{color:#aaa;font-size:13px;}}
a{{color:#39FF14;}}</style></head>
<body><div class="card">
<h2>Ошибка загрузки данных</h2>
<pre>{msg}</pre>
<p style="margin-top:20px"><a href="/">Повторить</a></p>
</div></body></html>"""
