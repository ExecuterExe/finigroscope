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

import llm

from agents import features
from agents import lens_review
from agents import mechanics
from agents import module_auditor
from agents import packaging
from agents import rules
from agents import story
from agents import verdict

# Сколько раз пробуем собрать модуль, дотягивающий до порога.
MAX_ATTEMPTS = 3

# Порог приёмки. Тот же, что у оценщика по линзам в ФинИгроСкопе (PASSING_SCORE)
# и тот же, что назван в ТЗ генератора. Держится здесь ЧИСЛОМ намеренно: линзы
# присылают своё значение в каждом ответе, и если они разойдутся — это видно
# сразу (см. _threshold_mismatch), а не молча меняет правила приёмки.
PASSING_SCORE = 6.0


class PipelineError(Exception):
    """Проход не удался целиком. Текст пригоден для показа пользователю.

    Метка `user_facing` — не украшение: по ней очередь (jobs.describe_failure)
    отличает объяснение, написанное для человека, от случайного исключения, в
    тексте которого может оказаться кусок промпта или путь на диске. Без метки
    вычисленная причина отбрасывалась, и пользователь видел «Проход не выполнен:
    PipelineError» — то есть ничего.
    """

    user_facing = True


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


def run_rules(params, progress, modules, components, attempts=MAX_ATTEMPTS,
              built_on=None):
    """Проход кратких правил и советов — четвёртый и последний (этап 6 ТЗ).

    `modules` — словарь принятых cleaned_module трёх предыдущих этапов.
    `components` — строки расчёта этапа 5 после второго прохода, с материалами.
    Считает их КОД, а не агент, поэтому в цепочку оснований они не попадают:
    у расчёта по таблицам нет балла, который можно было бы не взять.

    Единственный проход, где оцениваются не замысел, а изложение. Область линз
    здесь — всё ядро (см. lens_scope в ФинИгроСкопе): к этому моменту описание
    игры собрано целиком, и оценивать его больше нечем ограничивать.
    """
    previous = [{"phase": "mechanics", "module": (modules or {}).get("mechanics")},
                {"phase": "story", "module": (modules or {}).get("story")},
                {"phase": "features", "module": (modules or {}).get("features")}]

    return _run_module(
        phase="rules",
        step_label="сборка правил",
        step_detail="модель излагает принятую игру правилами и пишет советы",
        generate=lambda: rules.generate(params, modules, components),
        fatal=(rules.RulesError, "Правила собрать не удалось: %s"),
        params=params, progress=progress, attempts=attempts,
        previous_modules=previous, scored=True, built_on=built_on)


def run_packaging(params, progress, modules, components, rules_variant,
                  built_on=None):
    """Упаковка: полное описание и game_spec.json. Не проход, а сборка.

    Ни аудитора, ни линз здесь нет, и это не упущение. Оценивать нечего:
    описание собирается из уже принятых модулей, а перевод в game_spec
    проверяется собственным валидатором — он сверяет разбор действий с
    действиями, метрику победы с ресурсами и случайность с ответом автора.
    Позвать сюда аудитора значило бы попросить модель оценить, верно ли
    переписано то, что она сама только что переписала.

    Итог намеренно совпадает по форме с проходами в главном: `ok`, `warnings`,
    `built_on` — чтобы страница показывала непринятые основания одним и тем же
    кодом. Отличие одно: балла нет, и поле `scored` честно говорит об этом.
    """
    progress.check_cancelled()
    progress.say("упаковка", attempt=1, attempts_total=1,
                 detail="собираю описание и перевожу механики в game_spec")

    try:
        out = packaging.assemble(params, modules, components, rules_variant)
    except packaging.PackagingError as error:
        raise PipelineError(str(error)) from error
    except llm.LLMError as error:
        # Тот же недосмотр, что был в проходах модулей: ловился только «свой»
        # класс ошибки, а сбой обращения к модели уходил наверх необёрнутым и
        # без указания, ГДЕ он случился. Здесь это особенно дорого: к упаковке
        # автор приходит с четырьмя принятыми модулями.
        raise PipelineError("Упаковка не выполнена: %s" % error) from error

    out.update({
        "phase": "package",
        "scored": False,
        "passed": True,
        "built_on": built_on,
        "attempts_made": out.get("attempts", 1),
        "attempts_allowed": packaging.MAX_ATTEMPTS,
        "attempts": [],
        "verdict": "Игра упакована: полное описание и game_spec.json готовы.",
        "warnings": _built_on_warnings(built_on) + list(out.get("warnings") or []),
    })
    return out


