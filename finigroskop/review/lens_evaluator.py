"""Агент «Оценщик по линзам Шелла» (v2) — качественная оценка игры.

Пятый шаг ветки анализа, между диагностом и синтезатором. Единственный шаг
конвейера, который читает игру КАК ИГРУ, а не как набор чисел: замысел, глубину
решений, вовлечение, образовательную ценность. Полный текст роли —
review/prompts/lenses.md.

Его особое положение объясняет большую часть правил ниже. Всё, что симуляция
принципиально не может измерить — убедительность питча, напряжение за столом,
риск сговора между живыми людьми, вредность финансового урока, — доходит до
итогового отчёта ТОЛЬКО через этого агента. Числовые агенты честно помечают
такие места как непроверенные и передают их сюда; если агент промолчит, они
останутся непроверенными до конца, и никто этого не заметит.

Что здесь считает код, а не модель:

- средние по категориям (`category_avg_preliminary`) — это арифметика, и на ней
  ломается главное правило шкалы: N/A не равно нулю. Модель, посчитавшая N/A
  нулём, занижает категорию за то, что её нельзя было оценить, и понять это по
  готовому числу невозможно;
- признак «категория целиком N/A» — тоже вывод из данных, а не суждение;
- условие вызова агента (`should_run`) — пустые critical_flags диагноста.

Имена категорий — отдельная зона риска, ради которой существует половина
валидатора. Синтезатор сопоставляет категорию с её весом ПО ИМЕНИ; расхождение
в одном слове или скобочном пояснении молча выкидывает категорию из итогового
балла вместе со всеми её находками.
"""

import json
import os
import re
import time

from review import prompts
from review.llm_provider import get_provider

# --- рабочее ядро линз -------------------------------------------------------
# Источник истины — таблица ядра в промпте (review/prompts/lenses.md). Здесь её
# машинная копия: 47 линз в девяти категориях. Копия, а не разбор markdown на
# лету — чтобы опечатка в таблице не роняла код молча; за расхождением следит
# отдельная проверка в tests/lens_evaluator_check.py.
#
# ИМЕНА КАТЕГОРИЙ КАНОНИЧЕСКИЕ — без скобочных пояснений, которые есть в
# таблице промпта («ключевая для фин-игры», «если 2+ игрока»). Именно по этим
# строкам синтезатор находит вес категории.
CORE = {
    "Замысел и образовательная ценность": [1, 3, 5, 17, 97, 98],
    "Целостность дизайна и цели": [7, 9, 12, 25, 26, 82],
    "Механика и решения игрока": [22, 23, 24, 32, 33, 42, 43],
    "Экономика и баланс": [28, 29, 30, 40, 41, 46, 47],
    "Навык, шанс, напряжение": [27, 31, 34, 35],
    "Игрок, доступность, вовлечение": [16, 18, 48, 49, 61, 62],
    "Социальное взаимодействие": [36, 37, 38],
    "Реиграбельность и нарратив": [4, 6, 65, 69, 70],
    "Структурная целостность и физический интерфейс": [13, 20, 54],
}

CORE_LENSES = sorted(n for nums in CORE.values() for n in nums)

# Линзы обучающей цели: для них N/A ЗАПРЕЩЁН (правило-исключение промпта).
# Для финансово-образовательной игры отсутствие обучающей цели — это реальный
# дефект и низкий балл, а не пробел материалов.
EDUCATION_LENSES = (97, 98)

# Социальные линзы: ими же закрываются переданные методичкой тесты на
# квотербекинг и мультиплеерный пасьянс.
SOCIAL_LENSES = (36, 37, 38)

SCORE_MIN, SCORE_MAX = 1, 10

# Причины n/a диагноста, которые значат «не измерено» (в отличие от
# mechanic_absent — «механики нет, проверять нечего»).
UNMEASURED_REASONS = ("no_data", "method_blind", "search_incomplete", "budget_exceeded")

# Три источника вместе дают карту того, где числам верить нельзя.
SIM_META_FIELDS = ("subjective_actions", "metric_responds_immediately",
                   "coalition_expressible", "ignored_components",
                   "content_scale", "assumptions")

