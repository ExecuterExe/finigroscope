# -*- coding: utf-8 -*-
"""Упаковка: полное описание игры и game_spec.json. Последний шаг конвейера.

Отдаёт две вещи для двух разных читателей:

  описание   — человеку. Порядок и шаблоны разделов заданы Приложением А ТЗ.
  game_spec  — машине. ЗАКРЫТЫЙ контракт, общий с ФинИгроСкопом
               (finigroskop/review/extractor.py, CORE_FIELDS). Через него
               сгенерированная игра попадает на симуляционный этап: скелет,
               статистика, диагност, линзы, синтезатор.

# Почему описание собирает КОД, а не модель

По таблице 9 ТЗ «Итоговый сборщик (Трансформатор)» — агент на DeepSeek-Flash.
Здесь сознательное отступление, и вот причина.

К этому шагу всё уже написано и проверено: концепция и особенности — агентом
особенностей, сюжет — агентом сюжета, правила и советы — агентом правил,
количества и материалы — расчётом этапа 5. Каждый кусок прошёл аудитора и
оценку по линзам. Пустить по ним модель значит дать ей переписать проверенное
на самом последнем шаге, когда позади уже все проверки и впереди ни одной.
Выигрыш — гладкость формулировок; цена — правило, которого никто не видел, в
напечатанной коробке.

Собственного содержания у этого шага нет вовсе: девять разделов из одиннадцати
уже существуют готовым текстом, а «Артефакты» в Приложении А заданы прямым
шаблоном. Код переносит их, ничего не добавляя, — и это ровно то, что нужно.

# Почему core всё-таки просит модель

`game_spec.core` — не пересказ, а ПЕРЕВОД. В модуле механик написано «игроки по
очереди открывают карты, при 4+ находка», а контракту нужны типизированные
`turn.actions`, `randomness[].type`, `resources[].scope`,
`win_condition {type, metric, threshold}`. Это разбор смысла, и кодом он
получается либо неверным, либо набором догадок.

Поэтому разделение жёсткое: всё, что ИЗВЕСТНО из ответов опросника и расчётов,
код заполняет сам и модели не показывает как задачу — число игроков, режим,
выбывание, помощь отстающим, длительность, компоненты с количествами. Модель
занимается только тем, что требует чтения механик. А потом её ответ
проверяется: имена ресурсов, разбор действий и метрика победы обязаны сойтись
между собой и с уже принятым.
"""

import json
import re

import llm

MAX_ATTEMPTS = 3

# Закрытый контракт. Копия константы из finigroskop/review/extractor.py —
# намеренно, а не по недосмотру: сервисы разные, импортировать друг у друга они
# не могут (у обоих есть модуль верхнего уровня `config`, и это РАЗНЫЕ модули).
# Расхождение ловится проверкой ниже: лишнее поле в core — нарушение контракта,
# о котором надо сказать, а не «полезное дополнение».
CORE_FIELDS = ("players", "mode", "elimination", "turn", "randomness",
               "resources", "win_condition", "loss_condition", "catch_up",
               "play_time", "limits")
TEXT_FIELDS = ("concept", "setting", "full_rules", "components", "recommendations")

# Поля core, которые заполняет КОД: они известны из ответов опросника и расчёта,
# и спрашивать их у модели значило бы разрешить ей ошибиться там, где ошибки
# быть не может.
CODE_OWNED = ("players", "mode", "elimination", "catch_up", "play_time")

# Поля, за которые отвечает модель: их не вывести без чтения модуля механик.
MODEL_OWNED = ("turn", "randomness", "resources", "win_condition",
               "loss_condition", "limits")

RESOLUTION_KINDS = ("deterministic", "probabilistic", "subjective_judgment")

# Ответ опросника про взаимодействие → режим контракта. «Косвенное» и
# «смешанное» — соревновательные: победитель один, просто мешать друг другу
# нельзя. Оценщик статистик читает именно cooperative (stats_evaluator), и
# ошибка здесь молча меняет правила разбора ничьих.
MODE_BY_INTERACTION = {
    "кооперативное": "cooperative",
    "конкурентное": "competitive",
    "косвенное": "competitive",
    "смешанное": "competitive",
    "соло": "solo",
}


