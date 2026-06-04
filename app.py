"""
Дашборд прогноза выручки Hulk Fit — FastAPI + Jinja2 SSR.

Маршруты:
  GET /          → прогноз на текущий или выбранный месяц
  GET /health    → проверка работоспособности

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


def _month_options(current: date) -> list[dict]:
    options = []
    for delta in range(3):
        m = current.month + delta
        y = current.year
        if m > 12:
            m -= 12; y += 1
        d = date(y, m, 1)
        options.append({
            "value": d.strftime("%Y-%m"),
            "label": f"{calc.MONTH_NAMES_RU[d.month]} {d.year}",
            "selected": delta == 0,
        })
    return options


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


# ── Основной маршрут ──────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def forecast_page(
    request: Request,
    month: str | None = Query(default=None, description="YYYY-MM"),
    refresh: int = Query(default=0, description="1 = сбросить кэш"),
):
    target_month = _parse_month(month)
    cache_key = f"forecast:{target_month.strftime('%Y-%m')}"

    if not refresh:
        cached = _cache_get(cache_key)
        if cached:
            log.info("Cache hit: %s", cache_key)
            return templates.TemplateResponse(
                "forecast.html",
                {"request": request, **cached},
            )
    else:
        log.info("Force refresh: %s", cache_key)
        _cache.pop(cache_key, None)

    log.info("Building forecast for %s", target_month)

    try:
        # Параллельные запросы к 1С (6 штук)
        base, churn_pool, sales, pt_rows, avg_check, pt_consumption = await queries.fetch_all(target_month)
    except Exception as e:
        log.error("1C data fetch failed: %s", e)
        return HTMLResponse(
            content=_error_page(f"Ошибка получения данных из 1С: {e}"),
            status_code=503,
        )

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

    ctx = {
        "forecast": forecast_data,
        "month_options": _month_options(target_month),
        "current_month": target_month.strftime("%Y-%m"),
    }

    # Не кешируем если отделы пустые (Q3 упал, services=0)
    if forecast_data.get("other_total_k", 0) > 0:
        _cache_set(cache_key, ctx)

    return templates.TemplateResponse("forecast.html", {"request": request, **ctx})


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