# Агент читает много текста (правила целиком + справочник линз в промпте) и
# отвечает длинным разбором по 47 линзам — короткий таймаут его обрывает.
#
# ДВА срока, а не один, и это не перестраховка. Провайдер перебирает
# бесплатные модели каскадом, а таймаут раньше применялся к КАЖДОЙ: вызов с
# 300 с на пяти кандидатах шёл до 25 минут. Плюс сам агент делает второй заход,
# если ответ не прошёл самопроверку, — ещё столько же. На живом прогоне страница
# показала ошибку по своему сроку, а перебор в это время продолжался.
#
# LLM_TIMEOUT — предел ОДНОЙ попытки к одной модели. Он заметно короче общего
# бюджета намеренно: иначе первая же зависшая модель съест всё время, и каскад,
# ради которого он и заведён, ни разу не сработает.
LLM_TIMEOUT = 300

# TOTAL_BUDGET — предел на ВСЮ оценку: каскад моделей плюс повторный заход.
# Согласован с ожиданием генератора (generator/config.py, LENS_TIMEOUT): тот
# обязан быть больше, иначе он сдастся раньше, чем работа закончится.
# 1000, а не 300, по живым прогонам: оценка иногда не укладывалась и в 360 с,
# которые ждал генератор. Запас взят сознательно большой — это ПРЕДЕЛ, а не
# план: уложившаяся за минуту оценка вернётся за минуту, и лишнего никто не
# ждёт. А вот сдаться раньше, чем оценщик закончил, — это выбросить уже
# оплаченную работу и показать ошибку по несуществующему поводу.
TOTAL_BUDGET = 1000

# Сколько времени должно остаться, чтобы второй заход имел смысл.
MIN_RETRY_SECONDS = 45


def _issue(code: str, message: str, severity: str = "warning") -> dict:
    return {"code": code, "message": message, "severity": severity}


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


def lens_names() -> dict:
    """Номер линзы → её название, разобранное из справочника в промпте.

    Нужно только для экрана: показать автору «линза 32 «Осмысленного выбора»»
    вместо голого номера. Держать второй список названий в коде смысла нет —
    он бы неизбежно разошёлся со справочником, который правится чаще.
    """
    text = prompts.load_lenses_prompt()
    out = {}
    for num, name in re.findall(r"^(\d{1,3})\.\s+\*\*(.+?)\*\*", text, re.M):
        out.setdefault(int(num), name.strip())
    return out


def category_of(lens_number: int):
    """В какой категории ядра живёт линза (или None, если вне ядра)."""
    for name, nums in CORE.items():
        if lens_number in nums:
            return name
    return None


# --- условие вызова ----------------------------------------------------------
def should_run(findings_diagnost: dict) -> dict:
    """Можно ли звать линзы. Решает КОД, а не формулировка в ответе модели.

    Правило простое: пока у диагноста есть незакрытые критичные находки, игру
    чинят, а не оценивают содержательно. Иначе агент потратит вызов на разбор
    замысла игры, у которой прямо сейчас работает эксплойт-петля, — и его
    оценка устареет ровно в тот момент, когда правку применят.
    """
    crit = [f for f in (findings_diagnost or {}).get("critical_flags") or []
            if f.get("severity") == "critical"]
    if crit:
        codes = ", ".join(sorted({f.get("code") or "?" for f in crit}))
        return {"ready": False, "critical": crit,
                "reason": f"у диагноста есть незакрытые критичные находки: {codes} — "
                          "их чинят до содержательной оценки"}
    if not (findings_diagnost or {}).get("results"):
        return {"ready": False, "critical": [],
                "reason": "вердиктов диагноста ещё нет — линзы идут после него"}
    return {"ready": True, "critical": [], "reason": "критичных находок нет"}


# --- сборка входа ------------------------------------------------------------
def merge_notes(stats_notes, diagnost_notes) -> list:
    """Объединяет заметки для линз из ДВУХ источников в один список.

    Формы у них разные, и это не случайность: «Оценщик статистик» присылает
    свободные наблюдения строкой, а диагност — структурированные записи с полем
    `question_for_lenses`, то есть с прямым запросом вердикта по гибридному
    тесту методички. Различие сохраняется: ответа требуют только вторые, и
    именно по ним валидатор проверяет полноту блока `answers_to_diagnost`.
    """
    out = []
    for note in stats_notes or []:
        if isinstance(note, str) and note.strip():
            out.append({"source": "stats_evaluator", "observation": note.strip(),
                        "from_test": None, "question_for_lenses": None})
        elif isinstance(note, dict):
            out.append({"source": "stats_evaluator",
                        "observation": note.get("observation") or note.get("text") or "",
                        "from_test": note.get("from_test"),
                        "question_for_lenses": note.get("question_for_lenses")})
    for note in diagnost_notes or []:
        if not isinstance(note, dict):
            continue
        out.append({"source": "diagnost",
                    "observation": note.get("observation") or "",
                    "from_test": note.get("from_test"),
                    "question_for_lenses": note.get("question_for_lenses")})
    return out


