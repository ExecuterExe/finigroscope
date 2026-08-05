# -*- coding: utf-8 -*-
"""Оркестратор одного модуля: сгенерировать → проверить → оценить → повторить.

По ТЗ генератора («Если балл < 6 — модуль отправляется на перегенерацию, до N
попыток», и далее «оркестратор сортирует все сгенерированные варианты по
полученному баллу и выбирает лучший»).

Один проход попытки:

    генератор модуля (модель) → аудитор (модель) → линзы Шелла (ФинИгроСкоп)

Попыток до трёх. Останавливаемся раньше, как только балл достиг порога: платить
за две лишние попытки, когда результат уже годный, незачем. Если все три ниже
порога — берём попытку с НАИБОЛЬШИМ баллом и честно говорим, что порог не взят.
Выбрасывать наработанное только потому, что оно не дотянуло, нельзя: лучший из
трёх — это лучшее, что у нас есть, и решать по нему автору.

Почему оценивается один вариант из попытки, а не все. Генератор возвращает по
несколько вариантов за вызов, и оценить каждый значило бы девять обращений к
линзам вместо трёх. Берём тот, который сам генератор пометил рекомендованным, —
это его собственное суждение, и не доверять ему на этом шаге не за что.

Что здесь НЕ делается. Попытка, сорвавшаяся на аудите (нарушения чек-листа), до
линз не доходит и балла не получает — но в отчёт попадает. Иначе «сделано три
попытки, показываем одну» выглядит как потеря, хотя произошло ровно то, что
задумано.

Модули идут цепочкой (таблица 9 ТЗ): механики → сюжет → особенности, и каждый
следующий вызывается, только когда предыдущий принят. Различий между проходами
ровно три — чем генерируется модуль, с чем его сверяет аудитор и как называется
шаг на экране, — поэтому ход прохода общий, а различия собраны в описании фазы.
"""

from agents import features
from agents import lens_review
from agents import mechanics
from agents import module_auditor
from agents import story

# Сколько раз пробуем собрать модуль, дотягивающий до порога.
MAX_ATTEMPTS = 3

# Порог приёмки. Тот же, что у оценщика по линзам в ФинИгроСкопе (PASSING_SCORE)
# и тот же, что назван в ТЗ генератора. Держится здесь ЧИСЛОМ намеренно: линзы
# присылают своё значение в каждом ответе, и если они разойдутся — это видно
# сразу (см. _threshold_mismatch), а не молча меняет правила приёмки.
PASSING_SCORE = 6.0


class PipelineError(Exception):
    """Проход не удался целиком. Текст пригоден для показа пользователю."""


def _blocking(audit):
    """Критичные замечания аудитора: с ними линзы не зовут."""
    violations = [r for r in (audit.get("map") or [])
                  if r.get("status") == module_auditor.STATUS_VIOLATION]
    critical = [i for i in (audit.get("issues") or [])
                if i.get("severity") == module_auditor.SEVERITY_CRITICAL]
    return violations, critical


def _threshold_mismatch(score):
    """Разошёлся ли наш порог с тем, что прислали линзы.

    Молчаливое расхождение хуже любого из двух значений: приёмка шла бы по
    одному числу, а показывалось бы другое.
    """
    theirs = (score or {}).get("passing_score")
    if theirs is None or abs(float(theirs) - PASSING_SCORE) < 1e-9:
        return None
    return ("Порог приёмки разошёлся: у генератора %.3f, у оценщика по линзам "
            "%.3f. Приняли решение по значению оценщика." % (PASSING_SCORE, theirs))


# --------------------------------------------------------------------------
# Проходы
# --------------------------------------------------------------------------

def run(params, progress, attempts=MAX_ATTEMPTS):
    """Проход модуля механик — первый в цепочке, предыдущих модулей нет."""
    return _run_module(
        phase="mechanics",
        step_label="генерация механик",
        step_detail="модель собирает варианты игрового цикла",
        generate=lambda: mechanics.generate(params),
        fatal=(mechanics.NotEnoughMechanics,
               "Библиотека механик не покрывает выбранные параметры: %s"),
        params=params, progress=progress, attempts=attempts,
        previous_modules=None, scored=True)