def run_verdict(params, progress, spec_root, built_on=None):
    """Итоговый разбор упакованной игры в ФинИгроСкопе. Последний шаг конвейера.

    Своих попыток здесь нет и быть не может: круги авто-редизайна считает та
    сторона, потому что правит она же — game_spec. Повторять их отсюда значило
    бы завести второй счётчик тех же попыток, и они разошлись бы.
    """
    progress.check_cancelled()
    progress.say("итоговый разбор", attempt=1, attempts_total=1,
                 detail="ФинИгроСкоп проводит игру через симуляционный этап")

    try:
        out = verdict.evaluate(spec_root, progress=progress)
    except verdict.VerdictError as error:
        raise PipelineError(str(error)) from error
    except llm.LLMError as error:
        # Симметрия с остальными шагами. Сам разбор к модели не ходит — он зовёт
        # ФинИгроСкоп, — но полагаться на это значит оставить единственный шаг
        # конвейера, у которого сбой модели уйдёт наверх необёрнутым и без
        # указания, где он случился.
        raise PipelineError("Итоговый разбор не выполнен: %s" % error) from error

    if not out.get("ok"):
        raise PipelineError("Итоговый разбор не выполнен: %s"
                            % (out.get("error") or "причина не названа"))

    out.update({
        "phase": "verdict",
        "scored": True,
        "built_on": built_on,
        # Круги того сервиса показываем как свои попытки — счётчик один и тот же,
        # просто считает его сосед.
        "attempts_made": out.get("rounds_made"),
        "attempts_allowed": out.get("rounds_allowed"),
        "attempts": [_verdict_round(r) for r in out.get("rounds") or []],
        "warnings": _built_on_warnings(built_on) + _verdict_warnings(out),
    })
    return out


def _verdict_round(row):
    """Круг разбора строкой для таблицы попыток — тем же ключом, что у проходов."""
    return {
        "attempt": row.get("attempt"),
        "ok": row.get("score") is not None,
        "stage": "разбор",
        "title": ("правка применена" if row.get("redesign")
                  else "круг без правок"),
        "score": row.get("score"),
        "passed": (row.get("score") or 0) >= PASSING_SCORE,
        "reason": None,
    }


