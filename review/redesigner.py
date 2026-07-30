"""Агент «Авто-редизайнер» (v2) — минимальная правка параметров по найденным недочётам.

Пятый шаг ветки анализа, и первый, который вызывается УСЛОВНО: только когда в
находках есть критичное. Нет критичного — агента не зовут вовсе, игра идёт
дальше как есть. Условие вызова считает код (`should_trigger`), а не модель:
запуск правки — действие с последствиями (структура игры меняется, скелет и
статистика становятся недействительными), и решать «надо ли» по формулировке
из ответа LLM было бы неправильно.

Три режима определяются составом входа:
  A — только Finding_balance (после «Оценщика статистик»);
  D — + Findings_diagnost, чинятся ТОЛЬКО critical (диагност не реализован);
  B — + Findings_lenses и критика синтеза (линзы и синтез не реализованы).
Сейчас в конвейере достижим только режим A; определение режима сделано общим,
чтобы D и B включились без переписывания, когда появятся их агенты.

Дисциплина агента — лестница вмешательства: ст.1 подстройка числа, ст.2
вычитание мёртвого/вредного элемента, ст.3 (добавление нового) ЗАПРЕЩЕНА и
уходит в рекомендации автору.

Валидация здесь особенно нужна: правка применяется к структуре игры, и три
нарушения не проявятся до следующей итерации — расширение закрытого контракта
game_spec, рассинхрон actions_resolution с core.turn.actions (бесшумно обнуляет
классификацию) и правка по «мягкому» числу (чинит игру по цифре, которая
ничего не измеряла).
"""

import json
import re

from review import prompts
from review.extractor import CORE_FIELDS, TEXT_FIELDS
from review.llm_provider import get_provider

MODE_A = "A_technical"
MODE_D = "D_diagnostic"
MODE_B = "B_full_critique"

# Больше четырёх правок за вызов запрещено: не поймёшь, что сработало.
MAX_CHANGES = 4

# Флаги, которые НИКОГДА не чинятся автоматически (нужно новое правило).
NEVER_AUTO = ("coalition_effect", "kingmaking")


def _extract_json(raw_text: str):
    raw_text = (raw_text or "").strip()
    for candidate in (
        re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", raw_text),
        re.search(r"(\{[\s\S]*\})", raw_text),
    ):
        if candidate:
            try:
                return json.loads(candidate.group(1))
            except json.JSONDecodeError:
                continue
    return None


# --- когда агента вообще зовут ----------------------------------------------
def critical_flags(finding_balance: dict) -> list:
    """Флаги тяжести critical — единственное, что запускает режим A.

    Мягкие числа отсеиваются здесь же: по правилам промпта тяжесть critical на
    `assumed` недостижима в принципе, и такой флаг не может быть поводом
    трогать структуру игры.
    """
    flags = (finding_balance or {}).get("flags") or []
    return [f for f in flags
            if f.get("severity") == "critical" and f.get("confidence") != "assumed"]


def actionable_flags(finding_balance: dict) -> list:
    """Флаги, по которым правка вообще возможна: measured и не социальные."""
    flags = (finding_balance or {}).get("flags") or []
    return [f for f in flags
            if f.get("confidence") != "assumed" and f.get("code") not in NEVER_AUTO]


def should_trigger(finding_balance: dict) -> dict:
    """Нужен ли вызов авто-редизайнера и почему. Решение принимает код.

    Возвращает {"trigger": bool, "reason": str, "critical": [...]}.
    """
    crit = critical_flags(finding_balance)
    if crit:
        codes = ", ".join(sorted({f.get("code") for f in crit if f.get("code")}))
        return {"trigger": True, "critical": crit,
                "reason": f"есть критичные флаги: {codes}"}

    flags = (finding_balance or {}).get("flags") or []
    assumed_crit = [f for f in flags
                    if f.get("severity") == "critical" and f.get("confidence") == "assumed"]
    if assumed_crit:
        return {"trigger": False, "critical": [],
                "reason": "критичные флаги есть, но все на «мягких» числах — "
                          "правка по артефакту допущения запрещена, находки идут в рекомендации"}
    if flags:
        return {"trigger": False, "critical": [],
                "reason": "критичных флагов нет — правка структуры не требуется"}
    return {"trigger": False, "critical": [], "reason": "флагов нет"}


def detect_mode(finding_balance: dict = None, findings_diagnost: list = None,
                findings_lenses: list = None, synthesis_critique=None) -> str:
    """Режим определяется фактическим составом входа, а не догадкой."""
    if findings_lenses or synthesis_critique:
        return MODE_B
    if findings_diagnost:
        return MODE_D
    return MODE_A