def open_questions(notes: list) -> list:
    """Заметки, требующие явного вердикта: только те, где есть вопрос."""
    return [n for n in notes or [] if (n.get("question_for_lenses") or "").strip()]


def unmeasured_areas(coverage_summary: dict) -> dict:
    """Непроверенное из сводки диагноста — без `mechanic_absent`.

    Механики нет — проверять нечего, и это не пробел. Остальные четыре причины
    означают «не измерено», и агент обязан о них помнить: занижать за них
    оценку нельзя, но и молча засчитывать в плюс тоже.
    """
    br = (coverage_summary or {}).get("n_a_breakdown") or {}
    return {k: int(br.get(k) or 0) for k in UNMEASURED_REASONS if br.get(k)}


def build_input(game_spec_root: dict, canonical_text: str = None,
                stats_notes: list = None, diagnost_notes: list = None,
                sim_meta: dict = None, coverage_summary: dict = None) -> dict:
    """Семь источников входа. Полные отчёты числовых агентов сюда НЕ попадают.

    Разделение намеренное: качественная и числовая оценки сходятся впервые
    только в синтезаторе. Дай агенту готовые вердикты числовых агентов — и он
    начнёт их пересказывать вместо того, чтобы делать свою работу; к линзам
    приходит выжимка (заметки и метаданные), а не чужие отчёты.
    """
    root = game_spec_root or {}
    spec = root.get("game_spec") or {}
    meta = sim_meta or {}
    notes = merge_notes(stats_notes, diagnost_notes)
    return {
        "text": spec.get("text") or {},
        "core": spec.get("core") or {},
        "canonical_text": canonical_text or "",
        "notes_for_lenses": notes,
        "open_questions": open_questions(notes),
        "sim_meta": {k: meta.get(k) for k in SIM_META_FIELDS if k in meta},
        "actions_resolution": (root.get("diagnostic_meta") or {}).get("actions_resolution") or {},
        "coverage_summary": coverage_summary or {},
        "unmeasured": unmeasured_areas(coverage_summary),
    }


def _scope_core(scope) -> dict:
    """Ядро линз для этого прохода.

    Без области — полное ядро, то есть ровно прежнее поведение ФинИгроСкопа.
    Область задаётся только когда оценивается ОТДЕЛЬНЫЙ модуль игры (генератор,
    см. review/lens_scope.py): спрашивать у модели 47 линз о куске игры, который
    физически не содержит материала для трёх четвертей из них, — значит платить
    за N/A и получать балл, посчитанный неизвестно по чему.
    """
    core = (scope or {}).get("core")
    return core if core else CORE


def _scope_education(scope) -> tuple:
    """Линзы, которым N/A запрещён. Без области — правило ФинИгроСкопа."""
    if scope is None:
        return EDUCATION_LENSES
    return tuple(scope.get("education_lenses") or ())


