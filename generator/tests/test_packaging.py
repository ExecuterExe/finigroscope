# -*- coding: utf-8 -*-
"""Упаковка: описание для человека и game_spec для машины.

Обращений к модели нет — проверяется сборка и валидатор перевода.

Две вещи здесь стоят дороже остальных. Первая: описание НИЧЕГО не сочиняет,
поэтому каждый его раздел обязан находиться в принятых модулях. Вторая:
game_spec — закрытый контракт, общий с ФинИгроСкопом; лишнее поле в core или
метрика победы, ссылающаяся в пустоту, ломают симуляционный этап молча.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents import packaging  # noqa: E402


MECHANICS = {
    "title": "Совместный поиск",
    "game_loop": {
        "turn_order": "по часовой стрелке",
        "turn_structure": ["Откройте карту", "Положите жетон", "Передайте ход"],
        "success_check": {"rule": "при 4+ на кубике находка"},
        "resource_flow": "ключи копятся в общей копилке",
        "progression": "убывающая колода",
    },
    "win_condition": {"description": "Собрать 3 ключа до конца колоды",
                      "trigger": "3 ключа в копилке"},
    "catch_up_mechanism": "Находка любого игрока засчитывается команде.",
    "randomness_role": "Кубик решает, находка это или ловушка.",
    "fit_rationale": "Кооперативный поиск подходит младшим школьникам.",
    "required_component_types": ["карты", "жетоны"],
}

STORY = {
    "title": "Ключи старой библиотеки",
    "setting": "Старая волшебная библиотека",
    "synopsis": "Волшебник спрятал ключи, и двери не открыть.",
    "artifacts": [
        {"component": "карты", "name": "Комнаты библиотеки", "role": "места поиска"},
        {"component": "жетоны", "name": "Ключи", "role": "то, что ищут"},
    ],
}

FEATURES = {
    "concept": "Цель игры — развлечение. Игра рассчитана на 2-4 игроков 6-9 лет.",
    "features": [{"title": "Общая копилка", "description": "Ключи общие."}],
    "catch_up_help": "Находка любого приближает общую победу.",
}

MODULES = {"mechanics": MECHANICS, "story": STORY, "features": FEATURES}

COMPONENTS = [
    {"component": "карты", "quantity": 35, "material": {"chosen": "Картон"}},
    {"component": "жетоны", "quantity": 12, "material": {"chosen": "Картон"}},
]

RULES = {
    "setup": ["Перемешайте карты.", "Жетоны сложите рядом."],
    "turn": ["Откройте карту.", "Передайте ход."],
    "special_rules": [{"title": "Без вылета", "text": "Пропуск хода вместо выбывания."}],
    "ending": "Партия кончается, когда собраны 3 ключа.",
    "tips": [{"title": "Первая партия", "text": "Играйте открытыми картами."}],
    "gaps": ["Не сказано, что при пустой колоде."],
}


def params(**over):
    base = {
        "player_count": {"min": 2, "max": 4},
        "age_group": {"min": 6, "max": 9},
        "play_time": {"min": 5, "max": 15},
        "interaction": "кооперативное",
        "elimination": False,
        "catch_up": True,
        "randomness": True,
    }
    base.update(over)
    return base


def translation(**over):
    base = {
        "turn": {"order": "по часовой", "actions": ["draw_card", "place_token"]},
        "randomness": [{"type": "dice_roll", "outcomes": {"4-6": "находка"}}],
        "resources": [{"name": "keys", "scope": "shared", "start": 0, "goal": 3}],
        "win_condition": {"type": "collect_set", "metric": "keys", "threshold": 3},
        "loss_condition": {"type": "deck_exhausted"},
        "limits": {"max_rounds": None},
        "actions_resolution": {"draw_card": "probabilistic",
                               "place_token": "deterministic"},
        "resource_roles": {"keys": "win_metric"},
    }
    base.update(over)
    return base


def check(data, p=None):
    return packaging.validate(data, p or params(), MECHANICS, COMPONENTS)


# --------------------------------------------------------------------------
# Описание: ничего не сочиняем
# --------------------------------------------------------------------------

def test_разделы_идут_в_порядке_приложения_а():
    text = packaging.describe(params(), MODULES, COMPONENTS, RULES)
    order = ["Концепция", "Жанр и механики", "Сюжет", "Особенности игры",
             "Артефакты", "Краткие правила", "Условия победы",
             "Элементы случайности", "Как игра помогает отстающим",
             "Советы и рекомендации"]
    positions = [text.index("## " + name) for name in order]
    assert positions == sorted(positions), "разделы переставлены"


def test_название_стоит_заголовком():
    text = packaging.describe(params(), MODULES, COMPONENTS, RULES)
    assert text.startswith("# Ключи старой библиотеки")


def test_текст_разделов_взят_из_принятых_модулей():
    text = packaging.describe(params(), MODULES, COMPONENTS, RULES)
    assert FEATURES["concept"] in text
    assert STORY["synopsis"] in text
    assert MECHANICS["randomness_role"] in text
    assert FEATURES["catch_up_help"] in text
    assert RULES["ending"] in text


def test_артефакты_по_шаблону_с_количеством_и_материалом():
    text = packaging.describe(params(), MODULES, COMPONENTS, RULES)
    assert "Для игры понадобятся:" in text
    assert "Комнаты библиотеки — 35 шт. («Картон»)" in text
    assert "Ключи — 12 шт. («Картон»)" in text


def test_пустой_раздел_пропускается_а_не_заполняется():
    """У абстрактной игры сюжета нет — это факт, а не пробел."""
    modules = dict(MODULES, story=dict(STORY, synopsis=None))
    text = packaging.describe(params(), modules, COMPONENTS, RULES)
    assert "## Сюжет" not in text
    assert "## Концепция" in text


def test_подзаголовок_собирает_аудиторию():
    text = packaging.describe(params(), MODULES, COMPONENTS, RULES)
    assert "2–4 игрока" in text
    assert "6–9 лет" in text


def test_один_игрок_не_превращается_в_диапазон():
    text = packaging.describe(params(player_count={"min": 1, "max": 1}),
                              MODULES, COMPONENTS, RULES)
    assert "1 игрок" in text and "1–1" not in text


def test_шаги_правил_нумеруются():
    text = packaging.describe(params(), MODULES, COMPONENTS, RULES)
    assert "1. Перемешайте карты." in text
    assert "2. Передайте ход." in text


# --------------------------------------------------------------------------
# Что заполняет код, а не модель
# --------------------------------------------------------------------------

def test_режим_переводится_из_ответа_опросника():
    """Оценщик статистик читает cooperative — ошибка меняет разбор ничьих."""
    assert packaging.known_core(params(), MECHANICS)["mode"] == "cooperative"
    assert packaging.known_core(params(interaction="конкурентное"),
                                MECHANICS)["mode"] == "competitive"
    assert packaging.known_core(params(interaction="соло"),
                                MECHANICS)["mode"] == "solo"


def test_косвенное_взаимодействие_это_соревнование():
    """Мешать друг другу нельзя, но победитель один."""
    assert packaging.known_core(params(interaction="косвенное"),
                                MECHANICS)["mode"] == "competitive"


def test_помощь_отстающим_берётся_из_механик():
    core = packaging.known_core(params(), MECHANICS)
    assert core["catch_up"]["enabled"] is True
    assert core["catch_up"]["mechanism"] == MECHANICS["catch_up_mechanism"]


def test_компоненты_соединяют_имя_и_расчёт():
    rows = packaging.spec_components(COMPONENTS, STORY)
    assert rows[0] == {"name": "Комнаты библиотеки", "qty": 35,
                       "material": "Картон", "function": "места поиска"}


def test_безымянный_компонент_остаётся_под_родовым_именем():
    rows = packaging.spec_components(COMPONENTS, {"artifacts": []})
    assert rows[0]["name"] == "карты"


def test_имена_компонентов_в_описании_и_в_контракте_совпадают():
    """Иначе дальше по конвейеру это будут два разных компонента."""
    text = packaging.describe(params(), MODULES, COMPONENTS, RULES)
    for row in packaging.spec_components(COMPONENTS, STORY):
        assert row["name"] in text, row["name"]


# --------------------------------------------------------------------------
# Валидатор перевода
# --------------------------------------------------------------------------

def test_нормальный_перевод_принимается():
    problems, warnings = check(translation())
    assert problems == [], problems
    assert warnings == []


def test_без_действий_симулировать_нечего():
    problems, _ = check(translation(turn={"order": "по часовой", "actions": []}))
    assert any("turn.actions пуст" in p for p in problems)


def test_непокрытое_действие_ловится():
    """Рассинхрон бесшумно обнуляет часть тестов диагноста."""
    problems, _ = check(translation(actions_resolution={"draw_card": "probabilistic"}))
    assert any("не покрывает действия: place_token" in p for p in problems)


def test_лишнее_действие_в_разборе_ловится():
    resolution = {"draw_card": "probabilistic", "place_token": "deterministic",
                  "fly_away": "deterministic"}
    problems, _ = check(translation(actions_resolution=resolution))
    assert any("вне turn.actions: fly_away" in p for p in problems)


def test_неизвестный_способ_разрешения_ловится():
    resolution = {"draw_card": "магия", "place_token": "deterministic"}
    problems, _ = check(translation(actions_resolution=resolution))
    assert any("Неизвестный способ" in p for p in problems)


def test_метрика_победы_обязана_быть_ресурсом():
    """Ссылка в пустоту означает, что симулятор не найдёт, что считать."""
    problems, _ = check(translation(
        win_condition={"type": "most", "metric": "очки", "threshold": None}))
    assert any("не найдено среди ресурсов" in p for p in problems)


def test_метрика_может_отсутствовать():
    problems, _ = check(translation(
        win_condition={"type": "survive", "metric": None, "threshold": None}))
    assert problems == []


def test_повторяющиеся_ресурсы_ловятся():
    twice = [{"name": "keys", "scope": "shared", "start": 0, "goal": 3},
             {"name": "keys", "scope": "per_player", "start": 0, "goal": None}]
    problems, _ = check(translation(resources=twice))
    assert any("повторяются" in p for p in problems)


def test_чужой_scope_ловится():
    odd = [{"name": "keys", "scope": "общий", "start": 0, "goal": 3}]
    problems, _ = check(translation(resources=odd, resource_roles={"keys": "win_metric"},
                                    win_condition={"type": "collect_set",
                                                   "metric": "keys", "threshold": 3}))
    assert any("scope=«общий»" in p for p in problems)


def test_роль_несуществующего_ресурса_ловится():
    problems, _ = check(translation(resource_roles={"очки": "win_metric"}))
    assert any("несуществующие ресурсы" in p for p in problems)


def test_случайность_при_её_запрете_не_принимается():
    problems, _ = check(translation(), p=params(randomness=False))
    assert any("без случайности" in p for p in problems)


def test_отсутствие_случайности_при_её_заказе_это_предупреждение():
    _, warnings = check(translation(randomness=[]))
    assert any("источников в переводе нет" in w for w in warnings)


def test_порог_вне_механик_это_предупреждение():
    """Отказывать нельзя: порог мог быть выражен словами."""
    problems, warnings = check(translation(
        win_condition={"type": "collect_set", "metric": "keys", "threshold": 99}))
    assert problems == []
    assert any("не встречается в модуле механик" in w for w in warnings)


# --------------------------------------------------------------------------
# Вход упаковщика
# --------------------------------------------------------------------------

def test_без_модулей_упаковывать_нечего():
    with pytest.raises(packaging.PackagingError) as error:
        packaging.assemble(params(), {"mechanics": MECHANICS}, COMPONENTS, RULES)
    text = str(error.value)
    assert "story" in text and "features" in text


def test_ошибка_упаковки_показывается_пользователю():
    assert packaging.PackagingError.user_facing is True


def test_в_промпт_не_уходят_поля_которые_знает_код():
    """Иначе модель начнёт их «уточнять», а они уже посчитаны."""
    message = packaging.build_user_message(params(), MECHANICS, STORY)
    assert "не заполняй эти поля" in message
    assert "cooperative" in message
