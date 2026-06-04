"""Вызовы OpenRouter (deepseek/deepseek-v3-0324) для аналитических суждений."""

from __future__ import annotations
import json
import logging
import os
import re
from typing import Any

import httpx

log = logging.getLogger("ai")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "deepseek/deepseek-v4-pro"


def _key() -> str:
    return os.environ.get("OPENROUTER_API_KEY", "")


def _ai_call(system: str, user: str, max_tokens: int = 350) -> dict | None:
    key = _key()
    if not key or key.startswith("sk-or-REPLACE"):
        log.warning("OpenRouter API key not configured, skipping AI call")
        return None
    try:
        r = httpx.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={
                "model": MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.3,
            },
            timeout=30,
        )
        if not r.is_success:
            log.warning("OpenRouter HTTP %s: %s", r.status_code, r.text[:300])
            return None
        resp_json = r.json()
        msg = resp_json.get("choices", [{}])[0].get("message", {})
        # deepseek reasoning-моделей может вернуть content=null + reasoning_content
        content = msg.get("content") or msg.get("reasoning_content")
        if not content:
            log.warning("OpenRouter empty content: %s", str(resp_json)[:300])
            return None
        # Ищем JSON в тексте (модель может добавлять ```json ... ```)
        raw = content.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
        return json.loads(raw)
    except Exception as e:
        log.warning("OpenRouter error: %s", e)
        return None


# ── AI-функции ────────────────────────────────────────────────────────────────

def ai_explain_check_deviation(actual: float, expected: float, month: int) -> str | None:
    result = _ai_call(
        system="Ты аналитик фитнес-клуба Hulk Fit (Нижний Тагил). Отвечай строго JSON на русском языке.",
        user=(
            f"Средний чек продления клубной карты за последние 90 дней: {actual:.0f} руб. "
            f"Историческая константа модели: {expected:.0f} руб. "
            f"Отклонение {(actual - expected) / expected * 100:.1f}%, месяц прогноза: {month}. "
            "Объясни причину (1-2 предложения) и дай рекомендацию. "
            'JSON: {"explanation": "...", "recommendation": "..."}'
        ),
    )
    if result:
        return result.get("explanation", "") + " " + result.get("recommendation", "")
    return None


def ai_explain_retention(renewal_rate: float, base: dict, month: int) -> str | None:
    result = _ai_call(
        system="Ты аналитик фитнес-клуба Hulk Fit. Отвечай строго JSON на русском языке.",
        user=(
            f"Прогнозный retention клубных карт: {renewal_rate * 100:.1f}% "
            f"(норма 28-35%), месяц: {month}. "
            f"База: А={base['А']}, В={base['В']}, С={base['С']}, Никогда={base['Никогда']}. "
            "Объясни возможную причину аномалии (1-2 предложения). "
            'JSON: {"explanation": "..."}'
        ),
    )
    if result:
        return result.get("explanation")
    return None


def ai_decide_pool_adjustment(pool_size: int, avg_monthly: float) -> float:
    result = _ai_call(
        system="Ты финансовый аналитик. Отвечай строго JSON.",
        user=(
            f"Пул ушедших клиентов 13-24 месяца назад: {pool_size} чел. "
            f"Среднемесячный отток (последние 12 мес.): {avg_monthly:.0f} чел. "
            f"Пул в {pool_size / (avg_monthly * 12):.1f}× больше 12-месячной нормы. "
            "Это аномалия — массовые истечения годовых карт летом 2024. "
            "Нужен ли понижающий коэффициент 0.5 к этому пулу? "
            'JSON: {"apply_adjustment": true, "coefficient": 0.5, "reason": "..."}'
        ),
    )
    if result:
        return float(result.get("coefficient", 1.0))
    return 1.0


def ai_generate_risks(
    comp_a_k: float, comp_b_k: float, comp_c_k: float,
    base: dict, renewal_rate: float,
    fitness_k: float, runway: float, month: int,
    total_k: float,
) -> dict | None:
    result = _ai_call(
        system=(
            "Ты аналитик фитнес-клуба Hulk Fit (Нижний Тагил). "
            "Пиши кратко, конкретно, только факты из данных. "
            "Отвечай строго JSON на русском."
        ),
        user=(
            f"Данные прогноза на месяц {month}:\n"
            f"- Итого прогноз: {total_k:.0f} тыс. руб.\n"
            f"- Компонент А (продления): {comp_a_k:.0f} тыс., retention {renewal_rate*100:.1f}%\n"
            f"  (А:{base['А']}, В:{base['В']}, С:{base['С']}, Никогда:{base['Никогда']})\n"
            f"- Компонент Б (реактивация): {comp_b_k:.0f} тыс.\n"
            f"- Компонент В (новые): {comp_c_k:.0f} тыс.\n"
            f"- Фитнес-услуги: {fitness_k:.0f} тыс., ПТ runway {runway:.1f} мес.\n"
            "Сгенерируй 4 конкретных риска снижения и 4 возможности роста — по 1 предложению каждый.\n"
            'JSON: {"down": ["...", "...", "...", "..."], "up": ["...", "...", "...", "..."]}'
        ),
        max_tokens=500,
    )
    return result