def build_message(data: dict, scope: dict = None) -> str:
    core = _scope_core(scope)
    core_lenses = sorted(n for nums in core.values() for n in nums)
    parts = [
        "=== game_spec.text (концепт, правила, компоненты, рекомендации) ===",
        json.dumps(data["text"], ensure_ascii=False, indent=2),
        "",
        "=== game_spec.core (факты об устройстве партии) ===",
        json.dumps(data["core"], ensure_ascii=False, indent=2),
        "",
        "=== КАНОНИЧЕСКИЙ ТЕКСТ ИГРЫ (дословно, вместе с уточнениями автора) ===",
        data.get("canonical_text") or "(текст не передан)",
        "",
        "=== diagnostic_meta.actions_resolution (чем определяется исход действия) ===",
        json.dumps(data["actions_resolution"], ensure_ascii=False, indent=2),
        "",
        "=== МЕТАДАННЫЕ СИМУЛЯЦИОНИСТА (границы модели) ===",
        json.dumps(data["sim_meta"], ensure_ascii=False, indent=2),
    ]

    subj = (data.get("sim_meta") or {}).get("subjective_actions") or []
    if subj:
        parts += [
            "",
            "ВНИМАНИЕ: действия " + ", ".join(map(str, subj)) + " разрешаются "
            "субъективным суждением игроков. Любое число, посчитанное по ним, "
            "отражает допущение симулятора, а не игру: оценивай такие механики по "
            "замыслу и тексту правил, число упоминай только с оговоркой.",
        ]

    notes = data.get("notes_for_lenses") or []
    parts += [
        "",
        "=== ЗАМЕТКИ ДЛЯ ЛИНЗ (от «Оценщика статистик» и от диагноста) ===",
        json.dumps(notes, ensure_ascii=False, indent=2) if notes
        else "(заметок нет)",
    ]

    asked = data.get("open_questions") or []
    if asked:
        lines = [f"  • тест {q.get('from_test') or '—'}: {q.get('question_for_lenses')}"
                 for q in asked]
        parts += [
            "",
            f"=== ВОПРОСЫ, ТРЕБУЮЩИЕ ТВОЕГО ВЕРДИКТА ({len(asked)}) ===",
            "\n".join(lines),
            "",
            "На КАЖДЫЙ из них нужен явный ответ в блоке answers_to_diagnost. "
            "Пропущенный вопрос означает, что тест методички остался без вердикта: "
            "число посчитано, а решения никто не принял.",
        ]

    parts += [
        "",
        "=== СВОДКА НЕПРОВЕРЕННОГО (coverage_summary диагноста) ===",
        json.dumps(data.get("coverage_summary") or {}, ensure_ascii=False, indent=2),
    ]
    unmeasured = data.get("unmeasured") or {}
    if unmeasured:
        total = sum(unmeasured.values())
        parts += [
            "",
            f"Из них НЕ ИЗМЕРЕНО: {total} тестов ({unmeasured}). Это значит «не "
            "проверяли», а не «проблемы нет». Занижать за это оценку нельзя, но если "
            "непроверенная зона относится к оцениваемой линзе — скажи об этом в "
            "обосновании и перечисли такие зоны в unmeasured_noted.",
        ]

    # Список ядра подаём явно: «каждая линза ядра присутствует в выходе» —
    # первый пункт самопроверки промпта, и проще всего его выполнить, имея
    # перечень перед глазами.
    core_lines = [f"  {name}: " + ", ".join(map(str, nums)) for name, nums in core.items()]
    parts += [
        "",
        f"=== РАБОЧЕЕ ЯДРО: {len(core_lenses)} линз в {len(core)} категориях ===",
        "\n".join(core_lines),
        "",]

    if scope:
        parts += [
            "Это ЧАСТЬ игры, а не игра целиком: оценивается модуль "
            f"«{scope.get('phase')}». Категории вне списка выше к нему "
            "неприменимы — их не оценивай и не упоминай. Не достраивай "
            "недостающие части игры в уме: чего нет в присланных материалах, "
            "того нет.",
            "",
        ]

        # Главное правило модульной оценки. Без него агент читает пустоту как
        # брак: на живом прогоне модуль механик получил 4 из 10 за то, что в
        # нём «неизвестны стоимости и длина пути» — то есть за отсутствие того,
        # что появляется только на этапе 5 и содержаться в нём НЕ МОЖЕТ.
        later = scope.get("later_stages") or []
        if later:
            parts += [
                "=== ЧЕГО В ЭТОМ МОДУЛЕ НЕТ И БЫТЬ НЕ ДОЛЖНО ===",
                "Игра собирается по этапам. Перечисленное ниже появится позже, "
                "и его отсутствие здесь — НЕ дефект и НЕ находка:",
            ]
            parts += [f"  • {what} — {when}" for what, when in later]
            parts += [
                "",
                "Если линзу нельзя оценить БЕЗ этого материала — ставь ей N/A с "
                "причиной вроде «появится на этапе N», а НЕ низкий балл. Низкий "
                "балл означает «сделано плохо»; здесь же не сделано вовсе, и "
                "это правильно. N/A не занижает категорию: она считается по "
                "оценённым линзам.",
                "",
                "Находки о нехватке этого материала не пиши вообще. Автор "
                "прочтёт их как список ошибок и станет чинить то, что чинить не "
                "нужно.",
                "",
            ]

        notes = scope.get("category_notes") or {}
        if notes:
            parts += ["=== ОСОБЫЕ ОГОВОРКИ ПО КАТЕГОРИЯМ ==="]
            parts += [f"  «{name}»: {text}" for name, text in notes.items()]
            parts += [""]

        if not scope.get("educational"):
            parts += [
                "Игра НЕ заявлена как обучающая (вопрос 1 опросника). Линзы 97 и "
                "98 об обучающей цели оценивай только если материалы дают для "
                "этого основание; иначе честный N/A с причиной, а не низкий балл.",
                "",
            ]

    parts += [
        "Имена категорий в ответе указывай ТОЧНО как в списке выше — по ним "
        "синтезатор находит вес категории, и любое расхождение молча выкинет её "
        "из итогового балла вместе со всеми находками.",
        "",
        "Верни один JSON без текста вокруг.",
    ]
    return "\n".join(parts)


