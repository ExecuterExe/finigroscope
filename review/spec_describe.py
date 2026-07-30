"""Человекочитаемое описание game_spec — чтобы автор проверил структуру глазами.

Задача шага: показать автору, КАК сервис теперь понимает его игру, прежде чем
он примет структуру. Описание собирается ДЕТЕРМИНИРОВАННО из полей спеки, без
обращения к LLM: лишний вызов модели тут только добавил бы риск галлюцинаций
ровно там, где нужна точность — автор должен увидеть, что реально записано в
game_spec, а не литературный пересказ этого.

Пустые поля не замалчиваются: «не указано» — важный сигнал автору, что данных
не хватило, и он может это поправить.
"""

_NOT_SET = "не указано"

_MODE_RU = {
    "cooperative": "кооперативная (играют заодно)",
    "competitive": "соревновательная (каждый сам за себя)",
    "team": "командная",
    "solo": "одиночная",
}

_WIN_RU = {
    "collect": "собрать нужное количество",
    "max_score": "набрать больше всех очков",
    "most": "набрать больше всех",
    "race": "первым дойти до цели",
    "survive": "продержаться до конца",
    "eliminate": "устранить соперников",
}

# --- значения из diagnostic_meta (надстройка v3) ------------------------------
_ROLE_RU = {
    "win_metric": "копится ради победы",
    "spendable": "тратится на действия",
    "both": "и копится ради победы, и тратится на действия",
}

_RESOLUTION_RU = {
    "deterministic": "по правилу, однозначно",
    "probabilistic": "случайностью (кубик, карта, жребий)",
    "subjective_judgment": "суждением самих игроков",
}


def _fmt(value, empty=_NOT_SET):
    if value is None or value == "" or value == []:
        return empty
    if value is True:
        return "да"
    if value is False:
        return "нет"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