class PackagingError(Exception):
    """Упаковать игру не удалось. Текст пригоден для показа пользователю."""

    user_facing = True


# --------------------------------------------------------------------------
# Полное описание (Приложение А)
# --------------------------------------------------------------------------

def _range_text(span, one, few, many):
    """«2–4 игрока», «6–9 лет». Одинаковые границы не удваиваем."""
    if not span:
        return None
    low, high = span.get("min"), span.get("max")
    if low is None and high is None:
        return None
    if low == high:
        return "%s %s" % (low, _plural(low, one, few, many))
    return "%s–%s %s" % (low, high, _plural(high, one, few, many))


def _plural(number, one, few, many):
    try:
        number = abs(int(number))
    except (TypeError, ValueError):
        return many
    if number % 10 == 1 and number % 100 != 11:
        return one
    if 2 <= number % 10 <= 4 and not 12 <= number % 100 <= 14:
        return few
    return many


def named_components(components, story):
    """Расчёт этапа 5, соединённый с именами из сюжета.

    Один источник имён на описание и на game_spec: расходиться им нельзя. Автор
    прочитает «Ключи — 12 шт.», а в машинный файл уйдёт «жетоны» — и дальше по
    конвейеру это будут два разных компонента.
    """
    by_type = {}
    for item in (story or {}).get("artifacts") or []:
        if isinstance(item, dict) and item.get("component"):
            by_type[item["component"]] = item

    rows = []
    for row in components or []:
        kind = row.get("component")
        named = by_type.get(kind) or {}
        rows.append({
            "component": kind,
            "name": named.get("name") or kind,
            "quantity": row.get("quantity"),
            "material": (row.get("material") or {}).get("chosen"),
            "role": named.get("role") or kind,
        })
    return rows


def _components_lines(rows):
    """Раздел «Артефакты» по шаблону Приложения А: имя — количество (материал)."""
    lines = []
    for index, row in enumerate(rows or [], 1):
        piece = "%d. %s — %s шт." % (index, row["name"], row.get("quantity"))
        if row.get("material"):
            piece += " («%s»)" % row["material"]
        if row.get("role") and row["role"] != row["name"]:
            piece += " — %s" % row["role"]
        lines.append(piece)
    return lines


def _mechanics_section(mechanics):
    """«Жанр и механики»: игровой цикл словами, без выдумывания.

    Единственный раздел, который приходится собирать из структуры, а не брать
    готовым текстом. Собирается перечислением полей модуля — ни одно
    предложение здесь не сочинено заново.
    """
    loop = mechanics.get("game_loop") or {}
    parts = []

    if mechanics.get("fit_rationale"):
        parts.append(mechanics["fit_rationale"])

    steps = [s for s in (loop.get("turn_structure") or []) if s]
    if steps:
        parts.append("Ход игрока: " + "; ".join(steps) + ".")
    if loop.get("turn_order"):
        parts.append("Порядок хода: %s." % loop["turn_order"])

    check = loop.get("success_check") or {}
    if check.get("rule"):
        parts.append("Проверка успеха: %s." % check["rule"])
    if loop.get("resource_flow"):
        parts.append("Ресурсы: %s." % loop["resource_flow"])
    if loop.get("progression"):
        parts.append("К концу партии ведёт: %s." % loop["progression"])

    return "\n\n".join(parts)


def _rules_section(rules_variant):
    """«Краткие правила»: подготовка, ход, особые правила, конец партии."""
    parts = []
    setup = [s for s in (rules_variant.get("setup") or []) if s]
    if setup:
        parts.append("**Подготовка**\n" + "\n".join(
            "%d. %s" % (i, s) for i, s in enumerate(setup, 1)))

    turn = [s for s in (rules_variant.get("turn") or []) if s]
    if turn:
        parts.append("**Ход игрока**\n" + "\n".join(
            "%d. %s" % (i, s) for i, s in enumerate(turn, 1)))

    special = rules_variant.get("special_rules") or []
    if special:
        parts.append("**Особые правила**\n" + "\n".join(
            "- **%s.** %s" % (r.get("title", "").rstrip("."), r.get("text", ""))
            for r in special))

    if rules_variant.get("ending"):
        parts.append("**Конец партии**\n" + rules_variant["ending"])

    return "\n\n".join(parts)


