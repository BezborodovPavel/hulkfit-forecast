"""Расчётная модель прогноза выручки Hulk Fit."""

from __future__ import annotations
import datetime
import statistics
from datetime import date
from typing import Any

# ── Константы модели ──────────────────────────────────────────────────────────

RETENTION_H0 = {"А": 0.533, "В": 0.223, "С": 0.068, "Никогда": 0.059}

SEASON_COEFF_A = {
    1: 1.10, 2: 0.94, 3: 1.13, 4: 0.96,
    5: 0.90, 6: 0.92, 7: 0.94, 8: 1.03,
    9: 0.94, 10: 1.02, 11: 1.14, 12: 1.08,
}

AVG_RENEWAL_PRICE = {"А": 12055, "В": 11884, "С": 12055, "Никогда": 7817}
AVG_NEW_PRICE = 8933
AVG_REACTIVATION_PRICE = 8933

REACTIVATION_RATE = {
    "1m":    {1:12.1, 2:10.4, 3:8.4, 4:8.6, 5:4.8, 6:4.5, 7:6.8, 8:12.4, 9:6.6, 10:10.1, 11:10.3, 12:10.5},
    "2m":    {1:3.5,  2:8.0,  3:6.9, 4:6.7, 5:7.0, 6:4.5, 7:5.5, 8:6.5,  9:8.6, 10:4.7,  11:7.7,  12:6.1},
    "3m":    {1:3.8,  2:5.7,  3:7.0, 4:4.9, 5:1.8, 6:2.5, 7:3.7, 8:4.2,  9:5.1, 10:5.3,  11:4.2,  12:6.6},
    "4-6m":  {1:2.5,  2:4.7,  3:3.1, 4:2.8, 5:2.9, 6:1.3, 7:2.8, 8:4.4,  9:4.0, 10:5.7,  11:4.9,  12:3.9},
    "7-12m": {1:2.5,  2:1.9,  3:1.8, 4:1.0, 5:1.4, 6:1.7, 7:2.5, 8:1.4,  9:2.0, 10:2.1,  11:2.3,  12:2.9},
    "13-24m":{1:1.4,  2:1.0,  3:1.1, 4:0.7, 5:0.7, 6:0.5, 7:0.7, 8:0.7,  9:1.0, 10:1.2,  11:1.5,  12:1.0},
    "25m+":  {1:0.5,  2:0.5,  3:0.3, 4:0.7, 5:0.2, 6:0.4, 7:0.2, 8:0.3,  9:0.4, 10:0.4,  11:0.4,  12:0.6},
}

COMP_V_BASELINE = {
    1:550, 2:600, 3:580, 4:570, 5:520, 6:430,
    7:350, 8:350, 9:400, 10:480, 11:500, 12:400,
}

RENEWAL_RATE_LOW = 0.28
RENEWAL_RATE_HIGH = 0.35
PT_BUFFER_RUNWAY_THRESHOLD = 1.5
PT_CONSUMPTION_PER_MONTH = 1703  # fallback; перезаписывается из Q6
AVG_CHECK_DEVIATION_WARN = 0.05
AVG_CHECK_DEVIATION_AI = 0.10

EXCLUDED_PERIODS = {"2025-03"}
ANOMALY_PERIODS_COMP_V = {
    "2025-04", "2025-05", "2025-06", "2025-07", "2025-08",
    "2025-09", "2025-10", "2025-11", "2025-12",
}

PT_SEGMENTS = {
    "Персональная тренировка в тренажерном зале",
    "Тренировка Дуэт в тренажерном зале",
    "Тренировка Трио в тренажерном зале",
    "Платная группа",
}

MONTH_NAMES_RU = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}
MONTH_SHORT_RU = {
    1: "Янв", 2: "Фев", 3: "Мар", 4: "Апр",
    5: "Май", 6: "Июн", 7: "Июл", 8: "Авг",
    9: "Сен", 10: "Окт", 11: "Ноя", 12: "Дек",
}


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _month_key(d: date) -> str:
    return d.strftime("%Y-%m")


def _month_add(d: date, months: int) -> date:
    m = d.month + months
    y = d.year
    while m <= 0:
        m += 12; y -= 1
    while m > 12:
        m -= 12; y += 1
    return date(y, m, 1)


