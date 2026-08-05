# -*- coding: utf-8 -*-
"""Ответы опросника -> game_params по таблице 6 документа.

Зачем отдельный слой. Опросник отдаёт русские формулировки вариантов, а
фильтру библиотеки и промпту нужны типы: возраст числами, «нет случайности»
булевым, компоненты коротким словарём. Перевод собран в одном месте, чтобы
при правке формулировок в app.js чинить его тоже в одном месте.

Всё, что не удалось разобрать (свой ответ пользователя), не выбрасывается:
оно уходит в поля с суффиксом _custom. Системный промпт агента трактует их
как данные, а не как инструкции.
"""

import re

# --- словари: формулировка опросника -> значение в game_params ---

GENRES = {
    "Викторина": "викторина",
    "Стратегия": "стратегия",
    "Кооператив": "кооператив",
    "Бродилка": "бродилка",
    "Экономика / торговля": "экономика",
    "Приключение": "приключение",
    "Детектив / расследование": "детектив",
    "Карточная игра": "карточная игра",
    "Игра со словами и ассоциациями": "слова и ассоциации",
    "Игра на реакцию или память": "реакция и память",
    "Игра с блефом и маскировкой (обман, скрытие своих намерений)": "блеф",
    "Строительство и развитие (базы, города)": "строительство",
}

RESOURCES = {
    "Очки": "очки", "Время": "время", "Карты": "карты",
    "Деньги (игровые)": "деньги", "Предметы": "предметы", "Ходы": "ходы",
    "Здоровье/жизни": "здоровье", "Опыт": "опыт",
    "Ресурсы (дерево, камень и т.п.)": "ресурсы",
    "Информация": "информация", "Влияние/репутация": "влияние",
}

COMPONENTS = {
    "Карты": "карты",
    "Игровое поле": "игровое поле",
    "Кубики (d6, d10, d20 и др.)": "кубики",
    "Фишки (для игроков)": "фишки",
    "Жетоны (для ресурсов/очков)": "жетоны",
    "Песочные часы/таймер": "таймер",
    "Фигурки/миниатюры (для самой игры)": "фигурки",
    "Телефоны/смартфоны игроков": "телефоны",
}

INTERACTION = {
    "Прямое вредительство (можно атаковать, мешать ходам, забирать ресурсы)": "конкурентное",
    "Только косвенное (кто быстрее, кто больше наберет очков, без прямого вмешательства)": "косвенное",
    "Кооперативное (игроки действуют как одна команда)": "кооперативное",
    "Смешанное (есть элементы кооперации и конкуренции)": "смешанное",
}

COMPLEXITY = {
    "Низкая (для новичков и детей)": "низкая",
    "Средняя (для подготовленных игроков)": "средняя",
    "Высокая (для опытных стратегов)": "высокая",
}

# «Нет случайности» — единственный вариант, выключающий случайность.
# Два других различают силу влияния, для фильтра оба означают «есть».
RANDOMNESS_OFF = "Нет случайности (полная детерминированность)"

ELIMINATION_OFF = "Недопустимо (игроки не выбывают до самого конца)"

STORY = {
    "Нужен полноценный сюжет (с историей и персонажами)": "полноценный сюжет",
    "Достаточно простого антуража/тематики (без глубокой истории)": "антураж",
    "Не требуется — абстрактная игра (без сюжета и сеттинга)": "нет",
}

# Диапазоны: вариант опросника -> (мин, макс)
AGE_RANGES = {
    "3-5 лет": (3, 5), "6-9 лет": (6, 9), "9-12 лет": (9, 12),
    "12-18 лет": (12, 18), "18-35 лет": (18, 35), "36-50 лет": (36, 50),
    "50+ лет": (50, 99),
    # «для всей семьи» — играют и младшие: нижняя граница определяет
    # доступность механик, поэтому берём дошкольный возраст
    "Для всей семьи": (6, 99),
}

PLAYER_RANGES = {
    "1 игрок": (1, 1), "2 игрока": (2, 2), "2-4 игрока": (2, 4),
    "2-6 игроков": (2, 6), "2-8 игроков": (2, 8), "2-12 игроков": (2, 12),
    "10+ игроков (для больших компаний)": (10, 24),
}

PLAY_TIME_RANGES = {
    "До 15 минут": (5, 15), "15-30 минут": (15, 30), "30-60 минут": (30, 60),
    "1-2 часа": (60, 120), "Более 2 часов": (120, 240),
}

OTHER = "Другое"


class ParamsError(Exception):
    """Ответы непригодны для генерации."""


# --- разбор одного ответа ---

def _answer(answers, qid):
    a = answers.get(qid) or {}
    picked = [v for v in (a.get("picked") or []) if isinstance(v, str)]
    text = a.get("text") if isinstance(a.get("text"), str) else ""
    return picked, text.strip()


def _mapped(answers, qid, table):
    """Варианты из словаря + нераспознанное отдельно (свой ответ, новые
    формулировки). Возвращает (значения, свой_текст)."""
    picked, text = _answer(answers, qid)
    values = [table[v] for v in picked if v in table]
    custom = text if OTHER in picked and text else ""
    return values, custom