def run_story(params, progress, mechanics_module, attempts=MAX_ATTEMPTS,
              built_on=None):
    """Проход модуля сюжета — второй в цепочке (этап 3 ТЗ).

    `mechanics_module` — ПРИНЯТЫЙ модуль механик, то есть cleaned_module его
    аудита. Контракт задан ModuleChain и повторён здесь намеренно: сырой вывод
    генератора механик подавать сюда нельзя, сюжет опёрся бы на вариант, который
    аудитор мог поправить.

    Оценка по линзам пропускается для абстрактной игры (ответ «сюжет не
    требуется» на вопрос 12). Не из экономии: область линз этого прохода —
    «Реиграбельность и нарратив» и смежные категории (см. lens_scope в
    ФинИгроСкопе). Оценивать нарратив модуля, у которого его нет по требованию
    пользователя, значит наказывать за выполненное требование.

    `built_on` — СПИСОК оснований: на каких модулях построен проход, каковы их
    баллы и приняты ли они. Едет в итог целиком и НЕ молчит: этап поверх
    непринятого модуля — законный выбор автора, но по готовому результату
    догадаться об этом невозможно, а замечания никуда не делись и перешли в
    игру вместе с модулем. Список, а не одна запись, потому что дальше по
    цепочке оснований становится несколько, и умолчать об одном из них нельзя.
    """
    depth = story.depth_of(params)
    previous = [{"phase": "mechanics", "module": mechanics_module}]

    return _run_module(
        phase="story",
        step_label="генерация сюжета",
        step_detail="модель придумывает название, историю и имена артефактов",
        generate=lambda: story.generate(params, mechanics_module),
        fatal=(story.NotEnoughSeeds,
               "Библиотека сюжетов не покрывает выбранные параметры: %s"),
        params=params, progress=progress, attempts=attempts,
        previous_modules=previous,
        scored=depth != story.DEPTH_NONE,
        skip_reason=("игра абстрактная — сюжета в ней нет по ответу на вопрос 12, "
                     "а оценивать нарратив там, где его не должно быть, нельзя"),
        built_on=built_on)


def run_features(params, progress, mechanics_module, story_module,
                 attempts=MAX_ATTEMPTS, built_on=None):
    """Проход модуля особенностей — третий и последний в цепочке (этап 4 ТЗ).

    Оба предыдущих модуля — ПРИНЯТЫЕ (cleaned_module своих аудитов) и оба уходят
    аудитору как основание сверки: у этого прохода `consistency_against` в
    конфиге перечисляет и механики, и сюжет. Порядок в списке значим —
    аудитор читает его как порядок этапов.

    Балл здесь есть всегда: особенности в игре есть при любых ответах опросника,
    даже когда все они сводятся к «никто не выбывает».
    """
    previous = [{"phase": "mechanics", "module": mechanics_module},
                {"phase": "story", "module": story_module}]

    return _run_module(
        phase="features",
        step_label="генерация особенностей",
        step_detail="модель описывает концепцию, особенности и помощь отстающим",
        generate=lambda: features.generate(params, mechanics_module, story_module),
        fatal=(features.NotEnoughFeatures,
               "Библиотека особенностей не покрывает выбранные параметры: %s"),
        params=params, progress=progress, attempts=attempts,
        previous_modules=previous, scored=True, built_on=built_on)


