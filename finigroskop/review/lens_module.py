"""Оценка ОДНОГО модуля игры по линзам Шелла — вход для «Генератора игр».

Зачем отдельный модуль, а не ветка в lens_evaluator. Оценщик по линзам —
пятый шаг конвейера ФинИгроСкопа, и почти весь его вход собирают предыдущие
четыре: канонический текст автора, заметки «Оценщика статистик», вопросы
диагноста, метаданные симуляциониста. У генератора нет ни одного из них — у
него есть кусок игры, только что собранный агентом, и вердикт аудитора модуля.
Смешивать две сборки входа в одной функции значит получить функцию, где
половина аргументов всегда None; здесь она своя, и по ней сразу видно, из чего
на самом деле складывается оценка модуля.

Сам агент, промпт, валидатор и веса категорий переиспользуются как есть — иначе
через месяц у двух сервисов будут две разные шкалы Шелла с одинаковым названием.

Что здесь считает КОД, а не модель (то же правило, что и в ФинИгроСкопе):
  • средние по категориям — арифметика, и на ней ломается правило «N/A ≠ 0»;
  • итоговый балл — Σ(балл × вес) / Σ(весов), три знака;
  • доля покрытого веса — по ней видно, чего балл НЕ учитывает;
  • можно ли вообще звать линзы — по вердикту аудитора, а не по формулировке.
"""

import json

from review import lens_evaluator, lens_scope
from review.synthesizer import CATEGORY_WEIGHTS, PASSING_SCORE

# Статусы чек-листа аудитора генератора (generator/agents/module_auditor.py).
# Дублируются строкой намеренно: импортировать оттуда нельзя — у генератора свой
# модуль верхнего уровня `config`, и общий sys.path сломал бы оба сервиса.
STATUS_VIOLATION = "violation"
STATUS_CONCERN = "concern"
SEVERITY_CRITICAL = "critical"


# --- условие вызова ----------------------------------------------------------
def should_run(audit: dict) -> dict:
    """Можно ли звать линзы для этого модуля. Решает КОД, а не текст ответа.

    Правило то же, что у ФинИгроСкопа перед линзами: пока есть незакрытые
    критичные находки, модуль чинят, а не оценивают содержательно. Иначе агент
    потратит вызов на разбор замысла у модуля, который всё равно уйдёт на
    перегенерацию, и оценка устареет в момент правки.

    Критичным считается И нарушение чек-листа (`map[].status == violation`),
    И находка severity=critical: первое — вывод кода аудитора, второе — суждение
    модели, и пропускать нельзя ни то, ни другое.
    """
    audit = audit or {}
    violations = [row for row in (audit.get("map") or [])
                  if isinstance(row, dict) and row.get("status") == STATUS_VIOLATION]
    critical = [i for i in (audit.get("issues") or [])
                if isinstance(i, dict) and i.get("severity") == SEVERITY_CRITICAL]

    if violations or critical:
        names = sorted({row.get("item") or "?" for row in violations}
                       | {i.get("checklist_item") or "?" for i in critical})
        return {
            "ready": False,
            "blocking": {"violations": len(violations), "critical": len(critical)},
            "reason": "у аудитора есть незакрытые критичные замечания: "
                      + ", ".join(names) + " — их чинят до оценки по линзам",
        }
    if not (audit.get("map") or []):
        return {"ready": False, "blocking": {},
                "reason": "аудит модуля не выполнен — линзы идут после него"}
    return {"ready": True, "blocking": {}, "reason": "критичных замечаний нет"}