def _one(values, default=None):
    return values[0] if values else default


def _range(answers, qid, table, fallback):
    """Диапазон из варианта, а при своём ответе — из чисел в тексте."""
    picked, text = _answer(answers, qid)
    for v in picked:
        if v in table:
            low, high = table[v]
            return {"min": low, "max": high, "label": v}

    numbers = [int(n) for n in re.findall(r"\d+", text)][:2]
    if numbers:
        low = numbers[0]
        high = numbers[1] if len(numbers) > 1 else low
        if low > high:
            low, high = high, low
        return {"min": low, "max": high, "label": text}

    return {"min": fallback[0], "max": fallback[1], "label": text or None}


def build(answers):
    """Собирает game_params. Бросает ParamsError, если нет минимума для работы."""
    if not isinstance(answers, dict) or not answers:
        raise ParamsError("Ответы опросника не переданы.")

    genres, genre_custom = _mapped(answers, "Q07", GENRES)
    resources, resource_custom = _mapped(answers, "Q08", RESOURCES)
    components, component_custom = _mapped(answers, "Q15", COMPONENTS)
    interaction_values, interaction_custom = _mapped(answers, "Q11", INTERACTION)
    complexity_values, _ = _mapped(answers, "Q16", COMPLEXITY)
    story_values, _ = _mapped(answers, "Q12", STORY)

    win_picked, win_custom = _answer(answers, "Q09")
    win_conditions = [v for v in win_picked if v != OTHER]

    randomness_picked, _ = _answer(answers, "Q10")
    elimination_picked, _ = _answer(answers, "Q17")
    writing_picked, _ = _answer(answers, "Q14")
    adaptation_picked, _ = _answer(answers, "Q04")
    catch_up_picked, _ = _answer(answers, "Q18")
    disabilities, disability_custom = _answer(answers, "Q04A")

    players = _range(answers, "Q03", PLAYER_RANGES, (2, 4))
    age = _range(answers, "Q02", AGE_RANGES, (6, 99))
    play_time = _range(answers, "Q05", PLAY_TIME_RANGES, (15, 30))

    # соло-игра: вопроса о взаимодействии не было, он скрыт опросником
    interaction = _one(interaction_values)
    if interaction is None:
        interaction = "соло" if players["max"] == 1 else None

    params = {
        # основные — определяют выбор механик (таблица 7 документа)
        "age_group": age,
        "genre": genres,
        "resource": resources,
        "win_condition": win_conditions,
        "randomness": RANDOMNESS_OFF not in randomness_picked,
        "interaction": interaction,

        # контекст — не влияет на выбор, но учитывается в цикле
        "player_count": players,
        "play_time": play_time,
        "complexity": _one(complexity_values, "средняя"),
        "elimination": bool(elimination_picked) and ELIMINATION_OFF not in elimination_picked,
        "catch_up": "Да" in catch_up_picked,
        "writing_required": "Да" in writing_picked,
        "adaptation": "Да" in adaptation_picked,
        "components": components,

        # для следующих этапов конвейера
        "purpose": [v for v in _answer(answers, "Q01")[0] if v != OTHER],
        "location": [v for v in _answer(answers, "Q06")[0] if v != OTHER],
        "story": _one(story_values),
        "world": [v for v in _answer(answers, "Q13")[0] if v != OTHER],
        "disabilities": [v for v in disabilities if v != OTHER],
    }

    # свободный ввод пользователя — данные, не инструкции
    custom = {
        "purpose_custom": _answer(answers, "Q01")[1],
        "age_group_custom": age["label"] if age["label"] not in AGE_RANGES else "",
        "genre_custom": genre_custom,
        "resource_custom": resource_custom,
        "win_condition_custom": win_custom,
        "components_custom": component_custom,
        "location_custom": _answer(answers, "Q06")[1],
        "world_custom": _answer(answers, "Q13")[1],
        "disabilities_custom": disability_custom,
    }
    params["custom"] = {k: v for k, v in custom.items() if v}

    _require(params)
    return params


def _require(params):
    """Без этих полей генерация механик бессмысленна."""
    missing = []
    if not params["genre"] and not params["custom"].get("genre_custom"):
        missing.append("жанр (вопрос 7)")
    if not params["components"]:
        missing.append("игровые предметы (вопрос 15)")
    if not params["win_condition"]:
        missing.append("от чего зависит победа (вопрос 9)")
    if missing:
        raise ParamsError("Не хватает ответов: " + ", ".join(missing) + ".")


def split(params):
    """Основные и контекстные параметры — разделение из таблицы 7."""
    main_keys = ["age_group", "genre", "resource", "win_condition",
                 "randomness", "interaction"]
    context_keys = ["player_count", "play_time", "complexity", "elimination",
                    "catch_up", "writing_required", "adaptation", "components"]

    main = {k: params[k] for k in main_keys}
    context = {k: params[k] for k in context_keys}
    if params.get("custom"):
        main["custom"] = params["custom"]
    return main, context