def collect_previous_changes(attempts: list) -> list:
    """История уже применённых правок — защита от осцилляции.

    Берутся только ПРИНЯТЫЕ попытки: отклонённое предложение ничего в игре не
    сдвинуло, и запрещать по нему повторную правку было бы неверно.
    """
    history = []
    for att in attempts or []:
        for ch in att.get("changes") or []:
            history.append({
                "attempt": att.get("attempt_number"),
                "field": ch.get("field"),
                "from": ch.get("from"),
                "to": ch.get("to"),
                "op": ch.get("op"),
                "element": ch.get("element"),
                "addresses": ch.get("addresses"),
            })
    return history


def moved_fields(previous_changes: list) -> set:
    return {c.get("field") for c in previous_changes or [] if c.get("field")}


def _build_user_message(spec_root: dict, finding_balance: dict, attempt_number: int,
                        previous_changes: list, mode: str,
                        findings_diagnost: list = None,
                        findings_lenses: list = None,
                        synthesis_critique=None) -> str:
    spec = (spec_root or {}).get("game_spec") or {}
    parts = [
        f"=== РЕЖИМ: {mode} ===",
        {
            MODE_A: "На входе только Finding_balance. Чини ТОЛЬКО то, что видят шесть "
                    "проверок статистики. Данных диагноста и линз нет — не выдумывай их.",
            MODE_D: "На входе есть Findings_diagnost. Чини ТОЛЬКО находки тяжести critical; "
                    "major и minor отложи в not_touched.",
            MODE_B: "На входе полная критика. Работай по приоритетам синтеза, оставаясь "
                    "в рамках лестницы вмешательства.",
        }[mode],
        "",
        f"=== ПОПЫТКА № {attempt_number} ===",
        "",
        "=== GAME_SPEC (core + text) ===",
        json.dumps(spec, ensure_ascii=False, indent=2),
        "",
        "=== DIAGNOSTIC_META ===",
        json.dumps((spec_root or {}).get("diagnostic_meta") or {}, ensure_ascii=False, indent=2),
        "",
        "=== FINDING_BALANCE.JSON ===",
        json.dumps(finding_balance or {}, ensure_ascii=False, indent=2),
    ]

    if findings_diagnost:
        parts += ["", "=== FINDINGS_DIAGNOST.JSON ===",
                  json.dumps(findings_diagnost, ensure_ascii=False, indent=2)]
    if findings_lenses:
        parts += ["", "=== FINDINGS_LENSES.JSON ===",
                  json.dumps(findings_lenses, ensure_ascii=False, indent=2)]
    if synthesis_critique:
        parts += ["", "=== КРИТИКА СИНТЕЗА ===",
                  json.dumps(synthesis_critique, ensure_ascii=False, indent=2)]

    parts += ["", "=== PREVIOUS_CHANGES (уже применённые правки) ===",
              json.dumps(previous_changes or [], ensure_ascii=False, indent=2)]
    already = sorted(moved_fields(previous_changes))
    if already:
        parts.append(
            "ВНИМАНИЕ: эти поля уже двигали — " + ", ".join(already) + ". "
            "Возвращать их назад и двигать в противоположную сторону ЗАПРЕЩЕНО. "
            "Возьми другой рычаг для того же флага или отправь находку в "
            "handed_to_recommendations с пометкой, что параметрическая правка не сходится.")

    # Мягкие числа перечисляем явно: это самая частая причина бессмысленной правки.
    assumed = sorted({f.get("code") for f in (finding_balance or {}).get("flags") or []
                      if f.get("confidence") == "assumed" and f.get("code")})
    if assumed:
        parts += ["",
                  "=== ФЛАГИ НА МЯГКИХ ЧИСЛАХ (правка ЗАПРЕЩЕНА) ===",
                  ", ".join(assumed)
                  + ". Эти находки целиком уходят в handed_to_recommendations с пояснением, "
                    "почему автоматическая правка неуместна."]
    return "\n".join(parts)


# --- самопроверка ------------------------------------------------------------
def _issue(code: str, message: str, severity: str = "warning") -> dict:
    return {"code": code, "message": message, "severity": severity}


def _walk_fields(prefix: str, value, out: set):
    """Плоский список путей полей — чтобы найти НОВЫЕ поля в контракте."""
    if isinstance(value, dict):
        for k, v in value.items():
            path = f"{prefix}.{k}" if prefix else k
            out.add(path)
            _walk_fields(path, v, out)