# --- сборка входа ------------------------------------------------------------
def notes_from_audit(audit: dict) -> list:
    """Замечания аудитора → заметки для линз.

    Точный аналог того, чем в ФинИгроСкопе служат заметки числовых агентов:
    место, где проверка что-то заподозрила, но вердикт зависит от замысла игры.
    `concern` — это ровно «так делать можно, но проверь» и есть.

    `violation` сюда не попадает: с ними линзы не запускаются вовсе (see
    should_run), и заметка о них была бы мёртвой.
    """
    out = []
    for row in (audit or {}).get("map") or []:
        if not isinstance(row, dict) or row.get("status") != STATUS_CONCERN:
            continue
        out.append({
            "source": "module_auditor",
            "observation": row.get("note") or row.get("item") or "",
            "from_test": row.get("item"),
            # Вердикта по ним НЕ требуем: у аудитора генератора нет гибридных
            # тестов методички, где число посчитано, а решение не принято.
            # Требовать ответ на каждый concern значило бы навязать агенту
            # обязанность, которой ему никто не давал.
            "question_for_lenses": None,
        })
    for issue in (audit or {}).get("issues") or []:
        if not isinstance(issue, dict) or issue.get("severity") == SEVERITY_CRITICAL:
            continue
        out.append({
            "source": "module_auditor",
            "observation": issue.get("explanation") or "",
            "from_test": issue.get("checklist_item"),
            "question_for_lenses": None,
        })
    return out


def core_facts(params: dict) -> dict:
    """Факты об устройстве партии — то же, что game_spec.core у ФинИгроСкопа.

    Нужны валидатору (одиночная игра → социальные линзы не оценивают) и самому
    агенту как опора: «кооперативная игра на 2–4 игрока без выбывания» — это
    факт из опросника, а не догадка по тексту модуля.
    """
    params = params or {}
    players = params.get("player_count") or {}
    return {
        "players": {"min": players.get("min"), "max": players.get("max")},
        "mode": params.get("interaction"),
        "elimination": params.get("elimination"),
        "catch_up": params.get("catch_up"),
        "age_group": params.get("age_group") or {},
        "play_time": params.get("play_time") or {},
        "complexity": params.get("complexity"),
        "genre": params.get("genre") or [],
        "purpose": params.get("purpose") or [],
    }


def render_module(phase: str, module: dict) -> str:
    """Модуль в читаемом виде — аналог канонического текста игры.

    JSON модели читаются хуже прозы, а линзы оценивают замысел. Отдаём и то, и
    другое: структуру — машинно, а рядом ту же структуру строками, чтобы агент
    не тратил внимание на разбор скобок.
    """
    lines = [f"МОДУЛЬ «{phase}»"]

    def walk(value, indent=1):
        pad = "  " * indent
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, (dict, list)):
                    lines.append(f"{pad}{key}:")
                    walk(item, indent + 1)
                else:
                    lines.append(f"{pad}{key}: {item}")
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, (dict, list)):
                    walk(item, indent)
                    lines.append("")
                else:
                    lines.append(f"{pad}— {item}")
        else:
            lines.append(f"{pad}{value}")

    walk(module or {})
    return "\n".join(lines)


def build_input(phase: str, module: dict, params: dict, audit: dict) -> dict:
    """Вход для build_message в том же виде, в каком его строит ФинИгроСкоп."""
    notes = notes_from_audit(audit)
    return {
        "text": module or {},
        "core": core_facts(params),
        "canonical_text": render_module(phase, module),
        "notes_for_lenses": notes,
        # Вопросов, требующих вердикта, у генератора пока нет — см. notes_from_audit
        "open_questions": [],
        "sim_meta": {},
        "actions_resolution": {},
        "coverage_summary": {},
        "unmeasured": {},
        # Параметры игры подаются целиком: линзы 1, 3, 5, 17 оценивают замысел,
        # а замысел заявлен именно в опроснике, а не в модуле.
        "game_params": params or {},
    }


def build_message(data: dict, scope: dict) -> str:
    """Сообщение агенту: базовое из lens_evaluator плюс параметры игры."""
    base = lens_evaluator.build_message(data, scope)
    params = data.get("game_params") or {}
    if not params:
        return base
    block = "\n".join([
        "",
        "=== ПАРАМЕТРЫ ИГРЫ ИЗ ОПРОСНИКА (что автор заказал) ===",
        json.dumps(params, ensure_ascii=False, indent=2),
        "",
        "Модуль оценивается на соответствие ЭТОМУ заказу, а не абстрактному "
        "идеалу игры.",
    ])
    # Требование «верни один JSON» должно остаться последней строкой.
    marker = "Верни один JSON без текста вокруг."
    return base.replace(marker, block.lstrip("\n") + "\n\n" + marker)