# --- детерминированные расчёты ----------------------------------------------
def is_na(lens: dict) -> bool:
    """N/A — это либо явный флаг, либо отсутствующая оценка."""
    if (lens or {}).get("na"):
        return True
    score = (lens or {}).get("score")
    if score is None:
        return True
    return isinstance(score, str) and score.strip().upper() in ("N/A", "NA", "—", "-")


def category_average(lenses: list):
    """Среднее по категории. N/A НЕ входит в расчёт — ни как ноль, ни как-либо.

    Это главное правило шкалы, и нарушить его можно только тихо: категория с
    двумя оценками 8 и тремя N/A даёт 8.0, а не 3.2. Второй вариант выглядит
    как обычный балл и означает «занизили за то, что нельзя было оценить».
    """
    scored = [float(l["score"]) for l in (lenses or [])
              if not is_na(l) and isinstance(l.get("score"), (int, float))]
    if not scored:
        return None
    return round(sum(scored) / len(scored), 2)


def recompute_categories(report: dict) -> list:
    """Пересчитывает средние и признак N/A по каждой категории ответа.

    Возвращает список категорий в каноническом виде — то, что уйдёт синтезатору.
    Оценки линз при этом не трогаются: они суждение агента, а среднее — счёт.
    """
    out = []
    for cat in (report or {}).get("categories") or []:
        lenses = cat.get("lenses") or []
        avg = category_average(lenses)
        out.append({
            "name": cat.get("name"),
            "lenses": lenses,
            "category_avg_preliminary": avg,
            # Категория целиком неприменима — тоже вывод из данных, а не мнение.
            "na": avg is None,
            "scored": sum(1 for l in lenses if not is_na(l)),
            "total": len(lenses),
        })
    return out


def all_lenses(report: dict) -> dict:
    """Номер линзы → её запись, по всему ответу (для проверок полноты)."""
    out = {}
    for cat in (report or {}).get("categories") or []:
        for lens in cat.get("lenses") or []:
            n = lens.get("n")
            if isinstance(n, int):
                out[n] = dict(lens, _category=cat.get("name"))
    return out


def needs_playtest(sim_meta: dict) -> dict:
    """Обязательна ли рекомендация о живом плейтесте. Считает код.

    Два условия из промпта, и оба означают одно: ключевая механика игры
    симуляцией по существу не проверяется. Решение об этом не может зависеть от
    того, вспомнил ли агент правило, — автор обязан узнать об этом всегда.
    """
    meta = sim_meta or {}
    reasons = []
    subj = meta.get("subjective_actions") or []
    if subj:
        reasons.append("действия, разрешаемые субъективным суждением: "
                       + ", ".join(map(str, subj)))
    if meta.get("coalition_expressible") is False:
        reasons.append("сговор механически невыразим — симуляция о нём ничего не скажет")
    return {"required": bool(reasons), "reasons": reasons}


