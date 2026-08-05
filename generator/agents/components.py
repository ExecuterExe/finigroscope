# -*- coding: utf-8 -*-
"""Этап 5 ТЗ: количество компонентов и материал. Считает КОД, а не модель.

Почему без модели. Правило расчёта задано таблицами целиком: базовый диапазон,
поправка на каждый ответ опросника, нижний предел, допустимые материалы. Здесь
нечего сочинять — есть чему следовать. Просить у модели то, что вычисляется
арифметикой, значит платить за вызов, получать невоспроизводимый ответ и терять
возможность объяснить каждое число.

А объяснить нужно: количество компонентов уходит в симуляцию, и когда та
покажет, что партия не сходится, первый вопрос будет «откуда взялось 45 карт».
Поэтому вместе с числом возвращается ПОЛНЫЙ след расчёта: база, каждая поправка
со своим обоснованием из таблицы, срез по нижнему пределу.

Два прохода, как в ТЗ:

  base()  — после механик (этап 2). Сколько нужно карт, жетонов, кубиков, чтобы
            игровой цикл вообще работал. Эти числа идут в симуляцию.
  final() — после особенностей (этап 4). Пересчёт с учётом того, что добавили
            сюжет и особенности, плюс выбор материала: к этому моменту известно
            всё — и жанр, и сложность, и адаптация.

Источник — agents/library/components.json, собранный tools/build_components.py
из книги Excel. Править надо книгу и пересобирать: расхождение таблицы и расчёта
не заметит никто, пока не станет поздно.
"""

import json
from pathlib import Path

LIBRARY_FILE = Path(__file__).resolve().parent / "library" / "components.json"

BUCKET_PREFIX = "#"

# Порядок предпочтения материалов: сперва дешёвые и привычные для настолок.
# Это осознанный выбор по умолчанию, а не истина: таблица говорит, что МОЖНО,
# а что взять из можного — решение, и оно должно быть видимым и одинаковым от
# запуска к запуску.
MATERIAL_PREFERENCE = ("Картон", "Бумага", "Пластик", "Дерево", "Ткань",
                       "Резина / силикон", "Металл", "Кирамика", "Стекло",
                       "Другое")

# Жёсткие ограничения, читаемые прямо из ответов опросника.
UNSAFE_FOR_LITTLE_KIDS = ("Стекло", "Металл", "Кирамика")
NOT_FOR_OUTDOORS = ("Бумага", "Картон")
OUTDOOR_LOCATIONS = ("На открытом воздухе",)

_cache = {}


class ComponentsError(Exception):
    """Расчёт невозможен. Текст пригоден для показа пользователю."""

    user_facing = True


def library():
    if "data" not in _cache:
        _cache["data"] = json.loads(LIBRARY_FILE.read_text(encoding="utf-8"))
    return _cache["data"]


# --- сопоставление ответов опросника со значениями таблицы -------------------
def _norm(text):
    return str(text or "").strip().lower().replace("–", "-").replace("—", "-")


def _range_option(value, table):
    """Диапазон {min,max} обратно в вариант опросника, которым он задан."""
    if not isinstance(value, dict):
        return None
    pair = (value.get("min"), value.get("max"))
    for option, bounds in table.items():
        if tuple(bounds) == pair:
            return _norm(option)
    return None


def _player_bucket(steps, players):
    """Числовая корзина игроков («1-4», «9+») — второй способ задать поправку."""
    top = (players or {}).get("max")
    if top is None:
        return None
    for key in steps:
        if not key.startswith(BUCKET_PREFIX):
            continue
        body = key[len(BUCKET_PREFIX):]
        low, _, high = body.partition("-")
        try:
            low = int(low)
        except ValueError:
            continue
        if top < low:
            continue
        if high and top > int(high):
            continue
        return key
    return None