# --- итоговый балл -----------------------------------------------------------
def score(report: dict, scope: dict) -> dict:
    """Балл модуля 0–10 с тремя знаками и честная сводка о его покрытии.

    Формула та же, что у синтезатора: Σ(балл × вес) / Σ(весов ОЦЕНЁННЫХ
    категорий). Категории с N/A не входят ни в числитель, ни в знаменатель —
    это и есть правило «N/A не ноль».

    Вместе с баллом обязательно уходит `weight_covered`: доля веса, реально
    участвовавшая в расчёте. Без неё 8.4 за модуль механик неотличимы от 8.4 за
    игру целиком, хотя стоят за ними разные вещи. Никакая формула этого не
    исправит — это можно только показать.
    """
    categories = lens_evaluator.recompute_categories(report)
    in_scope = set((scope or {}).get("core") or {})

    rows, weighted, weights = [], 0.0, 0.0
    for name, weight in CATEGORY_WEIGHTS.items():
        found = next((c for c in categories if c.get("name") == name), None)
        avg = (found or {}).get("category_avg_preliminary")
        row = {"category": name, "weight": weight,
               "in_scope": name in in_scope, "score": avg}
        if avg is None:
            row["na_reason"] = ("вне области модуля" if name not in in_scope
                                else "ни одна линза категории не оценена")
        else:
            product = round(float(avg) * weight, 4)
            row["product"] = product
            weighted += product
            weights += weight
        rows.append(row)

    total_weight = sum(CATEGORY_WEIGHTS.values())
    overall = round(weighted / weights, 3) if weights else None
    return {
        "overall": overall,
        "rows": rows,
        "sum_weighted": round(weighted, 3),
        "sum_weights": round(weights, 3),
        "weight_total": round(total_weight, 3),
        "weight_covered": round(weights / total_weight, 3) if total_weight else 0.0,
        "passing_score": PASSING_SCORE,
        # Порог из ТЗ генератора: «балл < 6 — модуль на перегенерацию».
        # Тот же PASSING_SCORE, что у синтезатора, — шкала одна на два сервиса.
        "passed": None if overall is None else overall >= PASSING_SCORE,
        "categories_scored": sum(1 for r in rows if r["score"] is not None),
        "categories_in_scope": len(in_scope),
    }


# --- один проход -------------------------------------------------------------
def evaluate(phase: str, module: dict, params: dict, audit: dict,
             provider_name: str = None) -> dict:
    """Полная оценка модуля: проверка условия, вызов агента, балл.

    Возвращает словарь, пригодный для показа как есть. Ключ `ready=False`
    означает, что линзы не звали — это НЕ ошибка, а штатный исход.
    """
    gate = should_run(audit)
    if not gate["ready"]:
        return {"ready": False, "reason": gate["reason"],
                "blocking": gate.get("blocking") or {}}

    scope = lens_scope.scope_for(phase, params)
    if not scope["core"]:
        return {"ready": False,
                "reason": f"для модуля «{phase}» область линз не описана "
                          f"(известны: {', '.join(lens_scope.known_phases())})",
                "blocking": {}}

    data = build_input(phase, module, params, audit)
    result = lens_evaluator.run(data, provider_name=provider_name, scope=scope,
                                message=build_message(data, scope))
    if not result.get("available"):
        return {"ready": True, "available": False,
                "error": result.get("error"), "raw": result.get("raw")}

    report = result["report"]
    return {
        "ready": True,
        "available": True,
        "phase": phase,
        "score": score(report, scope),
        "report": report,
        "issues": result.get("issues") or [],
        "scope": {"categories": sorted(scope["core"]), "lenses": scope["lenses"],
                  "educational": scope["educational"]},
        "provider": result.get("provider"),
    }