def sections(params, modules, components, rules_variant):
    """Разделы описания структурой: [{heading, text}] в порядке Приложения А.

    Отдаются отдельно от собранного markdown нарочно. Страница показывает
    разделы, а файл выгружается целиком — и если бы страница разбирала markdown
    обратно, появился бы второй, кривой источник тех же данных.
    """
    mechanics = modules.get("mechanics") or {}
    story = modules.get("story") or {}
    features = modules.get("features") or {}
    rules_variant = rules_variant or {}

    sections = []

    title = story.get("title") or "Без названия"
    sections.append(("Название игры", "«%s»" % title))
    sections.append(("Концепция", features.get("concept")))
    sections.append(("Жанр и механики", _mechanics_section(mechanics)))
    sections.append(("Сюжет", story.get("synopsis")))

    feature_list = features.get("features") or []
    if feature_list:
        sections.append(("Особенности игры", "\n".join(
            "- **%s.** %s" % ((f.get("title") or "").rstrip("."),
                              f.get("description") or "")
            for f in feature_list)))

    lines = _components_lines(named_components(components, story))
    if lines:
        sections.append(("Артефакты", "Для игры понадобятся:\n" + "\n".join(lines)))

    sections.append(("Краткие правила", _rules_section(rules_variant)))
    sections.append(("Условия победы",
                     (mechanics.get("win_condition") or {}).get("description")))
    sections.append(("Элементы случайности", mechanics.get("randomness_role")))
    sections.append(("Как игра помогает отстающим", features.get("catch_up_help")))

    tips = rules_variant.get("tips") or []
    if tips:
        sections.append(("Советы и рекомендации", "\n".join(
            "- **%s.** %s" % ((t.get("title") or "").rstrip("."), t.get("text") or "")
            for t in tips)))

    return [{"heading": heading, "text": str(text).strip()}
            for heading, text in sections
            if text and str(text).strip()]


def subtitle(params):
    """Строка «2–4 игрока · 6–9 лет · 5–15 минут»."""
    # Формы даются тройкой «1 / 2-4 / 5+»: по-русски «2–4 игрока», но
    # «5–8 игроков», и склонение считается по ВЕРХНЕЙ границе диапазона.
    parts = (_range_text(params.get("player_count"), "игрок", "игрока", "игроков"),
             _range_text(params.get("age_group"), "год", "года", "лет"),
             _range_text(params.get("play_time"), "минута", "минуты", "минут"))
    return " · ".join(x for x in parts if x)


def describe(params, modules, components, rules_variant):
    """Полное описание игры одним markdown-текстом — то, что выгружается файлом.

    Ничего не сочиняет: каждый раздел — уже принятый текст либо перечисление
    уже посчитанных чисел. Пустой раздел пропускается, а не заполняется
    заглушкой: «сюжета нет» у абстрактной игры — это факт, а не пробел.
    """
    title = (modules.get("story") or {}).get("title") or "Без названия"
    body = ["# %s" % title, ""]

    line = subtitle(params)
    if line:
        body += ["_%s_" % line, ""]

    for block in sections(params, modules, components, rules_variant):
        body += ["## %s" % block["heading"], "", block["text"], ""]

    return "\n".join(body).strip() + "\n"


# --------------------------------------------------------------------------
# game_spec: то, что заполняет код
# --------------------------------------------------------------------------

def known_core(params, mechanics):
    """Поля core, известные без чтения модуля механик."""
    return {
        "players": _span(params.get("player_count")),
        "mode": MODE_BY_INTERACTION.get(params.get("interaction"), "competitive"),
        "elimination": bool(params.get("elimination")),
        "catch_up": {
            "enabled": bool(params.get("catch_up")),
            "mechanism": (mechanics or {}).get("catch_up_mechanism"),
        },
        "play_time": _span(params.get("play_time")),
    }