def _wanted_values(param, params):
    """Какие ключи таблицы соответствуют ответам автора. Список — не редкость:
    жанров и мест игры может быть несколько."""
    import params as params_module

    if param == "age_group":
        return [_range_option(params.get("age_group"), params_module.AGE_RANGES)]
    if param == "player_count":
        return [_range_option(params.get("player_count"),
                              params_module.PLAYER_RANGES)]
    if param == "play_time":
        return [_range_option(params.get("play_time"),
                              params_module.PLAY_TIME_RANGES)]
    if param == "genre":
        return [_norm(g) for g in (params.get("genre") or [])]
    if param == "location":
        return [_norm(loc) for loc in (params.get("location") or [])]
    if param == "complexity":
        return [_norm(params.get("complexity"))]
    if param == "randomness":
        return ["true" if params.get("randomness") else "false"]
    if param == "adaptation":
        return ["true" if params.get("adaptation") else "false"]
    return []


# Как сводить несколько подходящих поправок в одну.
#
#   genre    — берём САМУЮ ТРЕБОВАТЕЛЬНУЮ (наибольшую): игра обязана обеспечить
#              каждый заявленный жанр, а не их среднее. Детективу нужно много
#              карт — значит, их нужно много, даже если второй жанр «бродилка».
#   location — наоборот, самую ОГРАНИЧИВАЮЩУЮ (наименьшую): если в игру играют
#              и дома, и в дороге, комплект обязан помещаться в дорогу.
#
# Складывать их нельзя ни в каком случае: два жанра дали бы двойную поправку за
# одно и то же требование.
COMBINE = {"genre": max, "location": min}


def _step_for(param, table, params):
    """Поправка по одному параметру: значение, число и обоснование из таблицы."""
    wanted = [v for v in _wanted_values(param, params) if v]
    hits = [(value, table[value]) for value in wanted if value in table]

    if not hits and param == "player_count":
        key = _player_bucket(table, params.get("player_count"))
        if key:
            hits = [(key, table[key])]

    if not hits and "*" in table:
        hits = [("любое", table["*"])]

    if not hits:
        return None

    if len(hits) > 1:
        pick = COMBINE.get(param, max)
        chosen = pick(hits, key=lambda item: item[1]["step"])
    else:
        chosen = hits[0]

    value, entry = chosen
    return {"param": param, "value": value, "step": entry["step"],
            "why": entry.get("why", ""), "note": entry.get("note", ""),
            "considered": [v for v, _ in hits] if len(hits) > 1 else None}


