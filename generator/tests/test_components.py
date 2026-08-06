# -*- coding: utf-8 -*-
"""Этап 5: количество компонентов и материал.

Обращений к модели нет и быть не может: расчёт детерминированный.

Что здесь важно проверить. Числа уходят в симуляцию, и когда та покажет, что
партия не сходится, первым делом спросят «откуда взялось 55 карт». Значит,
проверять надо не только итог, но и то, что след расчёта его объясняет, а
правила сведения нескольких поправок не превращаются в двойной счёт.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents import components as C  # noqa: E402


def make(**overrides):
    params = {
        "age_group": {"min": 18, "max": 35},
        "genre": ["кооператив"],
        "randomness": True,
        "location": ["Дом"],
        "player_count": {"min": 2, "max": 4},
        "play_time": {"min": 30, "max": 60},
        "complexity": "средняя",
        "adaptation": False,
        "components": ["карты", "кубики", "жетоны", "фишки"],
    }
    params.update(overrides)
    return params


# --------------------------------------------------------------------------
# Библиотека собрана из книги и совпадает с ней
# --------------------------------------------------------------------------

def test_библиотека_содержит_все_считаемые_компоненты():
    base = C.library()["base"]
    assert set(base) == {"карты", "игровое поле", "кубики", "фишки",
                         "жетоны", "таймер", "фигурки"}


def test_базовые_значения_взяты_из_книги():
    base = C.library()["base"]
    assert (base["карты"]["base_min"], base["карты"]["base_max"]) == (30, 40)
    assert base["карты"]["floor"] == 15
    # фишки в книге заданы «4–6 шт. НА ИГРОКА» — это меняет весь расчёт
    assert base["фишки"]["per_player"] is True
    assert base["карты"]["per_player"] is False


# --------------------------------------------------------------------------
# Расчёт количества
# --------------------------------------------------------------------------

def test_середина_диапазона_и_есть_ответ():
    row = C.quantity("карты", make(genre=["кооператив"], randomness=False,
                                   age_group={"min": 18, "max": 35}))
    # кооператив 0, «нет случайности» -10 -> 20–30, середина 25
    assert row["range"] == [20, 30]
    assert row["quantity"] == 25


def test_нет_случайности_берёт_строку_полной_детерминированности():
    """В книге два «нет», в опроснике — один вариант, и назван он «Нет
    случайности (полная детерминированность)». Совпадать надо с ним."""
    шаг = C.library()["steps"]["карты"]["randomness"]["false"]
    assert шаг["step"] == -10
    assert "минимум карт" in шаг["why"].lower()


def test_каждая_поправка_объяснена():
    """Число без следа расчёта непроверяемо: симуляция покажет «не сходится»,
    а откуда оно взялось — неизвестно."""
    row = C.quantity("карты", make(genre=["детектив"]))
    assert row["step_total"] == 20            # детектив +10, случайность +10
    assert row["quantity"] == 55
    for step in row["steps"]:
        assert step["param"]
        assert step["value"]
        if step["step"]:
            assert step["why"], "поправка без обоснования из книги"


def test_несколько_жанров_не_складываются():
    """Иначе два жанра дали бы двойную поправку за одно и то же требование."""
    один = C.quantity("карты", make(genre=["детектив"]))["step_total"]
    два = C.quantity("карты", make(genre=["детектив", "приключение"]))
    assert два["step_total"] == один, "поправки жанров сложились"
    # берём самую требовательную: детективу нужно больше, чем приключению
    genre_step = [s for s in два["steps"] if s["param"] == "genre"][0]
    assert genre_step["value"] == "детектив"
    assert genre_step["considered"] == ["детектив", "приключение"]


def test_место_игры_берётся_самое_ограничивающее():
    """Играют и дома, и в дороге — комплект обязан поместиться в дорогу."""
    row = C.quantity("карты", make(location=["Дом", "Дорога/в пути"]))
    step = [s for s in row["steps"] if s["param"] == "location"][0]
    assert step["value"] == "дорога/в пути"
    assert step["step"] < 0


def test_нижний_предел_это_запрет_а_не_пожелание():
    """Сколько бы минусов ни набрали поправки, ниже предела компонента не
    хватает физически."""
    row = C.quantity("карты", make(
        age_group={"min": 3, "max": 5}, genre=["бродилка"], randomness=False,
        location=["Дорога/в пути"], play_time={"min": 5, "max": 15},
        complexity="низкая", adaptation=True))
    assert row["floored"] is True
    assert row["range"][0] == C.library()["base"]["карты"]["floor"]
    assert row["quantity"] >= row["floor"]


def test_фишки_считаются_на_каждого_игрока():
    row = C.quantity("фишки", make(player_count={"min": 2, "max": 6}))
    assert row["per_player"] is True
    assert row["players"] == 6
    assert row["quantity"] == row["per_player_count"] * 6


def test_дробная_середина_округляется_вверх():
    """Нехватка компонента ломает партию, лишний лежит в коробке."""
    assert C._midpoint(25, 30) == 28          # 27.5 -> 28
    assert C._midpoint(30, 40) == 35


def test_таймер_считается_по_числовым_корзинам():
    """У таймера в книге свои границы («1–4 игрока»), а не варианты опросника."""
    мало = C.quantity("таймер", make(player_count={"min": 1, "max": 4}))
    много = C.quantity("таймер", make(player_count={"min": 10, "max": 24}))
    шаги = [s for s in много["steps"] if s["param"] == "player_count"]
    assert шаги, "корзина игроков не сработала"
    assert много["quantity"] >= мало["quantity"]


def test_игровое_поле_всегда_одно():
    row = C.quantity("игровое поле", make(player_count={"min": 2, "max": 12}))
    assert row["quantity"] == 1


# --------------------------------------------------------------------------
# Материал
# --------------------------------------------------------------------------

def test_материал_выбирается_из_разрешённых_книгой():
    m = C.materials("карты", make())
    allowed = C.library()["materials"]["карты"]
    assert allowed[m["chosen"]] == "+"
    assert "Металл" not in m["recommended"]        # в книге у карт «-»


def test_малышам_не_дают_стекло_и_металл():
    m = C.materials("кубики", make(age_group={"min": 3, "max": 5}))
    assert m["chosen"] not in C.UNSAFE_FOR_LITTLE_KIDS
    assert m["excluded"], "опасные материалы не отмечены как исключённые"
    assert any("возраст" in r for r in m["reasons"])


def test_на_улице_не_дают_бумагу_и_картон():
    m = C.materials("карты", make(location=["На открытом воздухе"]))
    assert m["chosen"] not in C.NOT_FOR_OUTDOORS
    assert any("воздух" in r for r in m["reasons"])


def test_выбор_повторяем():
    """Один и тот же ввод обязан давать один и тот же материал: иначе две
    сборки одной игры разойдутся без всякой причины."""
    первый = C.materials("жетоны", make())["chosen"]
    for _ in range(5):
        assert C.materials("жетоны", make())["chosen"] == первый


# --------------------------------------------------------------------------
# Два прохода
# --------------------------------------------------------------------------

def test_первый_проход_без_материалов():
    """На этом шаге ещё нет особенностей и адаптации — а они и решают, годится
    ли стекло. Считать материал дважды хуже, чем не считать сразу."""
    out = C.base(make())
    assert out["pass"] == "base"
    assert all("material" not in r for r in out["components"])
    assert out["total_pieces"] > 0


def test_второй_проход_добавляет_материал_и_дельту():
    out = C.final(make())
    assert out["pass"] == "final"
    for row in out["components"]:
        assert row["material"]["chosen"]
        assert "was" in row and "delta" in row


# --------------------------------------------------------------------------
# Заявки модулей на дополнительные предметы
# --------------------------------------------------------------------------

def просит(component, count, per_player=False, why="под приём"):
    return {C.EXTRA_FIELD: [{"component": component, "count": count,
                             "per_player": per_player, "why": why}]}


def test_без_заявок_дельта_нулевая():
    """Базовый комплект посчитан по таблицам, и трогать его без причины нельзя."""
    out = C.final(make(), {"story": {}, "features": {}})
    assert all(r["delta"] == 0 for r in out["components"])
    assert out["changed"] == []


def test_заявка_особенностей_увеличивает_количество():
    базовый = C.final(make())
    итог = C.final(make(), {"features": просит("жетоны", 5)})

    было = {r["component"]: r["quantity"] for r in базовый["components"]}
    строка = [r for r in итог["components"] if r["component"] == "жетоны"][0]

    assert строка["quantity"] == было["жетоны"] + 5
    assert строка["delta"] == 5
    assert строка["was"] == было["жетоны"]
    assert "жетоны" in итог["changed"]


def test_дельта_объяснена_а_не_просто_посчитана():
    """«Стало 45» ничего не объясняет — автор не сможет ни проверить, ни оспорить."""
    итог = C.final(make(), {"features": просит("жетоны", 5, why="жетоны подсказок")})
    строка = [r for r in итог["components"] if r["component"] == "жетоны"][0]
    заявка = строка["extras"][0]
    assert заявка["phase"] == "features"
    assert заявка["why"] == "жетоны подсказок"
    assert заявка["pieces"] == 5


def test_на_игрока_умножается_программой():
    """Сколько игроков — знает опросник, а не тот, кто просит."""
    итог = C.final(make(player_count={"min": 2, "max": 6}),
                   {"story": просит("карты", 3, per_player=True)})
    строка = [r for r in итог["components"] if r["component"] == "карты"][0]
    assert строка["delta"] == 3 * 6
    assert строка["extras"][0]["pieces"] == 18


def test_заявки_разных_модулей_складываются():
    итог = C.final(make(), {"story": просит("карты", 4),
                            "features": просит("карты", 6)})
    строка = [r for r in итог["components"] if r["component"] == "карты"][0]
    assert строка["delta"] == 10
    assert {e["phase"] for e in строка["extras"]} == {"story", "features"}


def test_итого_предметов_учитывает_заявки():
    базовый = C.final(make())["total_pieces"]
    итог = C.final(make(), {"features": просит("жетоны", 7)})["total_pieces"]
    assert итог == базовый + 7


# --- что отклоняется, и почему это не молча ---------------------------------

@pytest.mark.parametrize("заявка,кусок_причины", [
    ({"component": "фигурки", "count": 3, "why": "зачем"}, "не выбирал"),
    ({"component": "жетоны", "count": 0, "why": "зачем"}, "положительным"),
    ({"component": "жетоны", "count": -5, "why": "зачем"}, "положительным"),
    ({"component": "жетоны", "count": "пять", "why": "зачем"}, "положительным"),
    ({"component": "жетоны", "count": 999, "why": "зачем"}, "предела"),
    ({"component": "жетоны", "count": 3, "why": "  "}, "зачем"),
])
def test_негодная_заявка_отклоняется_с_причиной(заявка, кусок_причины):
    итог = C.final(make(), {"features": {C.EXTRA_FIELD: [заявка]}})
    assert len(итог["rejected_extras"]) == 1
    assert кусок_причины in итог["rejected_extras"][0]["reason"]
    # и количество при этом не изменилось
    assert all(r["delta"] == 0 for r in итог["components"])


def test_отклонённая_заявка_не_пропадает_молча():
    """Модуль на неё рассчитывает: правила сошлются на предмет, которого нет."""
    итог = C.final(make(), {"story": просит("фигурки", 3)})
    assert итог["rejected_extras"], "заявка исчезла без следа"
    assert итог["rejected_extras"][0]["phase"] == "story"


def test_предел_на_заявку_защищает_от_опечатки_модели():
    """«700 жетонов» приедет в напечатанную коробку, и заметить будет некому."""
    assert 0 < C.MAX_EXTRA_COUNT <= 100


def test_неизвестные_компоненты_не_теряются_молча():
    out = C.base(make(components=["карты", "телефоны"]))
    assert out["skipped"] == ["телефоны"]


def test_если_считать_нечего_говорим_прямо():
    with pytest.raises(C.ComponentsError) as error:
        C.base(make(components=["телефоны"]))
    assert "телефоны" in str(error.value)
    # подсказываем, что вообще умеем считать
    assert "карты" in str(error.value)
