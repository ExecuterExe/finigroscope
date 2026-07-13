"""Предзаполнение конструктора, разрешение состояния формы и валидация.

Канонический вид конфигурации игры (он же — формат и предзаполнения, и сохранения,
и сериализации с фронтенда):

    {
      "players": {"min": 2, "max": 4},
      "targets": {"game_minutes": [20, 40], "tie_rate_max": 10, ...},
      "blocks": {
        "track": {"length": 40, "dice": "d6", ...},
        "decks": {"items": [{"name": "вопросы", "size": 50, ...}], "effect_low": -5, ...}
      }
    }

`build_prefill` строит такой словарь из извлечённых на этапе 1 значений.
`build_form_state` сливает (defaults ← prefill ← saved) в структуру, удобную для
отрисовки шаблоном. `validate` чистит и типизирует то, что пришло с формы.
"""

from simulation.schema import (
    BLOCKS,
    BLOCK_BY_KEY,
    PLAYERS_FIELD,
    TARGET_FIELDS,
    WIN_FIELD,
    WIN_THRESHOLD_FIELD,
)


# --- утилиты приведения типов ----------------------------------------------
def _to_int(value, default=0):
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "on", "yes", "да")


def _clamp(value, lo, hi):
    if lo is not None and value < lo:
        return lo
    if hi is not None and value > hi:
        return hi
    return value


# --- предзаполнение из этапа 1 ----------------------------------------------
def build_prefill(game: dict) -> dict:
    """Строит черновик конфигурации из извлечённых параметров одной игры."""
    values = (game.get("params") or {}).get("values", {})
    cfg = {"players": {}, "win": {}, "targets": {}, "blocks": {}}

    players = values.get("players")
    if players:
        cfg["players"] = {"min": players["min"], "max": players["max"]}

    # длительность в минутах больше не используется (симуляция меряет ходы)

    decks = values.get("decks")
    if decks and decks.get("sizes"):
        items = []
        for i, size in enumerate(decks["sizes"], start=1):
            items.append({
                "name": f"колода {i}", "size": size,
                "hand_size": 0, "draw_per_round": 1, "refill": True,
            })
        cfg["blocks"]["decks"] = {"items": items}
        rewards = values.get("rewards")
        if rewards:
            amounts = [_to_int(r["amount"]) for r in rewards]
            cfg["blocks"]["decks"]["effect_low"] = min(amounts)
            cfg["blocks"]["decks"]["effect_high"] = max(amounts)

    dice = values.get("dice")
    if dice and dice.get("faces"):
        cfg["blocks"].setdefault("track", {})["dice"] = dice["faces"][0]

    # инференс условия победы по наличию блоков/значений
    if "track" in cfg["blocks"]:
        cfg["win"] = {"type": "first_to_position"}
    elif decks and decks.get("sizes"):
        cfg["win"] = {"type": "max_score_at_end"}

    return cfg


# --- разрешение состояния формы --------------------------------------------
def _resolve_scalar(field, source: dict, show_auto: bool):
    key = field["key"]
    has = key in source
    value = source.get(key, field.get("default"))
    return {"value": value, "auto": show_auto and has}


def _resolve_range(field, source: dict, show_auto: bool):
    key = field["key"]
    has = key in source
    raw = source.get(key)
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        mn, mx = raw[0], raw[1]
    else:
        mn, mx = field.get("min_default"), field.get("max_default")
    return {"min_value": mn, "max_value": mx, "auto": show_auto and has}


def _resolve_grid(field, source: dict, show_auto: bool):
    key = field["key"]
    has = key in source
    raw = source.get(key)
    if isinstance(raw, dict) and raw.get("mode") == "known":
        return {"mode": "known", "known_value": raw.get("value", field["min"]),
                "scan_values": field.get("default_values", []), "auto": show_auto and has}
    scan = field.get("default_values", [])
    if isinstance(raw, dict) and raw.get("mode") == "scan":
        scan = raw.get("values", scan)
    return {"mode": "scan", "known_value": scan[0] if scan else field.get("min", 0),
            "scan_values": scan, "auto": show_auto and has}


def _empty_row(columns):
    return {c["key"]: c.get("default", "" if c["type"] == "text" else (False if c["type"] == "bool" else 0))
            for c in columns}


def _resolve_rows(field, source: dict, show_auto: bool):
    key = field["key"]
    has = key in source
    rows = source.get(key)
    if not isinstance(rows, list) or not rows:
        rows = [_empty_row(field["columns"])]
    # дополним недостающие колонки дефолтами
    clean = []
    for row in rows:
        base = _empty_row(field["columns"])
        if isinstance(row, dict):
            base.update({c["key"]: row.get(c["key"], base[c["key"]]) for c in field["columns"]})
        clean.append(base)
    return {"rows": clean, "auto": show_auto and has}