def _span(value):
    if not isinstance(value, dict):
        return None
    return {"min": value.get("min"), "max": value.get("max")}


def spec_components(components, story):
    """Раздел text.components контракта: те же строки, что и в описании."""
    return [{"name": row["name"], "qty": row["quantity"],
             "material": row["material"], "function": row["role"]}
            for row in named_components(components, story)]


def spec_text(params, modules, components, rules_variant, description):
    story = modules.get("story") or {}
    features = modules.get("features") or {}
    tips = (rules_variant or {}).get("tips") or []

    return {
        "concept": features.get("concept"),
        "setting": story.get("setting"),
        # Полные правила — тот же текст, что видит человек: разночтение между
        # тем, что показано автору, и тем, что уйдёт на симуляцию, было бы
        # худшим из возможных расхождений.
        "full_rules": _rules_section(rules_variant or {}),
        "components": spec_components(components, story),
        "recommendations": [t.get("text") for t in tips if t.get("text")],
    }


# --------------------------------------------------------------------------
# game_spec: то, что переводит модель
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """# РОЛЬ

Ты — переводчик игровых правил в машинный формат. Тебе дают ПРИНЯТЫЙ модуль
механик настольной игры, написанный по-русски, и ты выражаешь его игровой цикл
типизированными полями.

Ты ничего не оцениваешь и ничего не улучшаешь. Если в модуле сказано «при 4+ на
кубике находка», твоё дело — записать это как проверку с порогом, а не решать,
хороший ли это порог.

# ЧЕГО ТЫ НЕ ДЕЛАЕШЬ

- Не придумываешь действий, ресурсов и условий, которых нет в модуле.
- Не переименовываешь то, что уже названо. Ресурс «жетоны-ключи» остаётся им.
- Не заполняешь поля догадкой. Не сказано — ставь null. Пустое поле честнее
  выдуманного: следующий этап строит по нему симуляцию.
- Не трогаешь число игроков, режим, выбывание, помощь отстающим и длительность:
  они уже известны из ответов автора и подставляются программой.

# ЖЁСТКИЕ ПРАВИЛА

1. `turn.actions` — список действий игрока в его ход, короткими
   идентификаторами латиницей через подчёркивание: `draw_card`, `place_token`.
   Ровно те действия, что описаны в модуле.

2. `actions_resolution` — по записи на КАЖДОЕ действие из `turn.actions`, ни
   одного лишнего. Значение — чем определяется исход:
   - `deterministic` — исход однозначен по правилам;
   - `probabilistic` — решает случайность (кубик, колода);
   - `subjective_judgment` — решают сами игроки (оценка, голосование, спор).

3. `resources` — что игроки копят и тратят. `scope`: `per_player` или `shared`.
   `start` — стартовое количество, `goal` — целевое, если оно названо, иначе
   null. Имена ресурсов — латиницей через подчёркивание.

4. `resource_roles` — по записи на каждый ресурс: `spendable` (тратится),
   `win_metric` (по нему определяется победа), `tracker` (просто счётчик).

5. `win_condition.metric` — ИМЯ одного из ресурсов списка `resources` либо null.
   Ссылаться на то, чего нет в списке, нельзя.

6. `randomness` — список источников случайности. Если случайности в игре нет,
   это пустой список, а не выдуманный кубик.

7. `limits.max_rounds` — предел числа раундов, если он назван в модуле. Не
   назван — null. Не подставляй «примерно».

8. Отвечай ТОЛЬКО валидным JSON по схеме ниже, без markdown-ограждений и текста
   вокруг.

# ФОРМАТ ОТВЕТА

{
  "turn": {"order": "как передаётся ход", "actions": ["draw_card", "place_token"]},
  "randomness": [{"type": "dice_roll", "outcomes": {"4-6": "находка", "1-3": "ловушка"}}],
  "resources": [
    {"name": "keys", "scope": "shared", "start": 0, "goal": 3}
  ],
  "win_condition": {"type": "collect_set", "metric": "keys", "threshold": 3},
  "loss_condition": {"type": "deck_exhausted"},
  "limits": {"max_rounds": null},
  "actions_resolution": {"draw_card": "probabilistic", "place_token": "deterministic"},
  "resource_roles": {"keys": "win_metric"}
}"""