def validate(result: dict, spec_root: dict, finding_balance: dict,
             previous_changes: list = None, mode: str = MODE_A,
             findings_diagnost: list = None) -> tuple:
    """Самопроверка из промпта, выполненная кодом. Возвращает (result, issues).

    Ничего не «чинит»: правка — это решение агента, и подменять его кодом
    значило бы редактировать игру от себя. Задача — не дать молчаливому
    нарушению уехать в структуру игры.
    """
    issues = []
    result = dict(result or {})
    changes = result.get("changes") or []
    revised = result.get("revised_game_spec") or {}
    old_spec = (spec_root or {}).get("game_spec") or {}

    # 1) режим определён по входу, а не по вкусу
    if result.get("mode") and result["mode"] != mode:
        issues.append(_issue(
            "mode_mismatch",
            f"Агент объявил режим {result['mode']}, а по составу входа это {mode}.", "error"))

    # 2) не больше четырёх правок
    if len(changes) > MAX_CHANGES:
        issues.append(_issue(
            "too_many_changes",
            f"Правок {len(changes)} — больше {MAX_CHANGES}. При таком объёме нельзя "
            "понять, какая из них сработала."))

    # 3) каждая правка привязана к флагу из входа, и ни одна — к мягкому числу
    flags = {f.get("code"): f for f in (finding_balance or {}).get("flags") or []}
    diag = {f.get("code"): f for f in findings_diagnost or []}
    known = set(flags) | set(diag)
    assumed = {c for c, f in flags.items() if f.get("confidence") == "assumed"}

    for ch in changes:
        addresses = (ch.get("addresses") or "").strip()
        field = ch.get("field") or "?"
        if not addresses:
            issues.append(_issue(
                "change_without_source",
                f"Правка «{field}» не привязана ни к одному флагу. Нет недочёта — нет правки.",
                "error"))
            continue
        hit = {code for code in known if code and code in addresses}
        if not hit:
            issues.append(_issue(
                "change_flag_unknown",
                f"Правка «{field}» ссылается на «{addresses}», но такого флага нет во "
                "входных находках.", "error"))
        if hit & assumed:
            issues.append(_issue(
                "change_on_assumed",
                f"Правка «{field}» сделана по флагу на мягком числе "
                f"({', '.join(sorted(hit & assumed))}). Цифра отражает допущение симулятора, "
                "а не поведение игроков — такие находки идут в рекомендации.", "error"))
        if hit & set(NEVER_AUTO):
            issues.append(_issue(
                "change_on_social",
                f"Правка «{field}» чинит социальное явление "
                f"({', '.join(sorted(hit & set(NEVER_AUTO)))}) — это всегда новое правило "
                "и решение автора.", "error"))
        if ch.get("ladder") == 3:
            issues.append(_issue(
                "ladder_three",
                f"Правка «{field}» объявлена ступенью 3 — добавление нового запрещено, "
                "это рекомендация автору.", "error"))
        if ch.get("ladder") == 2 and not ch.get("subtraction_test"):
            issues.append(_issue(
                "subtraction_unmarked",
                f"Вычитание элемента «{field}» не помечено subtraction_test: true."))

        # 4) осцилляция: то же поле возвращают к прежнему значению
        for prev in previous_changes or []:
            if prev.get("field") != ch.get("field"):
                continue
            if ch.get("to") is not None and ch.get("to") == prev.get("from"):
                issues.append(_issue(
                    "oscillation",
                    f"Поле «{field}» возвращается к значению {ch.get('to')!r}, которое уже "
                    f"меняли на попытке {prev.get('attempt')}. Колебание между двумя "
                    "значениями тратит попытки впустую — нужен другой рычаг.", "error"))

    # 5) закрытый контракт: новых полей быть не может
    if revised:
        new_core = set((revised.get("core") or {}))
        new_text = set((revised.get("text") or {}))
        for extra in sorted(new_core - set(CORE_FIELDS)):
            issues.append(_issue(
                "core_extra_field",
                f"В core появилось поле «{extra}» — контракт разделяемый, добавлять поля "
                "нельзя.", "error"))
        for extra in sorted(new_text - set(TEXT_FIELDS)):
            issues.append(_issue(
                "text_extra_field", f"В text появилось поле «{extra}» — контракт закрыт.",
                "error"))

        old_paths, new_paths = set(), set()
        _walk_fields("", old_spec, old_paths)
        _walk_fields("", revised, new_paths)
        # Внутри core/text структура тоже фиксирована; интересуют именно НОВЫЕ пути.
        added = {p for p in new_paths - old_paths if p.count(".") <= 2}
        if added:
            issues.append(_issue(
                "spec_paths_added",
                "Появились новые пути в структуре: " + ", ".join(sorted(added)[:6])
                + ". Менять можно только значения существующих полей."))

    # 6) три слоя согласованы: actions_resolution ↔ core.turn.actions
    meta = result.get("revised_diagnostic_meta")
    if revised and meta is not None:
        actions = list(((revised.get("core") or {}).get("turn") or {}).get("actions") or [])
        resolution = dict((meta or {}).get("actions_resolution") or {})
        missing = [a for a in actions if a not in resolution]
        extra = [k for k in resolution if k not in actions]
        if missing:
            issues.append(_issue(
                "resolution_missing",
                "После правки действия остались без классификации: " + ", ".join(missing)
                + ". Рассинхрон ничего не уронит, но бесшумно обнулит классификацию на "
                  "следующей итерации.", "error"))
        if extra:
            issues.append(_issue(
                "resolution_extra",
                "В actions_resolution остались отключённые действия: " + ", ".join(extra)
                + ". При отключении действия ключ убирается из всех трёх слоёв.", "error"))
        targeted = ((meta or {}).get("targeted_actions") or {}).get("actions") or []
        stale = [a for a in targeted if a not in actions]
        if stale:
            issues.append(_issue(
                "targeted_stale",
                "В targeted_actions остались отсутствующие действия: " + ", ".join(stale) + "."))

    # 7) замысел не ломается ради баланса
    old_core = old_spec.get("core") or {}
    new_core_obj = revised.get("core") or {}
    if new_core_obj:
        if old_core.get("mode") and new_core_obj.get("mode") != old_core.get("mode"):
            issues.append(_issue(
                "intent_mode_changed",
                f"Режим игры изменён с «{old_core.get('mode')}» на "
                f"«{new_core_obj.get('mode')}» — замысел ломать нельзя.", "error"))
        if (old_core.get("elimination") is not None
                and new_core_obj.get("elimination") != old_core.get("elimination")):
            issues.append(_issue(
                "intent_elimination_changed",
                "Изменена политика выбывания — это часть замысла, а не параметр баланса.",
                "error"))

    # 8) режим D чинит только critical
    if mode == MODE_D:
        non_critical = {c: f for c, f in diag.items() if f.get("severity") != "critical"}
        for ch in changes:
            addresses = ch.get("addresses") or ""
            hit = {c for c in non_critical if c and c in addresses}
            if hit:
                issues.append(_issue(
                    "mode_d_touches_non_critical",
                    f"Правка «{ch.get('field')}» чинит находку тяжести не critical "
                    f"({', '.join(sorted(hit))}) — в режиме D это запрещено.", "error"))

    # 9) итог всегда требует пересимуляции
    if result.get("needs_resimulation") is not True:
        issues.append(_issue(
            "needs_resimulation_missing",
            "Не выставлен needs_resimulation: true — результат правки подтверждает только "
            "повторный прогон."))

    # 10) ничего не потеряно: мягкие и социальные находки должны быть в рекомендациях
    handed = json.dumps(result.get("handed_to_recommendations") or [], ensure_ascii=False)
    for code in sorted(assumed | {c for c in flags if c in NEVER_AUTO}):
        if code not in handed:
            issues.append(_issue(
                "finding_lost",
                f"Находка «{code}» не чинится автоматически, но и не передана в "
                "handed_to_recommendations — она просто исчезнет."))

    return result, issues