def _verdict_warnings(out):
    """О чём разбор обязан сказать вслух."""
    notes = []
    if out.get("code_execution") is False:
        notes.append(
            "Прогон скелета в ФинИгроСкопе выключен (SIM_API_ALLOW_RUN), поэтому "
            "статистику никто не считал. Это не сбой, а настройка — но балл без "
            "неё неполон.")
    # Предупреждаем только когда прогоны БЫЛИ заказаны и не выполнены. Первая
    # версия смотрела на «выполнены ли» и ругалась там, где диагносту просто
    # хватило основного прогона, — то есть в большинстве обычных случаев.
    skipped = sum((row.get("extra_runs_requested") or 0)
                  for row in out.get("rounds") or []
                  if row.get("extra_runs_skipped"))
    if skipped:
        notes.append(
            "Дополнительных прогонов заказано %d, и они не выполнялись — их "
            "тесты получили честное «не выполнен», а не пропали из отчёта."
            % skipped)

    broke = out.get("broke_on")
    if broke:
        notes.append("Круг %d оборвался: %s Показан лучший из завершённых."
                     % (broke.get("round"), broke.get("reason")))
    return notes


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
        except llm.BadAnswer as error:                         # noqa: PERF203
            # Модель ответила, но ответ негодный: оборван на пределе токенов,
            # пришёл не JSON, испорчен по дороге. Это ровно то, ради чего у
            # прохода есть три попытки, — тратим ОДНУ и идём дальше.
            #
            # Раньше этот случай убивал проход целиком: он ловился вместе с
            # отказом сети одним классом. На модуле особенностей, где ответ
            # самый длинный и обрывается чаще всего, так падали все три попытки
            # подряд, и до аудита дело не доходило ни разу.
            tried.append(_failed_attempt(
                attempt, "генерация", "ответ модели непригоден: %s" % error))
            tried[-1]["phase"] = phase
            progress.add_attempt(_short(tried[-1]))
            continue
        except llm.LLMError as error:
            # Сбой обращения к МОДЕЛИ: сеть, ключ, оплата, таймаут. Раньше здесь
            # ловился lens_review.LensError — тип, которого генерация не бросает
            # вовсе, — и настоящее исключение уходило наверх необёрнутым. Очередь
            # показывала «Проход не выполнен: LLMError», а точный текст
            # («Не удалось связаться с OpenRouter: getaddrinfo failed») оставался
            # только в журнале сервера. Поймано живым прогоном.
            raise PipelineError(
                "Модуль «%s» не собран: %s" % (phase, error)) from error
        except lens_review.LensError as error:
            raise PipelineError(str(error)) from error

        if not generated.get("ok"):
            tried.append(_failed_attempt(
                attempt, "генерация",
                "модель не собрала годный вариант",
                problems=generated.get("problems")))
            tried[-1]["phase"] = phase
            continue

        variant = _pick_variant(generated)
        if variant is None:
            tried.append(_failed_attempt(attempt, "генерация",
                                         "в ответе нет вариантов"))
            tried[-1]["phase"] = phase
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
            tried[-1]["phase"] = phase
            continue

        violations, critical = _blocking(audit)
        if violations or critical:
            names = sorted({r.get("item") for r in violations}
                           | {i.get("checklist_item") for i in critical})
            tried.append(_failed_attempt(
                attempt, "аудит",
                "критичные замечания: " + ", ".join(n for n in names if n),
                variant=variant, audit=audit))
            tried[-1]["phase"] = phase
            progress.add_attempt(_short(tried[-1]))
            continue

        if not scored:
            # Аудит чистый, а оценивать нечего. Это законный успех прохода, а не
            # его усечённая версия: результат принят, просто балла у него нет.
            row = _passed_without_score(attempt, variant, audit, skip_reason)
            row["phase"] = phase
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
            tried[-1]["phase"] = phase
            progress.add_attempt(_short(tried[-1]))
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
        row["phase"] = phase
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
    """Строка попытки для показа — без тяжёлых вложенных отчётов.

    `problems` здесь ОБЯЗАТЕЛЬНЫ, хотя это и список. Причины провала уже
    вычислены — их вернул валидатор агента или аудитор, — и выбрасывать их
    ровно перед показом значит оставить автора со строкой «модель не собрала
    годный вариант», по которой нельзя понять ни что чинить, ни виноват ли он
    сам. Ровно на это и жаловались: три попытки, три одинаковых слова, ноль
    сведений.

    Списки не тяжёлые: это несколько строк текста на попытку, а не отчёты
    аудитора и линз целиком — те остаются в полной строке и на экран не едут.
    """
    short = {k: row.get(k) for k in
             ("attempt", "ok", "stage", "variant_id", "title", "score", "passed",
              "reason", "weight_covered", "unscored_reason", "phase")}
    short["problems"] = list(row.get("problems") or [])

    # САМ МОДУЛЬ непрошедшей попытки тоже едет на экран. Сперва его отсюда
    # вычищали заодно с тяжёлыми отчётами — и зря: причина провала объясняет,
    # ЧТО не так, но не показывает, что именно получилось. Сравнить, чем
    # попытка 1 отличалась от попытки 2, без этого нельзя, а это первое, что
    # хочется сделать, увидев три разных балла.
    #
    # Он не тяжёлый: это тот же вариант, который у лучшей попытки и так уходит
    # целиком. Тяжёлые — отчёты аудитора и линз, и они по-прежнему остаются на
    # сервере: наружу идут только их выжимки (audit_issues, lens_findings).
    if row.get("variant"):
        short["variant"] = row["variant"]

    # У попытки, дошедшей до оценки, «почему не прошло» — это находки линз и
    # замечания аудитора, а не список проблем генерации.
    lens = row.get("lens") or {}
    if lens:
        score = lens.get("score") or {}
        short["lens_findings"] = [
            {"lens": f.get("lens"), "severity": f.get("severity"),
             "detail": f.get("detail") or f.get("text") or ""}
            for f in ((lens.get("report") or {}).get("findings") or [])
        ]
        short["weight_covered"] = score.get("weight_covered")

    audit = row.get("audit") or {}
    if audit:
        short["audit_issues"] = [
            {"item": i.get("checklist_item"), "severity": i.get("severity"),
             "explanation": i.get("explanation") or ""}
            for i in (audit.get("issues") or [])
        ]
        short["audit_violations"] = [
            {"item": r.get("item"), "note": r.get("note") or ""}
            for r in (audit.get("map") or [])
            if r.get("status") == module_auditor.STATUS_VIOLATION
        ]

    return short


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
    """Текст отказа с КОНКРЕТНЫМИ причинами, а не с общими словами.

    Раньше здесь было только `reason` — одна и та же строка «модель не собрала
    годный вариант» три раза подряд. Она верна и совершенно бесполезна: по ней
    не понять ни что не так с ответами опросника, ни что чинить в библиотеке.
    Подробности лежали рядом, в problems, и до автора не доезжали.
    """
    lines = []
    for row in tried:
        line = "попытка %d — %s (%s)" % (
            row["attempt"], row.get("reason", "?"), row.get("stage", "?"))
        problems = row.get("problems") or []
        if problems:
            # Три-четыре первых: полный список бывает длинным, а первые причины
            # обычно и есть настоящие — остальное часто их следствия.
            line += ": " + "; ".join(str(p) for p in problems[:4])
            if len(problems) > 4:
                line += " и ещё %d" % (len(problems) - 4)
        lines.append(line)

    return ("Ни одна из %d попыток %s.\n%s"
            % (len(tried) or attempts, what,
               "\n".join(lines) or "Причины неизвестны."))