# --- валидация ---------------------------------------------------------------
def validate(report: dict, data: dict = None, scope: dict = None) -> list:
    """Самопроверка промпта, выполненная кодом. Ответ не «чинится».

    Ловятся те нарушения, которые в готовом отчёте выглядят как нормальная
    работа: заниженная за N/A категория, потерянный вопрос диагноста, имя
    категории с лишним словом, пропущенная рекомендация о плейтесте.

    `scope` сужает требования до одного модуля игры (см. review/lens_scope.py).
    Без него проверяется полное ядро — прежнее поведение ФинИгроСкопа.
    """
    issues = []
    report = report or {}
    data = data or {}
    core = _scope_core(scope)
    core_lenses = sorted(n for nums in core.values() for n in nums)
    education_lenses = _scope_education(scope)
    cats = report.get("categories") or []
    seen = all_lenses(report)

    # 1) ИМЕНА КАТЕГОРИЙ. Расхождение здесь — самая дорогая тихая поломка:
    # синтезатор ищет категорию по имени и молча роняет ненайденную.
    for cat in cats:
        name = cat.get("name")
        if name not in core:
            near = ""
            if isinstance(name, str):
                base = re.sub(r"\s*\(.*?\)\s*$", "", name).strip()
                if base in core:
                    near = (f" Похоже, это «{base}» со скобочным пояснением — "
                            "в ответе оно недопустимо.")
            issues.append(_issue(
                "category_name_unknown",
                f"Категория «{name}» не совпадает с рабочим ядром.{near} "
                "Синтезатор сопоставляет категории по имени: ненайденная выпадет из "
                "итогового балла вместе со всеми своими находками.", "error"))

    for missing in [c for c in core if c not in {x.get("name") for x in cats}]:
        issues.append(_issue(
            "category_missing",
            f"Категория «{missing}» отсутствует в ответе целиком. Неприменимая "
            "категория обязана присутствовать с пометкой N/A, а не исчезать.", "error"))

    # 2) ПОЛНОТА ЯДРА: каждая линза — с оценкой или с N/A и причиной.
    lost = [n for n in core_lenses if n not in seen]
    if lost:
        issues.append(_issue(
            "lenses_missing",
            f"В ответе нет {len(lost)} линз ядра: {', '.join(map(str, lost[:15]))}"
            + ("…" if len(lost) > 15 else "")
            + f". Ожидалось ровно {len(core_lenses)} — по одной на каждую линзу ядра.",
            "error"))

    outside = sorted(n for n in seen if n not in core_lenses)
    declared = {int(x) for x in (report.get("out_of_core_used") or [])
                if isinstance(x, (int, str)) and str(x).isdigit()}
    for n in outside:
        if n not in declared:
            issues.append(_issue(
                "lens_out_of_core",
                f"Линза {n} оценена, хотя она вне ядра, и не объявлена в "
                "out_of_core_used."))

    # 3) ШКАЛА: N/A не ноль, оценки в диапазоне, у каждой — обоснование.
    for n, lens in sorted(seen.items()):
        score = lens.get("score")
        if is_na(lens):
            if not (lens.get("na_reason") or lens.get("basis") or "").strip():
                issues.append(_issue(
                    "na_without_reason",
                    f"Линза {n}: N/A без причины. «Не удалось оценить» обязано "
                    "объяснять, почему именно."))
            if score == 0:
                issues.append(_issue(
                    "na_as_zero",
                    f"Линза {n}: N/A и одновременно оценка 0. N/A не равно нулю — "
                    "занижать за отсутствие данных запрещено.", "error"))
            continue
        if isinstance(score, (int, float)):
            if not (SCORE_MIN <= float(score) <= SCORE_MAX):
                issues.append(_issue(
                    "score_out_of_range",
                    f"Линза {n}: оценка {score} вне шкалы {SCORE_MIN}–{SCORE_MAX}."
                    + (" Ноль на этой шкале означал бы N/A, а он ставится отдельно."
                       if score == 0 else ""), "error"))
        else:
            issues.append(_issue("score_not_number",
                                 f"Линза {n}: оценка «{score}» не число и не N/A."))
        if not (lens.get("basis") or "").strip():
            issues.append(_issue(
                "basis_missing",
                f"Линза {n}: оценка без обоснования. Каждая оценка обязана "
                "прослеживаться до фразы материалов."))

    # 4) ПРАВИЛО-ИСКЛЮЧЕНИЕ: обучающая цель не может быть N/A.
    for n in education_lenses:
        lens = seen.get(n)
        if lens and is_na(lens):
            issues.append(_issue(
                "education_na",
                f"Линза {n} (обучающая цель) помечена N/A. Для финансово-"
                "образовательной игры отсутствие обучающей цели — это реальный "
                "дефект и низкая оценка, а не пробел материалов.", "error"))

    # 5) СРЕДНИЕ: считает код, ответ модели идёт на сверку.
    computed = {c["name"]: c for c in recompute_categories(report)}
    for cat in cats:
        name = cat.get("name")
        got = cat.get("category_avg_preliminary")
        want = (computed.get(name) or {}).get("category_avg_preliminary")
        if got is None and want is None:
            continue
        if want is None:
            issues.append(_issue(
                "avg_on_empty_category",
                f"«{name}»: выставлено среднее {got}, хотя ни одна линза категории "
                "не оценена. Категория целиком N/A.", "error"))
        elif got is None:
            issues.append(_issue("avg_missing",
                                 f"«{name}»: не указано category_avg_preliminary."))
        elif abs(float(got) - want) > 0.051:
            issues.append(_issue(
                "avg_recomputed",
                f"«{name}»: среднее {got}, а по оценённым линзам — {want}. "
                "Взято посчитанное. Чаще всего расхождение значит, что N/A "
                "посчитали нулём.", "error"))

    # 6) ВОПРОСЫ ДИАГНОСТА: на каждый нужен явный вердикт.
    asked = {str(q.get("from_test")) for q in (data.get("open_questions") or [])
             if q.get("from_test")}
    answered = {str(a.get("from_test")) for a in (report.get("answers_to_diagnost") or [])
                if a.get("from_test")}
    for test_id in sorted(asked - answered):
        issues.append(_issue(
            "question_unanswered",
            f"Вопрос диагноста по тесту {test_id} остался без ответа. Гибридный тест "
            "методички повиснет: число посчитано, а вердикта никто не вынес.", "error"))
    for test_id in sorted(answered - asked):
        issues.append(_issue(
            "answer_without_question",
            f"Ответ на тест {test_id}, которого не было среди заданных вопросов."))
    for ans in report.get("answers_to_diagnost") or []:
        if not (ans.get("answer") or "").strip():
            issues.append(_issue(
                "answer_empty",
                f"Ответ по тесту {ans.get('from_test')} пуст."))
        if not (ans.get("lenses_used") or []):
            issues.append(_issue(
                "answer_without_lens",
                f"Ответ по тесту {ans.get('from_test')} не ссылается ни на одну линзу — "
                "непонятно, чем именно вынесен вердикт."))

    # 7) ПЛЕЙТЕСТ: обязателен при субъективных механиках и невыразимом сговоре.
    playtest = needs_playtest(data.get("sim_meta"))
    if playtest["required"] and not report.get("playtest_recommended"):
        issues.append(_issue(
            "playtest_missing",
            "Рекомендация о живом плейтесте обязательна: "
            + "; ".join(playtest["reasons"])
            + ". В такой игре ключевая механика симуляцией по существу не "
              "проверяется, и автор обязан об этом узнать.", "error"))
    if report.get("playtest_recommended") and not (report.get("playtest_reason") or "").strip():
        issues.append(_issue("playtest_without_reason",
                             "Плейтест рекомендован без объяснения причины."))

    # 8) НЕПРОВЕРЕННОЕ не должно молча засчитываться в плюс.
    if (data.get("unmeasured") or {}) and not (report.get("unmeasured_noted") or []):
        total = sum((data.get("unmeasured") or {}).values())
        issues.append(_issue(
            "unmeasured_not_noted",
            f"{total} тестов диагноста не выполнены (не измерены), но блок "
            "unmeasured_noted пуст. «Не измерено» не равно «нормально»."))

    # 9) Одиночная игра: социальные линзы оценивать не по чему.
    players = (data.get("core") or {}).get("players") or {}
    if players.get("max") == 1:
        scored_social = [n for n in SOCIAL_LENSES
                         if seen.get(n) and not is_na(seen[n])]
        if scored_social:
            issues.append(_issue(
                "social_scored_solo",
                f"Игра одиночная (players.max=1), но социальные линзы "
                f"{', '.join(map(str, scored_social))} получили оценку. Ожидался N/A."))
    return issues