def _months_diff(earlier: date, later: date) -> int:
    return (later.year - earlier.year) * 12 + (later.month - earlier.month)


# ── Компонент А: продления ────────────────────────────────────────────────────

def calc_component_a(
    base: dict[str, int],
    month: int,
    avg_check_renewal: float | None = None,
) -> dict[str, Any]:
    """
    Рассчитывает выручку от продлений карт.
    base: {"А": N, "В": N, "С": N, "Никогда": N}
    """
    s = SEASON_COEFF_A[month]

    ret = {
        "А": RETENTION_H0["А"] * s,
        "В": RETENTION_H0["В"] * (0.5 + 0.5 * s),
        "С": 0.12 if month == 1 else RETENTION_H0["С"],
        "Никогда": RETENTION_H0["Никогда"],
    }

    # Актуальный средний чек
    chk = avg_check_renewal if avg_check_renewal else AVG_RENEWAL_PRICE["А"]
    chk_b = AVG_RENEWAL_PRICE["В"] / AVG_RENEWAL_PRICE["А"] * chk
    chk_c = chk  # С платит так же, как А
    chk_d = AVG_RENEWAL_PRICE["Никогда"]

    renewals_by_cat = {
        "А": base["А"] * ret["А"],
        "В": base["В"] * ret["В"],
        "С": base["С"] * ret["С"],
        "Никогда": base["Никогда"] * ret["Никогда"],
    }
    total_renewals = sum(renewals_by_cat.values())
    total_expiring = sum(base.values())
    renewal_rate = total_renewals / total_expiring if total_expiring > 0 else 0.0

    revenue = (
        renewals_by_cat["А"] * chk +
        renewals_by_cat["В"] * chk_b +
        renewals_by_cat["С"] * chk_c +
        renewals_by_cat["Никогда"] * chk_d
    ) / 1000  # → тыс. руб.

    return {
        "base": base,
        "total_expiring": total_expiring,
        "ret": ret,
        "renewals_by_cat": {k: round(v) for k, v in renewals_by_cat.items()},
        "total_renewals": round(total_renewals),
        "renewal_rate": renewal_rate,
        "avg_check": round(chk),
        "revenue_k": round(revenue, 1),
    }


# ── Компонент Б: реактивация ──────────────────────────────────────────────────

def distribute_to_buckets(churn_pool: list[dict], target_month: date) -> dict[str, int]:
    """Распределяет ушедших клиентов по временным бакетам."""
    pools: dict[str, int] = {
        "1m": 0, "2m": 0, "3m": 0, "4-6m": 0, "7-12m": 0, "13-24m": 0, "25m+": 0
    }
    for row in churn_pool:
        churn_date: date = row["month"]
        months_ago = _months_diff(churn_date, target_month)
        count: int = row["count"]
        if months_ago == 1:
            pools["1m"] += count
        elif months_ago == 2:
            pools["2m"] += count
        elif months_ago == 3:
            pools["3m"] += count
        elif 4 <= months_ago <= 6:
            pools["4-6m"] += count
        elif 7 <= months_ago <= 12:
            pools["7-12m"] += count
        elif 13 <= months_ago <= 24:
            pools["13-24m"] += count
        elif months_ago >= 25:
            pools["25m+"] += count
    return pools


def calc_component_b(
    churn_pool: list[dict],
    target_month: date,
    pool_13_24_coeff: float = 1.0,
) -> dict[str, Any]:
    """Рассчитывает выручку от реактивации."""
    m = target_month.month
    pools = distribute_to_buckets(churn_pool, target_month)

    # Среднемесячный отток за последние 12 месяцев (для оценки аномалии)
    excluded = EXCLUDED_PERIODS | ANOMALY_PERIODS_COMP_V
    recent_counts = [
        row["count"] for row in churn_pool
        if _months_diff(row["month"], target_month) <= 12
        and _month_key(row["month"]) not in excluded
    ]
    avg_monthly_churn = statistics.mean(recent_counts) if recent_counts else 1

    # Проверка аномалии пула 13-24m (порог: >5× среднемесячного оттока)
    pool_anomaly = False
    if pools["13-24m"] > 5 * avg_monthly_churn:
        pool_anomaly = True
        pools["13-24m"] = int(pools["13-24m"] * pool_13_24_coeff)

    returns_by_bucket: dict[str, float] = {}
    for bucket, size in pools.items():
        rate = REACTIVATION_RATE[bucket][m] / 100
        returns_by_bucket[bucket] = size * rate

    total_returns = sum(returns_by_bucket.values())
    revenue = total_returns * AVG_REACTIVATION_PRICE / 1000

    return {
        "pools": pools,
        "avg_monthly_churn": round(avg_monthly_churn),
        "pool_anomaly": pool_anomaly,
        "returns_by_bucket": {k: round(v, 1) for k, v in returns_by_bucket.items()},
        "total_returns": round(total_returns, 1),
        "revenue_k": round(revenue, 1),
    }