def describe(root: dict) -> list:
    """Строит описание как список блоков [{title, lines}] для отрисовки.

    `root` — полный ответ извлеченца (game_spec + gaps + ambiguities).
    """
    root = root or {}
    spec = root.get("game_spec") or {}
    core = spec.get("core") or {}
    text = spec.get("text") or {}
    meta = root.get("diagnostic_meta") or {}
    blocks = []

    # --- как играют ---------------------------------------------------------
    players = core.get("players") or {}
    pmin, pmax = players.get("min"), players.get("max")
    if pmin or pmax:
        players_line = f"Играют от {_fmt(pmin)} до {_fmt(pmax)} человек."
    else:
        players_line = f"Число игроков — {_NOT_SET}."

    turn = core.get("turn") or {}
    order = turn.get("order")
    actions = turn.get("actions") or []
    if order and actions:
        turn_line = (f"Ходят {_fmt(order)}; в свой ход игрок может: "
                     f"{', '.join(str(a) for a in actions)}.")
    elif actions:
        turn_line = f"В свой ход игрок может: {', '.join(str(a) for a in actions)}."
    elif order:
        turn_line = f"Ходят {_fmt(order)}; конкретные действия хода — {_NOT_SET}."
    else:
        turn_line = f"Порядок хода и действия игрока — {_NOT_SET}."

    mode = core.get("mode")
    mode_line = f"Тип игры: {_MODE_RU.get(mode, _fmt(mode))}."

    elim = core.get("elimination")
    if elim is True:
        elim_line = "Игроки могут выбывать досрочно."
    elif elim is False:
        elim_line = "Никто не выбывает досрочно — все играют до конца."
    else:
        elim_line = f"Выбывание игроков — {_NOT_SET}."

    play_lines = [players_line, mode_line, turn_line, elim_line]

    # «Только на четверых» — это НЕ то же, что диапазон из одного значения, и
    # проверяется отдельным тестом, поэтому говорим об этом автору прямо.
    strict = meta.get("strict_player_count") or {}
    if strict.get("strict"):
        play_lines.append(
            f"Заявлено строго фиксированное число игроков: {_fmt(strict.get('declared'))}.")

    play_time = core.get("play_time")
    if play_time:
        play_lines.append(f"Ожидаемая длительность партии: {_fmt(play_time)}.")

    blocks.append({"title": "Как играют", "lines": play_lines})

    # --- чем заканчивается ---------------------------------------------------
    win = core.get("win_condition") or {}
    wtype, metric, threshold = win.get("type"), win.get("metric"), win.get("threshold")
    if wtype or metric or threshold is not None:
        win_line = "Побеждает тот, кто должен " + _WIN_RU.get(wtype, _fmt(wtype)) + "."
        detail = []
        if metric:
            detail.append(f"считаем «{metric}»")
        detail.append(f"порог победы — {_fmt(threshold)}")
        win_line += " (" + "; ".join(detail) + ")"
    else:
        win_line = f"Условие победы — {_NOT_SET}."

    loss = (core.get("loss_condition") or {}).get("type")
    loss_line = (f"Поражение/конец партии: {_fmt(loss)}." if loss
                 else f"Условие поражения — {_NOT_SET}.")

    limits = core.get("limits") or {}
    max_rounds = limits.get("max_rounds")
    limit_line = (f"Партия ограничена {max_rounds} раундами." if max_rounds
                  else f"Лимит раундов — {_NOT_SET}.")

    end_lines = [win_line]

    # Несколько путей к победе живут в надстройке: контракт core.win_condition
    # закрыт и хранит только основной путь, поэтому без этой строки автор не
    # увидел бы, что вторую цель его игры сервис заметил.
    paths = meta.get("win_paths") or {}
    if paths.get("multiple"):
        listed = ", ".join(str(p) for p in (paths.get("paths") or []))
        end_lines.append(f"Путей к победе несколько{': ' + listed if listed else ''}.")
    elif paths.get("multiple") is False:
        end_lines.append("Путь к победе один.")

    tie = meta.get("tie_breaker") or {}
    if tie.get("applicable") is False:
        end_lines.append("Равенство результатов невозможно — тай-брейк не нужен.")
    elif tie.get("present"):
        end_lines.append(f"При равенстве: {_fmt(tie.get('description'))}.")
    elif tie.get("applicable") and tie.get("present") is False:
        end_lines.append("Равенство результатов возможно, но правила не говорят, "
                         "как его разрешать.")

    end_lines += [loss_line, limit_line]

    catch_up = core.get("catch_up") or {}
    if catch_up.get("enabled"):
        end_lines.append(f"Отстающим помогает механика догоняния: "
                         f"{_fmt(catch_up.get('mechanism'))}.")
    elif catch_up.get("enabled") is False:
        end_lines.append("Механики помощи отстающим нет.")

    blocks.append({"title": "Чем заканчивается", "lines": end_lines})

    # --- что считаем и где случайность ---------------------------------------
    roles = meta.get("resource_roles") or {}
    res_lines = []
    for r in core.get("resources") or []:
        name = r.get("name")
        scope = {"shared": "общий", "personal": "личный",
                 "per_player": "личный"}.get(r.get("scope"), _fmt(r.get("scope")))
        line = (f"«{_fmt(name)}» — {scope} ресурс, старт: {_fmt(r.get('start'))}, "
                f"цель: {_fmt(r.get('goal'))}.")
        # Роль ресурса — ключевое различие v3: метрика победы и то, чем платят,
        # это разные вещи, и их слияние портит все экономические проверки.
        role = roles.get(name)
        if role:
            line += f" Роль: {_ROLE_RU.get(role, _fmt(role))}."
        res_lines.append(line)
    if not res_lines:
        res_lines.append(f"Ресурсы — {_NOT_SET}.")

    rnd_lines = []
    for r in core.get("randomness") or []:
        rnd_lines.append(f"источник случайности: {_fmt(r.get('type'))}.")
    if not rnd_lines:
        rnd_lines.append(f"Источники случайности — {_NOT_SET}.")

    blocks.append({"title": "Что считаем", "lines": res_lines + rnd_lines})

    # --- как определяются исходы действий -------------------------------------
    # Отдельный блок, потому что именно здесь решается судьба числовой ветки:
    # действия «по суждению игроков» кодом не моделируются по существу.
    resolution = meta.get("actions_resolution") or {}
    if resolution:
        act_lines = [f"«{name}» — {_RESOLUTION_RU.get(kind, _fmt(kind))}."
                     for name, kind in resolution.items()]
        subjective = [n for n, k in resolution.items() if k == "subjective_judgment"]
        if subjective:
            act_lines.append(
                "Исход этих действий определяют сами игроки, а не правила: "
                + ", ".join(subjective)
                + ". Симулятор их посчитает лишь приближённо — по ним нужен живой плейтест.")
        blocks.append({"title": "Как определяются исходы действий", "lines": act_lines})

    # --- прочие факты для проверок --------------------------------------------
    extra_lines = []
    targeted = meta.get("targeted_actions") or {}
    if targeted.get("exists"):
        listed = ", ".join(str(a) for a in (targeted.get("actions") or []))
        extra_lines.append(f"Есть действия, направленные на конкретного игрока"
                           f"{': ' + listed if listed else ''}.")
    elif targeted.get("exists") is False:
        extra_lines.append("Действий, направленных на конкретного игрока, нет.")

    deal = meta.get("initial_deal") or {}
    if deal.get("identical_start"):
        extra_lines.append("Все начинают в одинаковых условиях, без случайности на старте.")
    elif deal.get("random"):
        extra_lines.append(f"На старте есть случайная раздача: {_fmt(deal.get('what'))}.")

    hand = meta.get("hand_exists")
    if hand is True:
        extra_lines.append("Игроки держат карты на руках по ходу партии.")
    elif hand is False:
        extra_lines.append("Карт на руках игроки не держат.")

    modes = meta.get("difficulty_modes") or {}
    if modes.get("exists"):
        listed = ", ".join(str(m) for m in (modes.get("modes") or []))
        extra_lines.append(f"Заявлены варианты правил{': ' + listed if listed else ''}.")
    elif modes.get("exists") is False:
        extra_lines.append("Вариантов правил (базовый/продвинутый) не заявлено.")

    if extra_lines:
        blocks.append({"title": "Что ещё учтено", "lines": extra_lines})

    # --- компоненты ----------------------------------------------------------
    comp_lines = []
    for c in text.get("components") or []:
        qty = c.get("qty")
        qty_part = f"{qty} шт." if qty else "количество не указано"
        fn = c.get("function")
        fn_part = f" — {fn}" if fn else " — назначение не указано"
        comp_lines.append(f"«{_fmt(c.get('name'))}» ({qty_part}){fn_part}")
    if comp_lines:
        blocks.append({"title": "Из чего состоит игра", "lines": comp_lines})

    return blocks


def summary_line(root: dict) -> str:
    """Одна строка-выжимка для шапки: «Игра на 3–6 человек, соревновательная»."""
    core = ((root or {}).get("game_spec") or {}).get("core") or {}
    players = core.get("players") or {}
    pmin, pmax = players.get("min"), players.get("max")
    parts = []
    if pmin or pmax:
        parts.append(f"на {_fmt(pmin)}–{_fmt(pmax)} игроков")
    mode = core.get("mode")
    if mode:
        parts.append(_MODE_RU.get(mode, str(mode)).split(" (")[0])
    return "Игра " + ", ".join(parts) if parts else "Структура игры"