def _midpoint(low, high):
    """Середина диапазона. При дробной середине округляем ВВЕРХ: нехватка
    компонента ломает партию, а лишний лежит в коробке."""
    return -(-(low + high) // 2)


def quantity(component, params):
    """Количество одного компонента со всем следом расчёта."""
    data = library()
    base = data["base"].get(component)
    if base is None:
        return None

    steps = []
    total = 0
    for param, table in (data["steps"].get(component) or {}).items():
        row = _step_for(param, table, params)
        if row is None:
            continue
        steps.append(row)
        total += row["step"]

    low = base["base_min"] + total
    high = base["base_max"] + total

    floored = False
    if low < base["floor"]:
        # Нижний предел — запрет, а не пожелание: ниже него компонента не
        # хватает физически, сколько бы минусов ни набрали поправки.
        high = max(high, base["floor"])
        low, floored = base["floor"], True

    per_player = base.get("per_player", False)
    each = _midpoint(low, high)
    players = (params.get("player_count") or {}).get("max") or 1
    total_count = each * players if per_player else each

    return {
        "component": component,
        "base": [base["base_min"], base["base_max"]],
        "floor": base["floor"],
        "steps": steps,
        "step_total": total,
        "range": [low, high],
        "floored": floored,
        "per_player": per_player,
        "per_player_count": each if per_player else None,
        "players": players if per_player else None,
        "quantity": total_count,
        "why": base.get("why", ""),
    }


# --- материал ----------------------------------------------------------------
def materials(component, params):
    """Допустимые материалы и выбор по умолчанию — с объяснением выбора."""
    data = library()
    allowed = data["materials"].get(component) or {}
    if not allowed:
        return None

    good = [name for name, mark in allowed.items() if mark == "+"]
    maybe = [name for name, mark in allowed.items() if mark == "+/-"]

    reasons = []
    banned = set()

    age = params.get("age_group") or {}
    if (age.get("min") or 99) <= 5:
        banned |= set(UNSAFE_FOR_LITTLE_KIDS)
        reasons.append("возраст от %s лет: твёрдые и бьющиеся материалы "
                       "небезопасны" % age.get("min"))

    locations = [str(loc) for loc in (params.get("location") or [])]
    if any(loc in OUTDOOR_LOCATIONS for loc in locations):
        banned |= set(NOT_FOR_OUTDOORS)
        reasons.append("игра на открытом воздухе: бумага и картон не переживут "
                       "погоду")

    def pick(pool):
        pool = [m for m in pool if m not in banned]
        for name in MATERIAL_PREFERENCE:
            if name in pool:
                return name
        return pool[0] if pool else None

    chosen = pick(good) or pick(maybe)
    fallback = chosen is None and bool(good or maybe)
    if fallback:
        # Запреты вычистили всё. Молчать нельзя: выбор всё равно нужен, но он
        # больше не безопасен по нашим же правилам, и автор обязан это знать.
        chosen = pick(good + maybe) or (good + maybe)[0]

    return {
        "component": component,
        "chosen": chosen,
        "recommended": good,
        "acceptable": maybe,
        "excluded": sorted(banned & set(list(good) + list(maybe))),
        "reasons": reasons,
        "compromised": fallback,
    }


# --- два прохода -------------------------------------------------------------
def _requested(params):
    """Компоненты, которые автор выбрал в опроснике и которые мы умеем считать."""
    known = library()["base"]
    wanted = [c for c in (params.get("components") or []) if c in known]
    unknown = [c for c in (params.get("components") or []) if c not in known]
    return wanted, unknown


def base(params):
    """Проход 1 — после механик. Количества для симуляции, без материалов.

    Материал здесь не определяется намеренно: на этом шаге ещё нет ни
    особенностей, ни адаптации, а именно они решают, годится ли стекло. Считать
    его дважды и вторым разом переписывать — хуже, чем не считать вовсе.
    """
    wanted, unknown = _requested(params)
    if not wanted:
        raise ComponentsError(
            "Ни один из выбранных компонентов не считается по таблице: %s. "
            "Расчёт есть для: %s."
            % (", ".join(params.get("components") or ["—"]),
               ", ".join(sorted(library()["base"]))))

    rows = [quantity(c, params) for c in wanted]
    return {
        "pass": "base",
        "components": [r for r in rows if r],
        "skipped": unknown,
        "total_pieces": sum(r["quantity"] for r in rows if r),
    }


def final(params, features=None, story=None):
    """Проход 2 — после особенностей. Пересчёт плюс материал.

    ЧЕСТНО О ТЕКУЩЕМ СОСТОЯНИИ: дельта здесь пока всегда нулевая, и это не
    ошибка расчёта, а отсутствие источника. Оба прохода считаются от одних и тех
    же ответов опросника, а модули сюжета и особенностей своих требований к
    количеству компонентов ещё не выдают — им просто нечем сдвинуть число.

    Механизм дельты сделан сразу и работает: как только `features` или `story`
    начнут возвращать «нужно ещё N жетонов» или «добавьте колоду ролей», сюда
    добавляется их разбор, и разница станет видна в поле delta без правки всего
    остального. Показывать «было 40, стало 45» важнее, чем просто «45».

    Что второй проход даёт УЖЕ СЕЙЧАС — материал: его нельзя было выбрать на
    первом проходе, потому что там ещё неизвестны адаптация и место игры.
    """
    first = base(params)
    before = {r["component"]: r["quantity"] for r in first["components"]}

    rows = []
    for row in first["components"]:
        component = row["component"]
        row = dict(row)
        row["material"] = materials(component, params)
        row["was"] = before[component]
        row["delta"] = row["quantity"] - before[component]
        rows.append(row)

    return {
        "pass": "final",
        "components": rows,
        "skipped": first["skipped"],
        "total_pieces": sum(r["quantity"] for r in rows),
        "changed": [r["component"] for r in rows if r["delta"]],
    }