def run(spec_root: dict, finding_balance: dict, attempt_number: int = 1,
        previous_changes: list = None, findings_diagnost: list = None,
        findings_lenses: list = None, synthesis_critique=None,
        provider_name: str = None, cache_dir: str = None) -> dict:
    """Один шаг авто-редизайнера. Цикл ведёт оркестратор, агент круг не решает.

    Возвращает {available, result, issues, mode, raw, provider} либо
    {available: False, error}.
    """
    mode = detect_mode(finding_balance, findings_diagnost, findings_lenses, synthesis_critique)
    system = prompts.load_redesigner_prompt()
    user = _build_user_message(spec_root, finding_balance, attempt_number,
                               previous_changes, mode, findings_diagnost,
                               findings_lenses, synthesis_critique)

    provider = get_provider(provider_name, cache_dir=cache_dir)
    resp = provider.complete(system, user)
    if not resp.available:
        return {"available": False, "error": resp.error}

    result = _extract_json(resp.text)
    if result is None:
        return {"available": False,
                "error": "Авто-редизайнер вернул ответ, который не удалось разобрать как JSON.",
                "raw": resp.text}

    result, issues = validate(result, spec_root, finding_balance,
                              previous_changes, mode, findings_diagnost)
    return {
        "available": True,
        "result": result,
        "issues": issues,
        "mode": mode,
        "raw": resp.text,
        "provider": resp.provider,
        "cached": resp.cached,
    }


def apply_to_spec(spec_root: dict, result: dict) -> dict:
    """Собирает новую версию структуры из ответа агента.

    Пробелы, неоднозначности и формат источника переносятся как есть: агент их
    не пересматривает, он редактор параметров, а не извлеченец.
    """
    updated = dict(spec_root or {})
    revised = result.get("revised_game_spec")
    if revised:
        updated["game_spec"] = revised
    meta = result.get("revised_diagnostic_meta")
    if meta is not None:
        updated["diagnostic_meta"] = meta
    return updated