# ── Прочие отделы ─────────────────────────────────────────────────────────────

def _safe_mean(values: list[float]) -> float:
    return statistics.mean(values) if values else 0.0


def calc_other_depts(
    sales: list[dict],
    target_month: date,
) -> dict[str, float]:
    """
    Средняя выручка по каждому отделу за последние 3 доступных месяца
    (исключая аномальные периоды и Клубные карты).
    Месяцы-выбросы (>1.5× медианы) автоматически исключаются.
    Возвращает словарь {отдел: средняя выручка тыс. руб.} без сезонных поправок.
    """
    excluded = EXCLUDED_PERIODS | ANOMALY_PERIODS_COMP_V
    dept_months: dict[str, dict[str, float]] = {}

    for row in sales:
        dept = row["dept"]
        if not dept or "карт" in dept.lower():
            continue
        mk = _month_key(row["month"])
        if mk in excluded:
            continue
        months_ago = _months_diff(row["month"], target_month)
        if months_ago < 1 or months_ago > 3:
            continue
        dept_months.setdefault(dept, {})[mk] = row["revenue"] / 1000

    result: dict[str, float] = {}
    for dept, months in dept_months.items():
        if not months:
            continue
        values = list(months.values())
        # Фильтрация выбросов: если значение >1.5× медианы — исключаем
        if len(values) >= 3:
            med = statistics.median(values)
            if med > 0:
                filtered = [v for v in values if v <= 1.5 * med]
                if len(filtered) >= 2:
                    values = filtered
        result[dept] = round(_safe_mean(values), 1)
    return result


def calc_fitness_seasonal(avg_k: float, month: int, pt_runway: float) -> float:
    """Применяет сезонный коэффициент к фитнес-услугам + снижение при избытке ПТ-буфера."""
    value = avg_k * SEASON_COEFF_A[month]
    if pt_runway > PT_BUFFER_RUNWAY_THRESHOLD:
        value *= 0.80
    return round(value, 1)


# ── ПТ-буфер ──────────────────────────────────────────────────────────────────

def calc_pt_buffer(pt_rows: list[dict], consumption: float | None = None) -> dict[str, Any]:
    sessions = sum(
        row["sessions"] for row in pt_rows
        if any(seg in row["segment"] for seg in PT_SEGMENTS)
        or row["segment"] in PT_SEGMENTS
    )
    eff_consumption = consumption if (consumption and consumption > 0) else PT_CONSUMPTION_PER_MONTH
    runway = sessions / eff_consumption
    warning = runway > PT_BUFFER_RUNWAY_THRESHOLD
    return {
        "sessions": round(sessions),
        "consumption": round(eff_consumption),
        "runway": round(runway, 2),
        "warning": warning,
        "rows": [r for r in pt_rows if r["sessions"] >= 10],
    }


# ── Исторические данные для графика ──────────────────────────────────────────

def build_chart_data(sales: list[dict], target_month: date) -> dict[str, Any]:
    """Строит данные для графика выручки по отделам (последние 12 месяцев)."""
    from collections import defaultdict

    labels = []
    months_seq = []
    for i in range(12, 0, -1):
        m = _month_add(date(target_month.year, target_month.month, 1), -i)
        months_seq.append(m)
        labels.append(f"{MONTH_SHORT_RU[m.month]} {str(m.year)[2:]}")

    dept_order = ["Клубные карты", "Фитнес-услуги", "Бар", "СПА-услуги", "Солярий", "Доп. услуги", "Товары"]
    by_month_dept: dict[str, dict[str, float]] = defaultdict(dict)

    for row in sales:
        mk = _month_key(row["month"])
        by_month_dept[mk][row["dept"]] = row["revenue"] / 1000

    datasets = []
    colors = {
        "Клубные карты": "#39FF14",
        "Фитнес-услуги": "#6eb3ff",
        "Бар": "#ffa94d",
        "СПА-услуги": "#cc99ff",
        "Солярий": "#ffdd57",
        "Доп. услуги": "#80c8c8",
        "Товары": "#aaaaaa",
    }
    for dept in dept_order:
        data = []
        for m in months_seq:
            mk = _month_key(m)
            data.append(round(by_month_dept.get(mk, {}).get(dept, 0), 1))
        if any(v > 0 for v in data):
            datasets.append({
                "label": dept,
                "data": data,
                "backgroundColor": colors.get(dept, "#888"),
            })

    return {"labels": labels, "datasets": datasets}


