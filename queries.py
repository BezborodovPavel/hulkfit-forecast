"""Запросы к 1С FitClub через MCP execute_1c_query; отборы — по сегментам из настроек."""

from __future__ import annotations
import asyncio
import os
import json
import logging
from datetime import date, datetime
from typing import Any

from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

log = logging.getLogger("queries")

# Задаётся из app.py при старте
_MCP_URL: str = "https://mcp1c.hulk.fit/mcp/"
_MCP_TOKEN: str = os.environ.get("MCP_1C_TOKEN", "")


def set_mcp_url(url: str) -> None:
    global _MCP_URL
    _MCP_URL = url


def _fmt(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def parse_1c_date(s: str) -> date | None:
    """Парсит дату из формата 1С '01.07.2024 0:00:00' или ISO '2024-07-01'."""
    if not s:
        return None
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


_RETRIES = 2
_RETRY_DELAY = 2.5


async def _query_1c(query_text: str, params: dict[str, Any]) -> list[dict]:
    """Выполняет 1С-запрос через MCP с retry при ExceptionGroup."""
    last_err: Exception = RuntimeError("no attempts")

    for attempt in range(_RETRIES + 1):
        try:
            async with streamablehttp_client(_MCP_URL, headers=({"Authorization": f"Bearer {_MCP_TOKEN}"} if _MCP_TOKEN else {})) as (r, w, _):
                async with ClientSession(r, w) as sess:
                    await sess.initialize()
                    result = await sess.call_tool("execute_1c_query", {
                        "QueryText": query_text,
                        "Parameters": params,
                    })

            if not result.content:
                return []

            texts = [c.text for c in result.content if hasattr(c, "text")]
            raw = "\n".join(texts)
            try:
                data = json.loads(raw)
            except Exception as parse_err:
                log.warning("Failed to parse 1C response: %s", raw[:300])
                # «Ошибка выполнения инструмента» — intermittent, обрабатываем как retryable
                raise RuntimeError(f"1C response parse error: {raw[:100]}") from parse_err

            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return data.get("data") or data.get("value") or data.get("rows") or []
            return []

        except Exception as e:
            # ExceptionGroup из asyncio.TaskGroup / anyio в Python 3.11+
            cause = e.exceptions[0] if isinstance(e, BaseExceptionGroup) else e
            last_err = cause
            log.warning("1C query error attempt %d/%d: %s", attempt + 1, _RETRIES + 1, cause)
            if attempt < _RETRIES:
                await asyncio.sleep(_RETRY_DELAY * (attempt + 1))

    log.error("1C query failed after %d attempts: %s", _RETRIES + 1, last_err)
    raise last_err


# ── Отборы по сегментам номенклатуры 1С (настройки, settings_store) ──────────
# execute_1c_query не передаёт ссылку по UUID, поэтому сегмент — по наименованию
# (массив &Сегменты...). Дубли имён отсекает Q_CHECK перед расчётом.
def _cards_filter(field: str, alias: str) -> str:
    """Номенклатура — членство (клубная карта) из выбранных сегментов карт."""
    return (
        f"{field}.ТипНоменклатуры = ЗНАЧЕНИЕ(Перечисление.ТипыНоменклатуры.АбонементЧленство)\n"
        f"        И {field} В (ВЫБРАТЬ {alias}.ОбъектСегмента ИЗ РегистрСведений.СоставСегментов КАК {alias}\n"
        f"                    ГДЕ {alias}.Сегмент.Наименование В (&СегментыКарт))"
    )


# УслугаСегмент в РН.ЧленстваПакетыУслуг — услуга ИЛИ сегмент («любая услуга сегмента»).
# Берём услугу из выбранных сегментов либо сегмент, все позиции которого внутри набора
# (вариант «хотя бы одна позиция» тянет «Доступное на вводном занятии»; проверено fitclub-1c-expert).
PT_FILTER = """(ЧП.УслугаСегмент В (ВЫБРАТЬ С.ОбъектСегмента ИЗ РегистрСведений.СоставСегментов КАК С
                         ГДЕ С.Сегмент.Наименование В (&СегментыПТ))
     ИЛИ (ЧП.УслугаСегмент ССЫЛКА Справочник.СегментыНоменклатуры
          И ЧП.УслугаСегмент В (ВЫБРАТЬ Х.Сегмент ИЗ РегистрСведений.СоставСегментов КАК Х)
          И НЕ ЧП.УслугаСегмент В (ВЫБРАТЬ Х2.Сегмент ИЗ РегистрСведений.СоставСегментов КАК Х2
               ГДЕ НЕ Х2.ОбъектСегмента В (ВЫБРАТЬ С2.ОбъектСегмента ИЗ РегистрСведений.СоставСегментов КАК С2
                                          ГДЕ С2.Сегмент.Наименование В (&СегментыПТ)))))"""

# ── Q1: База окончаний (компонент А) ─────────────────────────────────────────
# Используем подзапрос вместо ПОМЕСТИТЬ — execute_1c_query возвращает
# результат последнего SELECT, но подзапрос надёжнее в HTTP-режиме.
Q1 = f"""
ВЫБРАТЬ
    ВЫБОР
        КОГДА Баз.МаксПосещение ЕСТЬ NULL ТОГДА "Никогда"
        КОГДА РАЗНОСТЬДАТ(Баз.МаксПосещение, Баз.СрокДействия, ДЕНЬ) <= 7 ТОГДА "А"
        КОГДА РАЗНОСТЬДАТ(Баз.МаксПосещение, Баз.СрокДействия, ДЕНЬ) <= 14 ТОГДА "В"
        ИНАЧЕ "С"
    КОНЕЦ КАК КатегорияДавности,
    КОЛИЧЕСТВО(*) КАК КолКарт
ИЗ (
    ВЫБРАТЬ
        ЧП.ЧленствоПакетУслуг КАК ЧленствоПакетУслуг,
        ЧП.СрокДействия КАК СрокДействия,
        МАКСИМУМ(Пос.ДатаПрихода) КАК МаксПосещение
    ИЗ
        РегистрСведений.ЧленстваПакетыУслугИтоги КАК ЧП
            ЛЕВОЕ СОЕДИНЕНИЕ Документ.Посещение КАК Пос
            ПО ЧП.ЧленствоПакетУслуг.Контрагент = Пос.Контрагент
                И Пос.ДатаПрихода <= ЧП.СрокДействия
                И Пос.Проведен = ИСТИНА
                И Пос.ПризнакГость = ЛОЖЬ
                И Пос.ПризнакОтменен = ЛОЖЬ
    ГДЕ
        ЧП.СрокДействия МЕЖДУ &НачалоМес И &КонецМес
        И {_cards_filter("ЧП.ЧленствоПакетУслуг.Номенклатура", "СгК")}
    СГРУППИРОВАТЬ ПО ЧП.ЧленствоПакетУслуг, ЧП.СрокДействия
) КАК Баз
СГРУППИРОВАТЬ ПО
    ВЫБОР
        КОГДА Баз.МаксПосещение ЕСТЬ NULL ТОГДА "Никогда"
        КОГДА РАЗНОСТЬДАТ(Баз.МаксПосещение, Баз.СрокДействия, ДЕНЬ) <= 7 ТОГДА "А"
        КОГДА РАЗНОСТЬДАТ(Баз.МаксПосещение, Баз.СрокДействия, ДЕНЬ) <= 14 ТОГДА "В"
        ИНАЧЕ "С"
    КОНЕЦ
"""

# ── Q2: Пул ушедших (компонент Б) ────────────────────────────────────────────
Q2 = f"""
ВЫБРАТЬ
    НАЧАЛОПЕРИОДА(ЧП.СрокДействия, МЕСЯЦ) КАК МесяцУхода,
    КОЛИЧЕСТВО(РАЗЛИЧНЫЕ ЧП.ЧленствоПакетУслуг) КАК КолУшедших
ИЗ
    РегистрСведений.ЧленстваПакетыУслугИтоги КАК ЧП
        ЛЕВОЕ СОЕДИНЕНИЕ РегистрСведений.ЧленстваПакетыУслугИтоги КАК СлК
        ПО СлК.ЧленствоПакетУслуг.Контрагент = ЧП.ЧленствоПакетУслуг.Контрагент
            И СлК.ДатаАктивации МЕЖДУ
                ДОБАВИТЬКДАТЕ(ЧП.СрокДействия, ДЕНЬ, -30)
                И ДОБАВИТЬКДАТЕ(ЧП.СрокДействия, ДЕНЬ, 14)
            И СлК.ЧленствоПакетУслуг <> ЧП.ЧленствоПакетУслуг
            И {_cards_filter("СлК.ЧленствоПакетУслуг.Номенклатура", "СгС")}
ГДЕ
    ЧП.СрокДействия МЕЖДУ &НачалоПериода И &КонецМес
    И {_cards_filter("ЧП.ЧленствоПакетУслуг.Номенклатура", "СгК")}
    И СлК.ЧленствоПакетУслуг ЕСТЬ NULL
СГРУППИРОВАТЬ ПО НАЧАЛОПЕРИОДА(ЧП.СрокДействия, МЕСЯЦ)
УПОРЯДОЧИТЬ ПО МесяцУхода
"""

# ── Q3: Продажи по подразделениям ────────────────────────────────────────────
Q3 = """
ВЫБРАТЬ
    НАЧАЛОПЕРИОДА(Реал.Период, МЕСЯЦ) КАК Месяц,
    Реал.Номенклатура.Подразделение.Наименование КАК Подразделение,
    СУММА(Реал.СтоимостьОборот) КАК Выручка,
    КОЛИЧЕСТВО(РАЗЛИЧНЫЕ Реал.Контрагент) КАК КолКлиентов
ИЗ
    РегистрНакопления.ПродажиСебестоимость.Обороты(&НачалоПериода, &КонецМес, Месяц, ) КАК Реал
СГРУППИРОВАТЬ ПО
    НАЧАЛОПЕРИОДА(Реал.Период, МЕСЯЦ),
    Реал.Номенклатура.Подразделение.Наименование
УПОРЯДОЧИТЬ ПО Месяц, Подразделение
"""

# ── Q4: Буфер ПТ-пакетов ──────────────────────────────────────────────────────
Q4 = f"""
ВЫБРАТЬ
    ПРЕДСТАВЛЕНИЕ(ЧП.УслугаСегмент) КАК Сегмент,
    КОЛИЧЕСТВО(РАЗЛИЧНЫЕ ЧП.Основание) КАК КолПакетов,
    СУММА(ЧП.КоличествоОстаток) КАК ОстатокСессий
ИЗ
    РегистрНакопления.ЧленстваПакетыУслуг.Остатки(&НаДату, ) КАК ЧП
ГДЕ
    ЧП.КоличествоОстаток > 0
    И {PT_FILTER}
СГРУППИРОВАТЬ ПО ПРЕДСТАВЛЕНИЕ(ЧП.УслугаСегмент)
УПОРЯДОЧИТЬ ПО ОстатокСессий УБЫВ
"""

# ── Q5: Средний чек карт ──────────────────────────────────────────────────────
# Подзапрос: сначала суммируем Стоимость по каждой карте (одна карта может давать
# несколько строк в регистре ПродажиСебестоимость), потом усредняем по картам.
Q5 = f"""
ВЫБРАТЬ
    ПодзапросЧек.ТипПокупки КАК ТипПокупки,
    КОЛИЧЕСТВО(*) КАК КолПродаж,
    СРЕДНЕЕ(ПодзапросЧек.ИтогоПоКарте) КАК СреднийЧек,
    СУММА(ПодзапросЧек.ИтогоПоКарте) КАК ИтогоВыручка
ИЗ (
    ВЫБРАТЬ
        ВЫБОР
            КОГДА Реал.ЧленствоПакетУслуг.ТипПродления =
                 ЗНАЧЕНИЕ(Перечисление.ТипыПродленияЧленстваПакетаУслуг.Продление) ТОГДА "Продление"
            КОГДА Реал.ЧленствоПакетУслуг.ТипПродления =
                 ЗНАЧЕНИЕ(Перечисление.ТипыПродленияЧленстваПакетаУслуг.Новое) ТОГДА "Новое"
            ИНАЧЕ "Прочее"
        КОНЕЦ КАК ТипПокупки,
        СУММА(Реал.Стоимость) КАК ИтогоПоКарте
    ИЗ
        РегистрНакопления.ПродажиСебестоимость КАК Реал
    ГДЕ
        Реал.Период МЕЖДУ &НачалоПериода И &КонецМес
        И {_cards_filter("Реал.Номенклатура", "СгК")}
    СГРУППИРОВАТЬ ПО
        ВЫБОР
            КОГДА Реал.ЧленствоПакетУслуг.ТипПродления =
                 ЗНАЧЕНИЕ(Перечисление.ТипыПродленияЧленстваПакетаУслуг.Продление) ТОГДА "Продление"
            КОГДА Реал.ЧленствоПакетУслуг.ТипПродления =
                 ЗНАЧЕНИЕ(Перечисление.ТипыПродленияЧленстваПакетаУслуг.Новое) ТОГДА "Новое"
            ИНАЧЕ "Прочее"
        КОНЕЦ,
        Реал.ЧленствоПакетУслуг
) КАК ПодзапросЧек
СГРУППИРОВАТЬ ПО ПодзапросЧек.ТипПокупки
"""

# ── Q6: Потребление ПТ-сессий по месяцам ─────────────────────────────────────
# Ввод начальных остатков (списание старых остатков) — не потребление: периодичность
# Регистратор, чтобы его исключить. Февраль–октябрь 2026 с исключением и без совпали.
Q6 = f"""
ВЫБРАТЬ
    НАЧАЛОПЕРИОДА(ЧП.Период, МЕСЯЦ) КАК Месяц,
    СУММА(ЧП.КоличествоРасход) КАК СессийПотреблено
ИЗ
    РегистрНакопления.ЧленстваПакетыУслуг.Обороты(&НачалоПериода, КОНЕЦПЕРИОДА(&КонецПериода, ДЕНЬ), Регистратор, ) КАК ЧП
ГДЕ
    НЕ ЧП.Регистратор ССЫЛКА Документ.ВводНачальныхОстатков
    И {PT_FILTER}
СГРУППИРОВАТЬ ПО НАЧАЛОПЕРИОДА(ЧП.Период, МЕСЯЦ)
УПОРЯДОЧИТЬ ПО Месяц
"""


# ── Справочники для настроек ─────────────────────────────────────────────────
# Список сегментов строится по составу (list_nomenclature_segments не показывает
# сегменты членств). Записей > 1 — в 1С несколько сегментов с этим именем.
Q_SEGMENTS = """
ВЫБРАТЬ
    С.Сегмент.Наименование КАК Имя,
    КОЛИЧЕСТВО(РАЗЛИЧНЫЕ С.Сегмент) КАК Записей,
    КОЛИЧЕСТВО(РАЗЛИЧНЫЕ С.ОбъектСегмента) КАК Позиций,
    СУММА(ВЫБОР КОГДА С.ОбъектСегмента.ТипНоменклатуры = ЗНАЧЕНИЕ(Перечисление.ТипыНоменклатуры.АбонементЧленство)
        ТОГДА 1 ИНАЧЕ 0 КОНЕЦ) КАК Членств
ИЗ
    РегистрСведений.СоставСегментов КАК С
ГДЕ
    С.Сегмент ССЫЛКА Справочник.СегментыНоменклатуры
СГРУППИРОВАТЬ ПО
    С.Сегмент.Наименование
"""

Q_DEPTS = """
ВЫБРАТЬ П.Наименование КАК Имя
ИЗ Справочник.Подразделения КАК П
ГДЕ НЕ П.ПометкаУдаления
УПОРЯДОЧИТЬ ПО Имя
"""

# Проверка выбранных сегментов перед расчётом: есть ли и не задвоены ли имена
Q_CHECK = """
ВЫБРАТЬ
    С.Сегмент.Наименование КАК Имя,
    КОЛИЧЕСТВО(РАЗЛИЧНЫЕ С.Сегмент) КАК Записей
ИЗ
    РегистрСведений.СоставСегментов КАК С
ГДЕ
    С.Сегмент ССЫЛКА Справочник.СегментыНоменклатуры
    И С.Сегмент.Наименование В (&Имена)
СГРУППИРОВАТЬ ПО
    С.Сегмент.Наименование
"""


class SettingsError(RuntimeError):
    """Выбранные в настройках сегменты нельзя применить — расчёт не делаем."""


async def list_segments() -> list[dict]:
    rows = await _query_1c(Q_SEGMENTS, {})
    out = []
    for r in rows:
        name = str(r.get("Имя") or "")
        if not name.strip():
            continue
        out.append({
            "name": name,
            "dups": int(r.get("Записей", 0) or 0),
            "items": int(r.get("Позиций", 0) or 0),
            "cards": int(r.get("Членств", 0) or 0),
        })
    out.sort(key=lambda r: r["name"].lower())
    return out


async def list_departments() -> list[str]:
    rows = await _query_1c(Q_DEPTS, {})
    return [str(r.get("Имя")) for r in rows if r.get("Имя")]


async def check_segments(names: list[str]) -> None:
    rows = await _query_1c(Q_CHECK, {"Имена": names})
    found = {str(r.get("Имя")): int(r.get("Записей", 0) or 0) for r in rows}
    missing = [n for n in names if n not in found]
    dups = [n for n, c in found.items() if c > 1]
    problems = []
    if missing:
        problems.append("нет в 1С (или пустой состав): " + ", ".join(f"«{n}»" for n in missing))
    if dups:
        problems.append("в 1С несколько сегментов с именем: " + ", ".join(f"«{n}»" for n in dups))
    if problems:
        raise SettingsError("Сегменты из настроек не применимы — " + "; ".join(problems)
                            + ". Исправьте выбор на странице «Настройки».")


# ── Публичные функции ─────────────────────────────────────────────────────────

async def get_base_expires(start: date, end: date, cards: list[str]) -> dict[str, int]:
    """Q1 → {"А": N, "В": N, "С": N, "Никогда": N}"""
    rows = await _query_1c(Q1, {"НачалоМес": _fmt(start), "КонецМес": _fmt(end), "СегментыКарт": cards})
    result: dict[str, int] = {"А": 0, "В": 0, "С": 0, "Никогда": 0}
    for row in rows:
        cat = str(row.get("КатегорияДавности", ""))
        n = int(row.get("КолКарт", 0) or 0)
        if cat in result:
            result[cat] = n
    return result


async def get_churn_pool(start: date, end: date, cards: list[str]) -> list[dict]:
    """Q2 → [{month: date, count: int}, ...]  отсортировано по дате."""
    rows = await _query_1c(Q2, {"НачалоПериода": _fmt(start), "КонецМес": _fmt(end), "СегментыКарт": cards})
    result = []
    for row in rows:
        d = parse_1c_date(str(row.get("МесяцУхода", "")))
        n = int(row.get("КолУшедших", 0) or 0)
        if d and n > 0:
            result.append({"month": d, "count": n})
    result.sort(key=lambda x: x["month"])
    return result


async def get_sales_by_dept(start: date, end: date) -> list[dict]:
    """Q3 → [{month: date, dept: str, revenue: float, clients: int}, ...]"""
    rows = await _query_1c(Q3, {"НачалоПериода": _fmt(start), "КонецМес": _fmt(end)})
    result = []
    for row in rows:
        d = parse_1c_date(str(row.get("Месяц", "")))
        if d:
            result.append({
                "month": d,
                "dept": str(row.get("Подразделение", "")),
                "revenue": float(row.get("Выручка", 0) or 0),
                "clients": int(row.get("КолКлиентов", 0) or 0),
            })
    return result


async def get_pt_buffer(as_of: date, pt: list[str]) -> list[dict]:
    """Q4 → [{segment: str, packages: int, sessions: float}, ...] — только сегменты ПТ из настроек."""
    rows = await _query_1c(Q4, {"НаДату": _fmt(as_of), "СегментыПТ": pt})
    return [
        {
            "segment": str(row.get("Сегмент", "")),
            "packages": int(row.get("КолПакетов", 0) or 0),
            "sessions": float(row.get("ОстатокСессий", 0) or 0),
        }
        for row in rows
    ]


async def get_avg_check(start: date, end: date, cards: list[str]) -> dict[str, float]:
    """Q5 → {"Продление": N, "Новое": N, "Прочее": N}  средний чек руб."""
    rows = await _query_1c(Q5, {"НачалоПериода": _fmt(start), "КонецМес": _fmt(end), "СегментыКарт": cards})
    result: dict[str, float] = {"Продление": 0.0, "Новое": 0.0, "Прочее": 0.0}
    for row in rows:
        t = str(row.get("ТипПокупки", ""))
        v = float(row.get("СреднийЧек", 0) or 0)
        if t in result:
            result[t] = v
    return result


async def get_pt_consumption(start: date, end: date, pt: list[str]) -> float:
    """
    Q6 → среднемесячное потребление ПТ-сессий за период.
    Используется вместо константы PT_CONSUMPTION_PER_MONTH.
    Fallback: 1703 если запрос не вернул данных.
    """
    rows = await _query_1c(Q6, {"НачалоПериода": _fmt(start), "КонецПериода": _fmt(end), "СегментыПТ": pt})
    values = [float(r.get("СессийПотреблено", 0) or 0) for r in rows if r.get("СессийПотреблено")]
    if not values:
        return 1703.0
    return round(sum(values) / len(values), 1)


async def fetch_all(
    target_month: date,
    cfg: dict,
) -> tuple[dict, list, list, list, dict, float]:
    """
    Проверяет сегменты из настроек (SettingsError — расчёт не делаем),
    затем параллельно выполняет 6 запросов.
    Возвращает (base, churn_pool, sales, pt_buffer, avg_check, pt_consumption).
    """
    await check_segments(sorted({n for k in ("cards", "pt") for n in cfg[k]}))
    cards, pt = cfg["cards"], cfg["pt"]
    from calendar import monthrange

    # Параметры периодов
    prev_month_end = date(target_month.year, target_month.month, 1)
    # конец предыдущего месяца
    import datetime as dt
    prev_month_end = date(target_month.year, target_month.month, 1) - dt.timedelta(days=1)
    prev_month_start = date(prev_month_end.year, prev_month_end.month, 1)

    _, last_day = monthrange(target_month.year, target_month.month)
    target_start = date(target_month.year, target_month.month, 1)
    target_end = date(target_month.year, target_month.month, last_day)

    # Q1: весь прогнозируемый месяц
    q1_start, q1_end = target_start, target_end

    # Q2: 24 месяца назад → конец прошлого месяца
    q2_start = _month_add(target_start, -24)
    q2_end = prev_month_end

    # Q3: 13 месяцев назад → конец прошлого месяца
    q3_start = _month_add(target_start, -13)
    q3_end = prev_month_end

    # Q4: остаток на начало прогнозируемого месяца. Дата без времени в Остатки() — начало дня,
    # поэтому prev_month_end теряла движения последнего дня (сверка с отчётом «Пакеты услуг» 04.11.2026)
    q4_date = target_start

    # Q5: 90 дней назад → вчера
    import datetime as dt2
    today = dt2.date.today()
    q5_start = today - dt2.timedelta(days=90)
    q5_end = today - dt2.timedelta(days=1)

    # Q6: потребление ПТ — последние 3 полностью завершённых месяца
    # Всегда берём месяцы до начала текущего (не включая текущий неполный)
    last_full_month_end = date(today.year, today.month, 1) - dt2.timedelta(days=1)
    q6_end = last_full_month_end
    q6_start = _month_add(date(last_full_month_end.year, last_full_month_end.month, 1), -2)

    results = await asyncio.gather(
        get_base_expires(q1_start, q1_end, cards),
        get_churn_pool(q2_start, q2_end, cards),
        get_sales_by_dept(q3_start, q3_end),
        get_pt_buffer(q4_date, pt),
        get_avg_check(q5_start, q5_end, cards),
        get_pt_consumption(q6_start, q6_end, pt),
        return_exceptions=True,
    )

    # Подставляем дефолты если отдельный запрос упал
    defaults = (
        {"А": 0, "В": 0, "С": 0, "Никогда": 0},
        [],
        [],
        [],
        {"Продление": 0.0, "Новое": 0.0, "Прочее": 0.0},
        1703.0,
    )
    out = []
    names = ("Q1 base_expires", "Q2 churn_pool", "Q3 sales_by_dept", "Q4 pt_buffer", "Q5 avg_check", "Q6 pt_consumption")
    for i, (res, default) in enumerate(zip(results, defaults)):
        if isinstance(res, Exception):
            log.error("1C query %s failed, using default: %s", names[i], res)
            out.append(default)
        else:
            out.append(res)

    return tuple(out)


def _month_add(d: date, months: int) -> date:
    """Добавляет/вычитает месяцы от первого числа месяца."""
    m = d.month + months
    y = d.year
    while m <= 0:
        m += 12
        y -= 1
    while m > 12:
        m -= 12
        y += 1
    return date(y, m, 1)