def _run_module(phase, step_label, step_detail, generate, fatal, params,
                progress, attempts, previous_modules, scored, skip_reason=None,
                built_on=None):
    """Общий ход прохода. Различия между модулями приходят аргументами."""
    fatal_type, fatal_template = fatal
    tried = []
    warnings = []

    warnings.extend(_built_on_warnings(built_on))

    for attempt in range(1, attempts + 1):
        progress.check_cancelled()
        progress.say(step_label, attempt=attempt, attempts_total=attempts,
                     detail=step_detail)

        try:
            generated = generate()
        except fatal_type as error:
            # Библиотека не покрывает параметры. Повтор не поможет: следующая
            # попытка упрётся в то же самое, только за деньги.
            raise PipelineError(fatal_template % error) from error
        except lens_review.LensError as error:                # noqa: PERF203
            raise PipelineError(str(error)) from error

        if not generated.get("ok"):
            tried.append(_failed_attempt(
                attempt, "генерация",
                "модель не собрала годный вариант",
                problems=generated.get("problems")))
            continue

        variant = _pick_variant(generated)
        if variant is None:
            tried.append(_failed_attempt(attempt, "генерация",
                                         "в ответе нет вариантов"))
            continue

        progress.check_cancelled()
        progress.say("аудит модуля", attempt=attempt, attempts_total=attempts,
                     detail="сверка с ответами опросника по чек-листу")

        try:
            audit = module_auditor.audit_module(
                phase, variant, params,
                previous_modules=previous_modules).to_dict()
        except module_auditor.AuditError as error:
            tried.append(_failed_attempt(attempt, "аудит", str(error)))
            continue

        violations, critical = _blocking(audit)
        if violations or critical:
            names = sorted({r.get("item") for r in violations}
                           | {i.get("checklist_item") for i in critical})
            tried.append(_failed_attempt(
                attempt, "аудит",
                "критичные замечания: " + ", ".join(n for n in names if n),
                variant=variant, audit=audit))
            progress.add_attempt(tried[-1])
            continue

        if not scored:
            # Аудит чистый, а оценивать нечего. Это законный успех прохода, а не
            # его усечённая версия: результат принят, просто балла у него нет.
            row = _passed_without_score(attempt, variant, audit, skip_reason)
            tried.append(row)
            progress.add_attempt(_short(row))
            break

        progress.check_cancelled()
        progress.say("оценка по линзам", attempt=attempt, attempts_total=attempts,
                     detail="агент ФинИгроСкопа разбирает модуль по линзам Шелла")

        lens = lens_review.evaluate(phase, variant, params, audit)
        if not lens.get("ready"):
            # Линзы отказались считать. Это уже проверено выше, поэтому сюда
            # попасть можно только при расхождении правил — и молчать об этом
            # нельзя.
            tried.append(_failed_attempt(attempt, "линзы",
                                         lens.get("reason") or "оценка не выполнена",
                                         variant=variant, audit=audit))
            progress.add_attempt(tried[-1])
            continue
        if not lens.get("available"):
            raise PipelineError(lens.get("error") or "Модель не ответила.")

        score = lens.get("score") or {}
        mismatch = _threshold_mismatch(score)
        if mismatch and mismatch not in warnings:
            warnings.append(mismatch)

        row = {
            "attempt": attempt,
            "ok": True,
            "stage": "линзы",
            "variant_id": variant.get("variant_id"),
            "title": variant.get("title"),
            "score": score.get("overall"),
            "passed": bool(score.get("passed")),
            "weight_covered": score.get("weight_covered"),
            "variant": variant,
            "audit": audit,
            "lens": lens,
        }
        tried.append(row)
        progress.add_attempt(_short(row))

        if row["passed"]:
            break

    return _finish(phase, tried, warnings, attempts, scored, skip_reason, built_on)


def _built_on_warnings(built_on):
    """Предупреждения о том, что этап построен на непринятых модулях.

    Стоят ПЕРВЫМИ в списке и появляются даже у безупречного результата. Это не
    придирка: модуль может взять свои 8 из 10 и всё равно стоять на механиках с
    баллом 4 — и по итоговой карточке этого не увидеть никак. Право автора идти
    дальше ТЗ ему даёт (этапы 2–6, пункт 3), а вот право забыть об этом — нет.

    По одной записи на КАЖДОЕ непринятое основание: у этапа особенностей их два,
    и сказать про одно, умолчав о втором, было бы хуже, чем промолчать про оба.
    """
    notes = []
    for base in built_on or []:
        if not base or base.get("accepted", True):
            continue
        score = base.get("score")
        notes.append(
            "Этап построен на НЕПРИНЯТОМ модуле «%s»%s — по вашему решению. "
            "Замечания к нему никуда не делись и перешли в игру вместе с ним."
            % (base.get("phase") or "предыдущий",
               "" if score is None else " (балл %s при пороге %s)"
               % (score, base.get("threshold", PASSING_SCORE))))
    return notes


