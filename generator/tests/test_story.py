# -*- coding: utf-8 -*-
"""Агент сюжета: фильтр библиотеки и детерминированные проверки ответа.

Обращений к модели нет. Проверяется ровно то, за что отвечает код агента:
какие завязки он вообще даёт выбрать и какой ответ модели отказывается принять.
Качество самого сюжета кодом не проверяется никогда — за это отвечают аудитор и
линзы, и подменять их регулярным выражением было бы самообманом.

Главное, что здесь защищается: сюжет не имеет права менять принятые механики.
Модуль механик к этому моменту стоил двух платных проверок, и порча его текстом
сюжета — самый дорогой из возможных сбоев этапа.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents import story  # noqa: E402


MECHANICS = {
    "title": "Совместный поиск",
    "game_loop": {"turn_order": "по часовой стрелке"},
    "win_condition": {"description": "собрать три предмета"},
    "required_component_types": ["карты", "жетоны", "кубики"],
}


def params(**over):
    base = {
        "story": story.DEPTH_FULL,
        "world": ["Фэнтези"],
        "genre": ["приключение"],
        "age_group": {"min": 6, "max": 9},
        "interaction": "кооперативное",
        "purpose": ["Развлечение"],
        "components": ["карты", "жетоны", "кубики"],
    }
    base.update(over)
    return base


SYNOPSIS = ("Волшебник спрятал ключи от старой библиотеки в трёх залах, и "
            "теперь двери не открыть. Юные хранители отправляются искать их, "
            "пока свеча в главном зале не догорела. Каждый зал хранит подсказку "
            "о следующем, а вместе ключи снова откроют двери для всех.")


def variant(number=1, **over):
    base = {
        "variant_id": number,
        "seed_id": "STORY_STOLEN_TREASURE",
        "title": "Ключи старой библиотеки",
        # без числительного: «найти три ключа» — уже названное количество,
        # и проверка отклонит такой вариант (см. тест ниже)
        "logline": "Найти все ключи до того, как догорит свеча.",
        "setting": "Старая волшебная библиотека",
        "synopsis": SYNOPSIS,
        "player_role": "юные хранители",
        "characters": [{"name": "Виллем", "role": "старый библиотекарь"}],
        "stakes": "Если свеча догорит, двери останутся закрытыми навсегда.",
        "ending": "Ключи вставлены в замок, и библиотека снова открыта для всех.",
        "artifacts": [
            {"component": "карты", "name": "Залы библиотеки", "role": "места поиска"},
            {"component": "жетоны", "name": "Ключи", "role": "то, что ищут"},
            {"component": "кубики", "name": "Свеча", "role": "отмеряет время"},
        ],
        "fit_rationale": "Кооперативный поиск ложится на совместный сбор предметов.",
        "risks": ["детям может быть непонятно, зачем нужны ключи"],
    }
    base.update(over)
    return base


def answer(*variants, **over):
    base = {
        "error": None,
        "invented_seeds": [],
        "variants": list(variants) or [variant(1), variant(2), variant(3)],
        "recommended_variant_id": 1,
        "self_check": {"depth_matches_story_param": True},
    }
    base.update(over)
    return base


def three(**over):
    """Три различающихся варианта: одинаковая завязка — отдельное нарушение."""
    seeds = ["STORY_STOLEN_TREASURE", "STORY_SEALED_DOOR", "STORY_HELPING_HAND"]
    if "seed_id" in over:                      # намеренно одна завязка на всех
        seeds = [over.pop("seed_id")] * 3
    return [variant(i + 1, seed_id=s, **over) for i, s in enumerate(seeds)]


def check(data, p=None, seeds=None, mode=story.MODE_STRICT):
    p = p or params()
    seeds = seeds if seeds is not None else story.filter_library(p)[0]
    return story.validate(data, seeds, p, MECHANICS, mode)


def problems_of(data, **kwargs):
    return check(data, **kwargs)[0]


# --------------------------------------------------------------------------
# Фильтр библиотеки
# --------------------------------------------------------------------------

def test_библиотека_читается_и_завязки_описаны_полностью():
    library = story.load_library()
    assert library["seeds"], "библиотека пуста"
    for seed in library["seeds"]:
        for field in ("id", "name", "premise", "conflict", "goal_shape", "tags"):
            assert seed.get(field), "у %s нет поля %s" % (seed.get("id"), field)


def test_мир_сужает_выбор():
    fantasy = {s["id"] for s in story.filter_library(params(world=["Фэнтези"]))[0]}
    business = {s["id"] for s in story.filter_library(
        params(world=["Бизнес"], genre=["экономика"], age_group={"min": 18, "max": 35},
               interaction="конкурентное"))[0]}
    assert fantasy and business
    assert fantasy != business


def test_пугающие_завязки_не_доходят_до_младших():
    """Дешевле отсеять кодом, чем ловить потом аудитором."""
    young = params(age_group={"min": 6, "max": 9}, world=["Постапокалипсис"],
                   genre=["стратегия"])
    kept, dropped = story.filter_library(young)
    assert all(not s["tags"]["scary"] for s in kept)
    assert any("страх" in d["reason"] for d in dropped)


def test_кооперативу_не_дают_завязку_на_противостоянии():
    """Механики уже приняты — сюжет не может с ними спорить.

    Библиотека проверяется подставная: в настоящей завязки на противостоянии и
    так не помечены кооперативным взаимодействием, и до этой проверки дело не
    доходит. Но она — не украшение: она страхует от завязки, которую однажды
    добавят с обоими признаками сразу, и тогда сюжет начал бы спорить с уже
    принятыми механиками.
    """
    trap = {"seeds": [{
        "id": "STORY_TRAP", "name": "Ловушка", "premise": "п", "conflict": "к",
        "player_role": "р", "goal_shape": "ц", "hooks": [],
        "pairs_well_with": [], "conflicts_with": [],
        "tags": {"worlds": ["Фэнтези"], "genres": ["приключение"],
                 "age_min": 3, "age_max": 99,
                 "story_depth": [story.DEPTH_FULL],
                 "interaction": ["кооперативное"], "tone": "любой",
                 "implies_player_conflict": True, "scary": False},
    }]}

    kept, dropped = story.filter_library(params(interaction="кооперативное"), trap)
    assert kept == []
    assert any("противостояни" in d["reason"] for d in dropped)


def test_причина_отсева_названа_у_каждой_отброшенной():
    _, dropped = story.filter_library(params(world=["Бизнес"]))
    assert dropped
    assert all(d["reason"] for d in dropped)


def test_абстрактной_игре_завязки_не_ищутся_вовсе():
    """Иначе режим достройки попросит придумать сюжет, от которого отказались."""
    kept, dropped = story.filter_library(params(story=story.DEPTH_NONE, world=[]))
    assert kept == [] and dropped == []
    assert story.choose_mode(kept, dropped, story.DEPTH_NONE) == story.MODE_STRICT


def test_нехватка_завязок_включает_достройку():
    kept, dropped = story.filter_library(
        params(world=["Ужасы/хоррор"], genre=["викторина"]))
    assert len(kept) < story.MIN_FOR_STRICT
    assert story.choose_mode(kept, dropped, story.DEPTH_FULL) == story.MODE_INVENT


# --------------------------------------------------------------------------
# Годный ответ
# --------------------------------------------------------------------------

def test_нормальный_ответ_принимается():
    problems, warnings = check(answer(*three()))
    assert problems == [], problems
    assert warnings == []


def test_модель_призналась_в_невыполненном_это_предупреждение():
    data = answer(*three())
    data["self_check"]["no_new_mechanics"] = False
    problems, warnings = check(data)
    assert problems == []
    assert warnings and "no_new_mechanics" in warnings[0]


def test_отказ_модели_это_нарушение():
    problems = problems_of(answer(*three(), error="нет подходящих завязок"))
    assert problems and "отказалась" in problems[0]


# --------------------------------------------------------------------------
# Глубина сюжета — главное различие этого агента
# --------------------------------------------------------------------------

def test_полноценный_сюжет_без_персонажей_не_принимается():
    problems = problems_of(answer(*three(characters=[])))
    assert any("персонажей нет" in p for p in problems)


def test_полноценный_сюжет_обязан_иметь_ставку_и_развязку():
    problems = problems_of(answer(*three(stakes="", ending=None)))
    assert any("ставка не описана" in p for p in problems)
    assert any("развязка не описана" in p for p in problems)


def test_короткое_описание_не_проходит_за_полноценный_сюжет():
    problems = problems_of(answer(*three(synopsis="Герои ищут ключи.")))
    assert any("описание короткое" in p for p in problems)


def test_антураж_не_разворачивают_в_историю():
    """Пользователь просил обёртку — история здесь не бонус, а не то, что просили."""
    long_text = SYNOPSIS * 4
    problems = problems_of(answer(*three(synopsis=long_text)),
                           p=params(story=story.DEPTH_FLAVOR))
    assert any("развёрнуто в историю" in p for p in problems)


def test_антураж_с_короткой_обёрткой_принимается():
    p = params(story=story.DEPTH_FLAVOR)
    problems = problems_of(
        answer(*three(synopsis="Действие происходит в старой библиотеке.",
                      characters=[], stakes=None, ending=None)), p=p)
    assert problems == [], problems


def test_абстрактной_игре_сюжет_не_дописывают():
    p = params(story=story.DEPTH_NONE, world=[])
    problems = problems_of(answer(*three()), p=p, seeds=[])
    assert any("игра абстрактная" in p_ and "заполнено" in p_ for p_ in problems)
    assert any("персонажи описаны" in p_ for p_ in problems)


def test_абстрактная_игра_принимается_с_одним_названием():
    p = params(story=story.DEPTH_NONE, world=[])
    empty = [variant(i + 1, seed_id=None, synopsis=None, setting=None,
                     stakes=None, ending=None, characters=[],
                     title="Поиск ключей %d" % (i + 1))
             for i in range(3)]
    problems = problems_of(answer(*empty), p=p, seeds=[])
    assert problems == [], problems


def test_абстрактной_игре_завязка_не_положена():
    p = params(story=story.DEPTH_NONE, world=[])
    empty = [variant(i + 1, synopsis=None, setting=None, stakes=None,
                     ending=None, characters=[]) for i in range(3)]
    problems = problems_of(answer(*empty), p=p, seeds=[])
    assert any("указана сюжетная завязка" in p_ for p_ in problems)


# --------------------------------------------------------------------------
# Артефакты: имя каждому типу компонентов из механик
# --------------------------------------------------------------------------

def test_безымянный_компонент_не_проходит():
    """Следующие этапы имён не придумывают — он останется безымянным навсегда."""
    short = [{"component": "карты", "name": "Залы", "role": "места"}]
    problems = problems_of(answer(*three(artifacts=short)))
    assert any("без названия остались" in p and "жетоны" in p for p in problems)


def test_лишний_компонент_не_проходит():
    """Компонент, которого нет в механиках, на этапе 5 не из чего считать."""
    extra = variant(1)["artifacts"] + [
        {"component": "игровое поле", "name": "Карта мира", "role": "путь"}]
    problems = problems_of(answer(*three(artifacts=extra)))
    assert any("нет в принятых механиках" in p and "игровое поле" in p
               for p in problems)


def test_дважды_названный_компонент_не_проходит():
    twice = variant(1)["artifacts"] + [
        {"component": "карты", "name": "Ещё карты", "role": "тоже места"}]
    problems = problems_of(answer(*three(artifacts=twice)))
    assert any("назван дважды" in p for p in problems)


def test_артефакт_без_имени_замечен():
    nameless = [dict(a, name="") if a["component"] == "жетоны" else a
                for a in variant(1)["artifacts"]]
    problems = problems_of(answer(*three(artifacts=nameless)))
    assert any("нет названия" in p for p in problems)


# --------------------------------------------------------------------------
# Сюжет не переписывает механики
# --------------------------------------------------------------------------

def test_количество_компонентов_не_называется():
    """По ТЗ количества считает программа на этапе 5 — названное здесь разойдётся."""
    problems = problems_of(answer(*three(
        logline="Соберите 3 ключа, пока горит свеча.")))
    assert any("количество компонентов" in p for p in problems)


def test_количество_словами_тоже_ловится():
    problems = problems_of(answer(*three(
        logline="В игре три жетона, и все нужно найти.")))
    assert any("количество компонентов" in p for p in problems)


def test_сюжет_не_пишет_правила():
    problems = problems_of(answer(*three(
        synopsis=SYNOPSIS + " В свой ход игрок открывает зал.")))
    assert any("описывает правила" in p for p in problems)


def test_идентификатор_завязки_не_считается_текстом():
    """Латинские id не должны попадать под проверки текста."""
    problems = problems_of(answer(*three()))
    assert problems == []


# --------------------------------------------------------------------------
# Возраст
# --------------------------------------------------------------------------

def test_пугающее_слово_не_проходит_для_младших():
    problems = problems_of(answer(*three(
        stakes="Если свеча догорит, хранители погибнут в темноте.")))
    assert any("рассчитана с 6 лет" in p for p in problems)


def test_для_подростков_та_же_формулировка_допустима():
    p = params(age_group={"min": 12, "max": 18}, world=["Детектив"],
               genre=["детектив"], interaction="конкурентное")
    data = answer(*[variant(i + 1, seed_id="STORY_HIDDEN_CULPRIT",
                            title="Тень над городом %d" % (i + 1),
                            stakes="Виновный уйдёт, и расследование погибнет.")
                    for i in range(3)])
    # завязки все три одинаковые — проверяем только отсутствие возрастного
    # замечания, остальное ловят свои тесты
    problems = problems_of(data, p=p)
    assert not any("рассчитана с" in p_ for p_ in problems)


# --------------------------------------------------------------------------
# Происхождение завязки и структура ответа
# --------------------------------------------------------------------------

def test_выдуманная_завязка_без_описания_не_принимается():
    problems = problems_of(answer(*three(seed_id="STORY_MADE_UP")))
    assert any("нет ни в библиотеке" in p for p in problems)


def test_выдуманная_завязка_с_описанием_принимается_в_режиме_достройки():
    seed = {"id": "STORY_NEW_HEIST", "name": "Ограбление", "premise": "п",
            "conflict": "к", "goal_shape": "ц"}
    data = answer(*three(seed_id="STORY_NEW_HEIST"), invented_seeds=[seed])
    problems = problems_of(data, mode=story.MODE_INVENT)
    assert not any("нет ни в библиотеке" in p for p in problems)
    assert any("построены на одной завязке" in p for p in problems)


def test_придумывать_завязки_вне_режима_достройки_нельзя():
    seed = {"id": "STORY_NEW_HEIST", "name": "Ограбление", "premise": "п",
            "conflict": "к", "goal_shape": "ц"}
    problems = problems_of(answer(*three(), invented_seeds=[seed]))
    assert any("режим достройки" in p for p in problems)


def test_три_варианта_на_одной_завязке_это_один_вариант():
    problems = problems_of(answer(variant(1), variant(2), variant(3)))
    assert any("построены на одной завязке" in p for p in problems)


def test_вариантов_должно_быть_три():
    problems = problems_of(answer(variant(1), variant(2)))
    assert any("ровно три" in p for p in problems)


def test_рекомендованный_вариант_обязан_существовать():
    problems = problems_of(answer(*three(), recommended_variant_id=9))
    assert any("recommended_variant_id" in p for p in problems)


def test_длинное_название_не_принимается():
    long_title = "Очень длинное название игры про поиски древних ключей"
    problems = problems_of(answer(*three(title=long_title)))
    assert any("название из" in p for p in problems)


def test_без_названия_не_принимается():
    problems = problems_of(answer(*three(title="")))
    assert any("нет названия игры" in p for p in problems)


# --------------------------------------------------------------------------
# Вход агента
# --------------------------------------------------------------------------

def test_механики_без_компонентов_не_дают_запустить_агента():
    """Без списка компонентов артефакты не с чем сверять — полсмысла агента."""
    with pytest.raises(story.NotEnoughSeeds) as error:
        story.generate(params(), {"title": "механики без компонентов"})
    assert "required_component_types" in str(error.value)


def test_в_промпт_уходит_принятый_модуль_и_список_компонентов():
    seeds = story.filter_library(params())[0]
    message = story.build_user_message(params(), MECHANICS, story.for_prompt(seeds))
    assert "ПРИНЯТЫЙ МОДУЛЬ МЕХАНИК" in message
    assert "собрать три предмета" in message
    for component in MECHANICS["required_component_types"]:
        assert component in message


def test_абстрактной_игре_библиотека_в_промпт_не_уходит():
    p = params(story=story.DEPTH_NONE, world=[])
    message = story.build_user_message(p, MECHANICS, [])
    assert "БИБЛИОТЕКА СЮЖЕТНЫХ ЗАВЯЗОК" not in message
    assert "АБСТРАКТНУЮ" in message