def build_user_message(params, mechanics, story, critique=None, previous=None):
    def dump(value):
        return json.dumps(value, ensure_ascii=False, indent=2)

    parts = ["""## ПРИНЯТЫЙ МОДУЛЬ МЕХАНИК

Единственный источник игрового цикла. Переводи ровно его.

%s

## СПРАВОЧНО: НАЗВАНИЯ ИЗ СЮЖЕТА

Чем компоненты названы в игре. Нужно, чтобы имена ресурсов были узнаваемы.

%s

## УЖЕ ИЗВЕСТНО ПРОГРАММЕ (не заполняй эти поля)

%s

## ЗАДАЧА

Вырази игровой цикл полями по схеме из системного промпта.""" % (
        dump(mechanics),
        dump([{"component": a.get("component"), "name": a.get("name")}
              for a in (story or {}).get("artifacts") or []]),
        dump(known_core(params, mechanics)),
    )]

    if critique:
        parts.append("""## ПРЕДЫДУЩИЙ ОТВЕТ НЕ ПРИНЯТ

%s

Исправь ровно эти нарушения.

### Отклонённый ответ

%s""" % ("\n".join("- " + c for c in critique), dump(previous)))

    return "\n\n".join(parts)


def validate(data, params, mechanics, components):
    """Проверка перевода. Возвращает (нарушения, предупреждения)."""
    problems, warnings = [], []

    if not isinstance(data, dict):
        return ["Ответ модели не является объектом JSON."], warnings

    turn = data.get("turn") or {}
    actions = [a for a in (turn.get("actions") or []) if isinstance(a, str)]
    if not actions:
        problems.append("turn.actions пуст: без действий игрока симулировать нечего.")

    # 1. Разбор действий обязан покрывать ровно turn.actions. Рассинхрон здесь
    #    бесшумно обнуляет часть тестов диагноста — он читает разбор по имени.
    resolution = data.get("actions_resolution") or {}
    missing = [a for a in actions if a not in resolution]
    extra = [k for k in resolution if k not in actions]
    if missing:
        problems.append("actions_resolution не покрывает действия: %s."
                        % ", ".join(missing))
    if extra:
        problems.append("actions_resolution ссылается на действия вне "
                        "turn.actions: %s." % ", ".join(extra))
    bad = [k for k, v in resolution.items() if v not in RESOLUTION_KINDS]
    if bad:
        problems.append("Неизвестный способ разрешения у действий: %s. "
                        "Допустимы: %s." % (", ".join(bad),
                                            ", ".join(RESOLUTION_KINDS)))

    # 2. Ресурсы и их роли
    resources = [r for r in (data.get("resources") or []) if isinstance(r, dict)]
    names = [r.get("name") for r in resources if r.get("name")]
    if len(set(names)) != len(names):
        problems.append("Имена ресурсов повторяются: %s." % ", ".join(names))
    for resource in resources:
        if resource.get("scope") not in ("per_player", "shared"):
            problems.append("У ресурса «%s» scope=«%s», допустимы per_player и "
                            "shared." % (resource.get("name"), resource.get("scope")))
    roles = data.get("resource_roles") or {}
    unknown = [k for k in roles if k not in names]
    if unknown:
        problems.append("resource_roles ссылается на несуществующие ресурсы: %s."
                        % ", ".join(unknown))

    # 3. Метрика победы — имя из списка ресурсов. Ссылка в пустоту означает,
    #    что симулятор не найдёт, что считать.
    win = data.get("win_condition") or {}
    metric = win.get("metric")
    if metric and metric not in names:
        problems.append("win_condition.metric=«%s» не найдено среди ресурсов "
                        "(%s)." % (metric, ", ".join(names) or "их нет"))

    # 4. Случайность обязана соответствовать ответу автора
    randomness = data.get("randomness")
    if not isinstance(randomness, list):
        problems.append("randomness должно быть списком.")
    elif params.get("randomness") is False and randomness:
        problems.append("Автор выбрал игру без случайности, а в переводе "
                        "источников случайности %d." % len(randomness))
    elif params.get("randomness") is True and not randomness:
        warnings.append("Автор выбрал игру со случайностью, а источников в "
                        "переводе нет — проверьте модуль механик.")

    # 5. Порог победы, названный числом, стоит увидеть в модуле механик:
    #    расхождение здесь означает, что переведено не то условие.
    threshold = win.get("threshold")
    if isinstance(threshold, (int, float)):
        text = json.dumps(mechanics, ensure_ascii=False)
        if str(int(threshold)) not in re.findall(r"\d+", text):
            warnings.append("Порог победы %s не встречается в модуле механик — "
                            "проверьте, то ли условие переведено." % threshold)

    return problems, warnings