def ai_generate_comment(
    total_k: float, comp_a_k: float, comp_b_k: float, comp_c_k: float,
    renewal_rate: float, month: int, month_name: str,
) -> str | None:
    result = _ai_call(
        system=(
            "Ты аналитик фитнес-клуба Hulk Fit. Пиши деловым языком, 3-4 предложения. "
            "Отвечай строго JSON на русском."
        ),
        user=(
            f"Прогноз выручки на {month_name}: {total_k:.0f} тыс. руб. "
            f"Продления: {comp_a_k:.0f} тыс. ({comp_a_k/total_k*100:.0f}%), "
            f"реактивация: {comp_b_k:.0f} тыс. ({comp_b_k/total_k*100:.0f}%), "
            f"новые: {comp_c_k:.0f} тыс. ({comp_c_k/total_k*100:.0f}%). "
            f"Retention: {renewal_rate*100:.1f}%. "
            "Напиши итоговый комментарий для карточки планирования (3-4 предложения). "
            'JSON: {"comment": "..."}'
        ),
    )
    if result:
        return result.get("comment")
    return None


# ── Оркестрация всех AI-вызовов ───────────────────────────────────────────────

def run_ai_analysis(
    avg_check: dict[str, float],
    comp_a_data: dict,
    comp_b_data: dict,
    comp_c_k: float,
    depts: dict,
    pt: dict,
    month: int,
    month_name: str,
    total_k: float,
) -> dict[str, Any]:
    """
    Запускает AI-анализ последовательно.
    Возвращает словарь с результатами (None = AI не вызывался/ошибка).
    """
    from calculator import AVG_RENEWAL_PRICE, AVG_CHECK_DEVIATION_AI, RENEWAL_RATE_LOW, RENEWAL_RATE_HIGH

    results: dict[str, Any] = {}

    # 1. Проверка среднего чека
    actual_check = avg_check.get("Продление", 0)
    const_check = AVG_RENEWAL_PRICE["А"]
    if actual_check > 0:
        deviation = abs(actual_check - const_check) / const_check
        if deviation > AVG_CHECK_DEVIATION_AI:
            results["check_note"] = ai_explain_check_deviation(actual_check, const_check, month)

    # 2. Проверка retention
    renewal_rate = comp_a_data.get("renewal_rate", 0)
    if renewal_rate > 0 and not (RENEWAL_RATE_LOW <= renewal_rate <= RENEWAL_RATE_HIGH):
        results["retention_note"] = ai_explain_retention(renewal_rate, comp_a_data["base"], month)

    # 3. Корректировка пула 13-24m
    # Аномалия лето 2024 — детерминированная: всегда применяем коэф. 0.5
    if comp_b_data.get("pool_anomaly"):
        results["pool_coefficient"] = 0.5
        results["pool_note"] = "Коэффициент 0.5: аномальный пул (массовые истечения годовых карт лето 2024)"

    # 4. Риски
    risks = ai_generate_risks(
        comp_a_k=comp_a_data.get("revenue_k", 0),
        comp_b_k=comp_b_data.get("revenue_k", 0),
        comp_c_k=comp_c_k,
        base=comp_a_data.get("base", {}),
        renewal_rate=renewal_rate,
        fitness_k=depts.get("Фитнес-услуги", 0),
        runway=pt.get("runway", 0),
        month=month,
        total_k=total_k,
    )
    results["risks"] = risks

    # 5. Итоговый комментарий
    results["comment"] = ai_generate_comment(
        total_k=total_k,
        comp_a_k=comp_a_data.get("revenue_k", 0),
        comp_b_k=comp_b_data.get("revenue_k", 0),
        comp_c_k=comp_c_k,
        renewal_rate=renewal_rate,
        month=month,
        month_name=month_name,
    )

    return results