def _resolve_field(field, source: dict, show_auto: bool):
    t = field["type"]
    state = dict(field)
    if t == "range":
        state.update(_resolve_range(field, source, show_auto))
    elif t == "grid":
        state.update(_resolve_grid(field, source, show_auto))
    elif t == "rows":
        state.update(_resolve_rows(field, source, show_auto))
    else:  # int / float / select / bool / text
        state.update(_resolve_scalar(field, source, show_auto))
    return state


def build_form_state(prefill: dict = None, saved: dict = None) -> dict:
    """Сливает defaults ← prefill ← saved в структуру для шаблона.

    Если есть `saved` — показываем его (пользователь уже подтвердил), без бейджей
    «из текста». Иначе показываем `prefill` с бейджами на автозаполненных полях.
    """
    source = saved or prefill or {}
    show_auto = saved is None and bool(prefill)

    blocks_src = source.get("blocks", {})
    players_src = source.get("players", {})
    targets_src = source.get("targets", {})
    win_src = source.get("win", {})

    players_state = _resolve_range(
        PLAYERS_FIELD, {PLAYERS_FIELD["key"]: [players_src.get("min"), players_src.get("max")]}
        if players_src else {}, show_auto)
    players_state.update({"label": PLAYERS_FIELD["label"], "help": PLAYERS_FIELD.get("help"),
                          "min": PLAYERS_FIELD["min"], "max": PLAYERS_FIELD["max"]})

    # условие победы: type (select) + threshold (int)
    win_flat = {WIN_FIELD["key"]: win_src.get("type"),
                WIN_THRESHOLD_FIELD["key"]: win_src.get("threshold")}
    win_state = {
        "type": _resolve_field(WIN_FIELD, win_flat, show_auto),
        "threshold": _resolve_field(WIN_THRESHOLD_FIELD, win_flat, show_auto),
    }

    targets_state = [_resolve_field(f, targets_src, show_auto) for f in TARGET_FIELDS]

    blocks_state = []
    for block in BLOCKS:
        enabled = block["key"] in blocks_src
        src = blocks_src.get(block["key"], {})
        fields = [_resolve_field(f, src, show_auto) for f in block["fields"]]
        blocks_state.append({
            "key": block["key"], "title": block["title"], "icon": block["icon"],
            "desc": block["desc"], "enabled": enabled, "fields": fields,
        })

    return {"players": players_state, "win": win_state,
            "targets": targets_state, "blocks": blocks_state}


# --- валидация присланной формы --------------------------------------------
def _validate_scalar(field, raw):
    t = field["type"]
    if t == "int":
        return _clamp(_to_int(raw, field.get("default", 0)), field.get("min"), field.get("max"))
    if t == "float":
        return _clamp(_to_float(raw, field.get("default", 0.0)), field.get("min"), field.get("max"))
    if t == "bool":
        return _to_bool(raw)
    if t == "select":
        allowed = [o if isinstance(o, str) else o["value"] for o in field.get("options", [])]
        return raw if raw in allowed else field.get("default", allowed[0] if allowed else None)
    return str(raw)[:500] if raw is not None else ""


def _validate_field(field, raw_block: dict):
    key = field["key"]
    t = field["type"]
    if key not in raw_block:
        return None
    raw = raw_block[key]

    if t == "range":
        if isinstance(raw, (list, tuple)) and len(raw) == 2:
            lo = _clamp(_to_int(raw[0], field.get("min_default", 0)), field.get("min"), field.get("max"))
            hi = _clamp(_to_int(raw[1], field.get("max_default", 0)), field.get("min"), field.get("max"))
            return [min(lo, hi), max(lo, hi)]
        return None
    if t == "grid":
        if isinstance(raw, dict) and raw.get("mode") == "known":
            return {"mode": "known",
                    "value": _clamp(_to_float(raw.get("value"), field.get("min", 0)),
                                    field.get("min"), field.get("max"))}
        vals = raw.get("values", []) if isinstance(raw, dict) else []
        vals = [_clamp(_to_float(v, 0), field.get("min"), field.get("max")) for v in vals]
        return {"mode": "scan", "values": vals or field.get("default_values", [])}
    if t == "rows":
        rows = raw if isinstance(raw, list) else []
        clean = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            clean.append({c["key"]: _validate_scalar(c, row.get(c["key"])) for c in field["columns"]})
        return clean
    return _validate_scalar(field, raw)