# --------------------------------------------------------------------------
# Сборка
# --------------------------------------------------------------------------

def assemble(params, modules, components, rules_variant, attempts=MAX_ATTEMPTS):
    """Полная упаковка: описание, game_spec и надстройка для диагноста."""
    missing = [name for name in ("mechanics", "story", "features")
               if not (modules or {}).get(name)]
    if missing:
        raise PackagingError(
            "Упаковывать нечего — не хватает принятых модулей: %s."
            % ", ".join(missing))

    mechanics = modules["mechanics"]
    story = modules.get("story") or {}
    description = describe(params, modules, components, rules_variant)
    blocks = sections(params, modules, components, rules_variant)

    log = []
    critique = None
    previous = None

    for attempt in range(1, attempts + 1):
        try:
            result = llm.complete_json(
                [{"role": "system", "content": SYSTEM_PROMPT},
                 {"role": "user", "content": build_user_message(
                     params, mechanics, story, critique, previous)}],
                # Перевод, а не сочинение: разнообразие здесь только вредит.
                tier="flash", temperature=0.1, max_tokens=4000)
        except llm.BadAnswer as error:
            # Модель ответила, но ответ негодный: оборван, не JSON, испорчен по
            # дороге. Это ровно то, ради чего у упаковки есть попытки, — тратим
            # одну и пробуем снова.
            #
            # Упаковка не идёт через общий проход конвейера, поэтому такая же
            # починка в pipeline её НЕ покрыла: один оборванный ответ убивал её
            # целиком, хотя цикл попыток был тут же, строкой ниже. И это худшее
            # место для такого: к упаковке автор приходит с четырьмя принятыми
            # модулями, то есть с оплаченной работой всего конвейера.
            critique = ["Предыдущий ответ не удалось разобрать: %s" % error]
            log.append({"attempt": attempt, "problems": critique, "warnings": [],
                        "usage": {}})
            continue

        data = result["data"]
        problems, warnings = validate(data, params, mechanics, components)
        log.append({"attempt": attempt, "problems": problems, "warnings": warnings,
                    "usage": result.get("usage", {})})

        if not problems:
            core = dict(known_core(params, mechanics))
            for field in MODEL_OWNED:
                core[field] = data.get(field)

            spec = {
                "game_spec": {
                    "core": {k: core.get(k) for k in CORE_FIELDS},
                    "text": spec_text(params, modules, components,
                                      rules_variant, description),
                },
                "diagnostic_meta": {
                    "actions_resolution": data.get("actions_resolution") or {},
                    "resource_roles": data.get("resource_roles") or {},
                    "tie_breaker": {
                        "applicable": core["mode"] != "cooperative",
                        "present": None, "description": None,
                    },
                    "strict_player_count": {
                        "strict": core["players"]["min"] == core["players"]["max"],
                        "declared": core["players"]["min"],
                    },
                },
                "gaps": (rules_variant or {}).get("gaps") or [],
                "source": "generator",
            }
            return {"ok": True,
                    "title": story.get("title") or "Без названия",
                    "subtitle": subtitle(params),
                    "description": description,
                    "sections": blocks,
                    "spec": spec,
                    "warnings": warnings, "attempts": attempt, "log": log,
                    "model": result.get("model")}

        critique = problems
        previous = data

    raise PackagingError(
        "Перевод механик в game_spec не прошёл проверку за %d попытки. "
        "Последние нарушения: %s" % (attempts, "; ".join(critique or [])))