# ── Главная сборка ────────────────────────────────────────────────────────────

def build_forecast(
    target_month: date,
    base: dict[str, int],
    churn_pool: list[dict],
    sales: list[dict],
    pt_rows: list[dict],
    avg_check: dict[str, float],
    ai_results: dict,
    pt_consumption: float | None = None,
) -> dict[str, Any]:
    """Собирает итоговую структуру данных для шаблона."""
    m = target_month.month

    # Средний чек: актуальный vs константа
    renewal_check_actual = avg_check.get("Продление", 0)
    renewal_check_const = AVG_RENEWAL_PRICE["А"]
    check_deviation = (
        abs(renewal_check_actual - renewal_check_const) / renewal_check_const
        if renewal_check_const > 0 else 0
    )
    effective_check = renewal_check_actual if check_deviation > AVG_CHECK_DEVIATION_WARN else renewal_check_const

    # Компонент А
    comp_a = calc_component_a(base, m, avg_check_renewal=effective_check if effective_check > 0 else None)
    comp_a["check_note"] = ai_results.get("check_note")
    comp_a["retention_note"] = ai_results.get("retention_note")
    comp_a["check_deviation_pct"] = round(check_deviation * 100, 1)
    comp_a["check_actual"] = round(renewal_check_actual)
    comp_a["check_const"] = round(renewal_check_const)

    # Компонент Б
    pool_coeff = ai_results.get("pool_coefficient", 1.0)
    comp_b = calc_component_b(churn_pool, target_month, pool_13_24_coeff=pool_coeff)
    comp_b["pool_note"] = ai_results.get("pool_note")

    # Компонент В
    comp_c_revenue = COMP_V_BASELINE[m]

    # ПТ-буфер
    pt = calc_pt_buffer(pt_rows, consumption=pt_consumption)

    # Прочие отделы
    depts = calc_other_depts(sales, target_month)
    fitness_avg = depts.get("Фитнес-услуги", 0)
    fitness = calc_fitness_seasonal(fitness_avg, m, pt["runway"])
    depts["Фитнес-услуги"] = fitness

    # Сезонный коэффициент на все прочие отделы (кроме Фитнес — у него своя формула)
    s = SEASON_COEFF_A[m]
    for dept in list(depts.keys()):
        if dept != "Фитнес-услуги":
            depts[dept] = round(depts[dept] * s, 1)

    # Итог
    cards_total = comp_a["revenue_k"] + comp_b["revenue_k"] + comp_c_revenue
    other_total = sum(v for k, v in depts.items() if k != "Фитнес-услуги")
    total = cards_total + fitness + other_total

    # Данные для графика
    chart = build_chart_data(sales, target_month)

    return {
        "target_month": target_month.strftime("%Y-%m"),
        "target_month_display": f"{MONTH_NAMES_RU[m]} {target_month.year}",
        "computed_at": datetime.datetime.now().strftime("%d.%m.%Y %H:%M"),
        "comp_a": comp_a,
        "comp_b": comp_b,
        "comp_c": {
            "revenue_k": comp_c_revenue,
            "baseline": COMP_V_BASELINE[m],
        },
        "depts": depts,
        "pt": pt,
        "avg_check": avg_check,
        "check_deviation_pct": round(check_deviation * 100, 1),
        "cards_total_k": round(cards_total, 1),
        "other_total_k": round(other_total + fitness, 1),
        "total_k": round(total, 1),
        "risks": ai_results.get("risks"),
        "ai_comment": ai_results.get("comment"),
        "chart": chart,
        "season_coeff": SEASON_COEFF_A[m],
    }