# --- вызов -------------------------------------------------------------------
def run(data: dict, provider_name: str = None, cache_dir: str = None,
        scope: dict = None, message: str = None) -> dict:
    """Один вызов агента. Возвращает {available, report, issues, raw, provider}.

    Средние по категориям подменяются посчитанными ПОСЛЕ валидации: расхождение
    уже зафиксировано отдельным замечанием, а дальше по конвейеру должно уйти
    верное число. Оценки самих линз — суждение агента, они не трогаются.

    `scope` — область применимости для оценки отдельного модуля игры. Без него
    работа идёт по полному ядру, как и раньше.

    `message` — готовое сообщение вместо собранного здесь. Нужно вызывающему,
    который добавляет свои блоки (review/lens_module.py подкладывает параметры
    игры из опросника). Повтор при неудачной валидации достраивается поверх
    именно его — иначе второй заход ушёл бы уже без этих блоков.
    """
    system = prompts.load_lenses_prompt()
    user = message if message is not None else build_message(data, scope)

    provider = get_provider(provider_name, cache_dir=cache_dir)

    # Общий срок на всю оценку. Дальше он только уменьшается — и каскад моделей,
    # и повторный заход укладываются В НЕГО, а не каждый в свой.
    deadline = time.monotonic() + TOTAL_BUDGET

    resp = provider.complete(system, user, timeout=LLM_TIMEOUT, deadline=deadline)
    if not resp.available:
        return {"available": False, "error": resp.error}

    report = _extract_json(resp.text)
    if report is None:
        return {"available": False,
                "error": "Оценщик по линзам вернул ответ, который не удалось разобрать "
                         "как JSON.",
                "raw": resp.text}

    issues = validate(report, data, scope)

    # Повтор просим только по тем нарушениям, которые модель должна чинить сама.
    # Расхождение в среднем к ним не относится: правильное значение известно
    # точно, и просить угадать его ещё раз — трата вызова.
    RECOMPUTED = ("avg_recomputed",)
    blocking = [i for i in issues
                if i["severity"] == "error" and i["code"] not in RECOMPUTED]
    # Второй заход — только если на него осталось время. Начинать его за
    # полминуты до конца бюджета значит гарантированно оборвать на середине:
    # первый ответ мы при этом уже имеем, и он не хуже оттого, что не исправлен.
    if blocking and deadline - time.monotonic() > MIN_RETRY_SECONDS:
        lines = [f"• [{i['severity']}] {i['code']}: {i['message']}" for i in blocking]
        retry = (user + "\n\n=== ТВОЙ ПРЕДЫДУЩИЙ ОТВЕТ НЕ ПРОШЁЛ ПРОВЕРКУ ===\n"
                 + "\n".join(lines)
                 + "\n\nИсправь ровно эти нарушения и верни ПОЛНЫЙ JSON заново.")
        resp2 = provider.complete(system, retry, timeout=LLM_TIMEOUT,
                                  deadline=deadline)
        if resp2.available:
            report2 = _extract_json(resp2.text)
            if report2 is not None:
                issues2 = validate(report2, data, scope)
                if len(issues2) < len(issues):
                    report, issues, resp = report2, issues2, resp2

    report["categories"] = recompute_categories(report)
    return {
        "available": True, "report": report, "issues": issues,
        "raw": resp.text, "provider": resp.provider,
    }


# --- что уходит дальше -------------------------------------------------------
def to_synthesis(report: dict) -> dict:
    """Срез для синтезатора: категории со средними, ответы, плейтест.

    Имена полей совпадают с тем, что читает review/synthesizer.py — этот срез и
    есть Findings_lenses.json в том виде, в каком он нужен следующему шагу.

    Средние ПЕРЕСЧИТЫВАЮТСЯ здесь заново, хотя `run` уже сделал это перед
    сохранением. Дублирование намеренное: это граница с синтезатором, за
    которой число превращается в итоговый балл, и отдать сюда неповеренное
    значение модели нельзя ни при каком порядке вызовов. Пересчёт идемпотентен,
    лишним он не будет.
    """
    report = report or {}
    return {
        "categories": [
            {"name": c.get("name"), "na": bool(c.get("na")),
             "category_avg_preliminary": c.get("category_avg_preliminary")}
            for c in recompute_categories(report)
        ],
        "answers_to_diagnost": report.get("answers_to_diagnost") or [],
        "unmeasured_noted": report.get("unmeasured_noted") or [],
        "playtest_recommended": bool(report.get("playtest_recommended")),
        "playtest_reason": report.get("playtest_reason"),
        "findings": report.get("findings") or [],
    }