def validate(payload: dict) -> dict:
    """Чистит и типизирует конфигурацию, пришедшую с формы. Возвращает канон."""
    payload = payload or {}
    cfg = {"players": {}, "win": {}, "targets": {}, "blocks": {}}

    p = payload.get("players") or {}
    pmin = _clamp(_to_int(p.get("min"), PLAYERS_FIELD["min_default"]), PLAYERS_FIELD["min"], PLAYERS_FIELD["max"])
    pmax = _clamp(_to_int(p.get("max"), PLAYERS_FIELD["max_default"]), PLAYERS_FIELD["min"], PLAYERS_FIELD["max"])
    cfg["players"] = {"min": min(pmin, pmax), "max": max(pmin, pmax)}

    w_src = payload.get("win") or {}
    cfg["win"] = {
        "type": _validate_scalar(WIN_FIELD, w_src.get("type")),
        "threshold": _validate_scalar(WIN_THRESHOLD_FIELD, w_src.get("threshold")),
    }

    t_src = payload.get("targets") or {}
    for field in TARGET_FIELDS:
        val = _validate_field(field, t_src)
        if val is not None:
            cfg["targets"][field["key"]] = val

    b_src = payload.get("blocks") or {}
    enabled_keys = b_src if isinstance(b_src, dict) else {}
    for key, raw_block in enabled_keys.items():
        block = BLOCK_BY_KEY.get(key)
        if not block or not isinstance(raw_block, dict):
            continue
        clean_block = {}
        for field in block["fields"]:
            val = _validate_field(field, raw_block)
            if val is not None:
                clean_block[field["key"]] = val
        cfg["blocks"][key] = clean_block

    return cfg


# --- пресеты-шаблоны (рабочие примеры для понятности) -----------------------
PRESETS = {
    "race_quiz": {
        "title": "🏁 Гонка-викторина",
        "desc": "Трек + колода вопросов; движение при верном ответе.",
        "config": {
            "players": {"min": 2, "max": 4},
            "win": {"type": "first_to_position", "threshold": 10},
            "targets": {"length_rounds": [15, 30], "tie_rate_max": 10, "first_player_tolerance": 3},
            "blocks": {
                "track": {"length": 40, "dice": "d6", "move_condition": "correct_answer",
                          "p_correct": {"mode": "scan", "values": [0.4, 0.7, 0.9]}, "special_cells": []},
                "decks": {"items": [{"name": "вопросы", "size": 50, "hand_size": 0,
                                      "draw_per_round": 1, "refill": True}],
                          "effect_low": 0, "effect_high": 0},
            },
        },
    },
    "judge_cards": {
        "title": "⚖️ Судейская карточная",
        "desc": "Раунды + судья выбирает победителя раунда + колода кейсов.",
        "config": {
            "players": {"min": 4, "max": 6},
            "win": {"type": "max_score_at_end", "threshold": 10},
            "targets": {"length_rounds": [6, 12], "tie_rate_max": 10, "first_player_tolerance": 3},
            "blocks": {
                "rounds": {"formula": "players_times_laps", "laps": 1, "role_rotation": "clockwise"},
                "judge_vote": {"scoring_type": "judge_pick", "points_per_round": 1,
                               "judge_model": "uniform_random"},
                "decks": {"items": [{"name": "кейсы", "size": 150, "hand_size": 6,
                                      "draw_per_round": 1, "refill": False}],
                          "effect_low": 0, "effect_high": 0},
            },
        },
    },
    "economy": {
        "title": "💰 Экономический симулятор",
        "desc": "Ресурсы + действия игрока + колода событий; кто богаче в конце.",
        "config": {
            "players": {"min": 3, "max": 4},
            "win": {"type": "max_resource_at_end", "threshold": 100},
            "targets": {"length_rounds": [15, 25], "tie_rate_max": 10, "first_player_tolerance": 3},
            "blocks": {
                "resources": {"items": [{"name": "капитал", "start": 20, "per_turn": -1, "bankrupt_at": 0}],
                              "on_bankrupt": "eliminated"},
                "actions": {"items": [
                    {"name": "работать", "score_delta": 0, "resource_delta": 5, "position_delta": 0, "cost": 0, "risk": 1},
                    {"name": "инвестировать", "score_delta": 0, "resource_delta": 0, "position_delta": 0, "cost": 5, "risk": 8},
                ], "policy": "random"},
                "decks": {"items": [{"name": "события", "size": 60, "hand_size": 0,
                                      "draw_per_round": 1, "refill": True}],
                          "effect_low": -5, "effect_high": 6},
                "rounds": {"formula": "fixed", "fixed_rounds": 20, "role_rotation": "none"},
            },
        },
    },
}


def get_preset(key: str):
    """Канонический конфиг пресета по ключу или None."""
    preset = PRESETS.get(key)
    return preset["config"] if preset else None


def preset_list():
    """Список пресетов для меню: [{key, title, desc}]."""
    return [{"key": k, "title": v["title"], "desc": v["desc"]} for k, v in PRESETS.items()]