def _pick_variant(generated):
    """Вариант, который сам генератор пометил рекомендованным."""
    data = generated.get("data") or {}
    variants = data.get("variants") or []
    if not variants:
        return None
    wanted = data.get("recommended_variant_id")
    for variant in variants:
        if variant.get("variant_id") == wanted:
            return variant
    return variants[0]


def _failed_attempt(attempt, stage, reason, problems=None, variant=None, audit=None):
    return {"attempt": attempt, "ok": False, "stage": stage, "reason": reason,
            "problems": problems or [], "score": None, "passed": False,
            "variant": variant, "audit": audit,
            "variant_id": (variant or {}).get("variant_id"),
            "title": (variant or {}).get("title")}


def _passed_without_score(attempt, variant, audit, reason):
    return {"attempt": attempt, "ok": True, "stage": "аудит",
            "variant_id": variant.get("variant_id"), "title": variant.get("title"),
            "score": None, "passed": True, "unscored_reason": reason,
            "variant": variant, "audit": audit, "lens": None}


def _short(row):
    """Строка для показа хода работы — без тяжёлых вложенных отчётов."""
    return {k: row.get(k) for k in
            ("attempt", "ok", "stage", "variant_id", "title", "score", "passed",
             "reason", "weight_covered", "unscored_reason")}


def _finish(phase, tried, warnings, attempts, scored, skip_reason, built_on=None):
    if not scored:
        return _finish_unscored(phase, tried, warnings, attempts, skip_reason,
                                built_on)

    done = [r for r in tried if r.get("score") is not None]
    if not done:
        raise PipelineError(_no_result_message(tried, attempts, "не дошла до оценки"))

    # Лучший — по баллу. При равенстве берём тот, что получен раньше: он дешевле
    # достался, и предпочитать поздний не за что.
    best = max(done, key=lambda r: (r["score"], -r["attempt"]))

    return {
        "ok": True,
        "phase": phase,
        "scored": True,
        "built_on": built_on,
        "attempts_made": len(tried),
        "attempts_allowed": attempts,
        "passed": best["passed"],
        "threshold": PASSING_SCORE,
        "best": best,
        "attempts": [_short(r) for r in tried],
        "warnings": warnings,
        # Честная формулировка итога: разница между «прошло» и «взяли лучшее из
        # непрошедших» принципиальна, и прятать её за одним баллом нельзя.
        "verdict": (
            "Модуль принят: балл %.3f при пороге %.1f." % (best["score"], PASSING_SCORE)
            if best["passed"] else
            "Порог не взят ни одной из %d попыток. Показан лучший результат — "
            "%.3f из 10 (попытка %d). Это не готовый модуль, а лучшее из "
            "полученного: решать, дорабатывать его или менять ответы опросника, "
            "вам." % (len(tried), best["score"], best["attempt"])
        ),
    }


def _finish_unscored(phase, tried, warnings, attempts, skip_reason, built_on=None):
    """Итог прохода, у которого балла не бывает по устройству, а не по сбою."""
    done = [r for r in tried if r.get("ok")]
    if not done:
        raise PipelineError(_no_result_message(tried, attempts, "не прошла аудит"))

    best = done[0]
    return {
        "ok": True,
        "phase": phase,
        "scored": False,
        "built_on": built_on,
        "unscored_reason": skip_reason,
        "attempts_made": len(tried),
        "attempts_allowed": attempts,
        "passed": True,
        "threshold": PASSING_SCORE,
        "best": best,
        "attempts": [_short(r) for r in tried],
        "warnings": warnings,
        "verdict": ("Модуль принят аудитором. Балла у него нет: %s."
                    % (skip_reason or "оценка по линзам для этого прохода "
                                      "не выполняется")),
    }


def _no_result_message(tried, attempts, what):
    return ("Ни одна из %d попыток %s. Причины: %s"
            % (len(tried) or attempts, what,
               "; ".join("попытка %d — %s (%s)"
                         % (r["attempt"], r.get("reason", "?"), r.get("stage", "?"))
                         for r in tried) or "неизвестны"))
