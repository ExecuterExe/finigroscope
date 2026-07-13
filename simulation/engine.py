"""Универсальный движок партии: исполняет конфиг-блоки как данные.

Никакой генерации/исполнения произвольного кода (Принцип безопасности) — один
движок интерпретирует канонический конфиг конструктора {players, win, targets,
blocks} и прогоняет партию. Блоки комбинируются (трек + колоды + ресурсы + судья
+ таймер + действия), а УСЛОВИЕ ПОБЕДЫ задаётся явно (config["win"]) — это делает
движок применимым почти к любой игре, а не к фиксированным архетипам.

Блок «Действия игрока» задаёт пространство решений: каждый ход игрок выбирает
действие из списка, и его эффекты меняют состояние. Так автор выражает «что
вообще можно делать», а движок остаётся общим.

Длительность меряется В ХОДАХ И ДЕЙСТВИЯХ (минуты в симуляции неизвестны).
Seat 0 ходит первым каждый раунд — так детектируется преимущество очерёдности.
"""

import random

MAX_ROUNDS_SAFETY = 1000  # жёсткий предохранитель от бесконечной партии


def _f(block, key, default):
    v = block.get(key, default)
    return v if v is not None else default


def _dice_faces(track):
    d = str(track.get("dice", "d6"))
    try:
        return int(d.lstrip("dD"))
    except (ValueError, AttributeError):
        return 6


def _max_rounds(config, n):
    rounds = config["blocks"].get("rounds")
    if not rounds:
        return 80
    formula = rounds.get("formula", "players_times_laps")
    if formula == "fixed":
        return max(1, int(_f(rounds, "fixed_rounds", 10)))
    if formula == "players_times_laps":
        return max(1, n * int(_f(rounds, "laps", 1)))
    return MAX_ROUNDS_SAFETY


def _win_rule(config):
    """Возвращает (тип_победы, порог). 'auto' выводится по включённым блокам."""
    w = config.get("win") or {}
    t = w.get("type", "auto")
    if t == "auto":
        b = config.get("blocks", {})
        if "track" in b:
            t = "first_to_position"
        elif "judge_vote" in b:
            t = "max_score_at_end"
        elif "resources" in b or "characters" in b:
            t = "max_resource_at_end"
        else:
            t = "max_score_at_end"
    return t, int(_f(w, "threshold", 10))


# --- состояние --------------------------------------------------------------
def _setup(config, n, rng):
    blocks = config["blocks"]
    st = {
        "n": n, "active": list(range(n)),
        "pos": {s: 0 for s in range(n)},
        "score": {s: 0 for s in range(n)},
        "res": {s: {} for s in range(n)},
        "skip": {s: 0 for s in range(n)},
        "judge": 0, "threat": None, "decks": [], "deck_exhausted": False,
        "actions": {}, "eliminated": [], "speed_bonus": {},
    }
    resources = blocks.get("resources")
    if resources:
        for s in range(n):
            for item in resources.get("items", []):
                st["res"][s][item.get("name", "ресурс")] = int(_f(item, "start", 10))

    chars = blocks.get("characters")
    if chars and chars.get("items"):
        items = chars["items"]
        for s in range(n):
            ch = items[s % len(items)]
            st["res"][s]["капитал"] = st["res"][s].get("капитал", 0) + int(_f(ch, "start_resource", 10))
            st["speed_bonus"][s] = int(_f(ch, "start_speed", 0))

    if blocks.get("decks"):
        for d in blocks["decks"].get("items", []):
            size = int(_f(d, "size", 50))
            st["decks"].append({"remaining": size, "size": size,
                                "draw": int(_f(d, "draw_per_round", 1)),
                                "refill": bool(_f(d, "refill", True))})

    timer = blocks.get("timer")
    if timer:
        st["threat"] = int(_f(timer, "threat_start", 10))
    return st


def _bump(st, name):
    st["actions"][name] = st["actions"].get(name, 0) + 1


def _eliminate(st, seat):
    if seat in st["active"]:
        st["active"].remove(seat)
        st["eliminated"].append(seat)


def _res_total(st, seat):
    return sum(st["res"].get(seat, {}).values())


# --- обобщённые действия игрока --------------------------------------------
def _choose_action(block, st, seat, rng):
    items = [a for a in block.get("items", []) if a.get("name")]
    if not items:
        return None
    res_total = _res_total(st, seat)
    affordable = [a for a in items if int(_f(a, "cost", 0)) <= res_total] or items
    policy = block.get("policy", "random")
    if policy == "greedy_score":
        return max(affordable, key=lambda a: int(_f(a, "score_delta", 0)))
    if policy == "greedy_resource":
        return max(affordable, key=lambda a: int(_f(a, "resource_delta", 0)) - int(_f(a, "cost", 0)))
    return rng.choice(affordable)


def _apply_action(st, seat, a, rng):
    risk = int(_f(a, "risk", 0))
    jitter = rng.randint(-risk, risk) if risk > 0 else 0
    st["score"][seat] += int(_f(a, "score_delta", 0))
    delta = int(_f(a, "resource_delta", 0)) + jitter - int(_f(a, "cost", 0))
    if st["res"][seat]:
        st["res"][seat][next(iter(st["res"][seat]))] += delta
    else:
        st["score"][seat] += delta
    st["pos"][seat] += int(_f(a, "position_delta", 0))
    _bump(st, a.get("name") or "действие")


# --- ход игрока -------------------------------------------------------------
def _take_turn(config, st, seat, rng, p_correct):
    blocks = config["blocks"]
    if st["skip"].get(seat, 0) > 0:
        st["skip"][seat] -= 1
        _bump(st, "пропуск")
        return

    res_block = blocks.get("resources")
    if res_block:
        for item in res_block.get("items", []):
            name = item.get("name", "ресурс")
            st["res"][seat][name] = st["res"][seat].get(name, 0) + int(_f(item, "per_turn", 0))

    decks_block = blocks.get("decks")
    if decks_block and st["decks"]:
        low, high = int(_f(decks_block, "effect_low", -5)), int(_f(decks_block, "effect_high", 5))
        for deck in st["decks"]:
            if deck["remaining"] <= 0:
                if deck["refill"]:
                    deck["remaining"] = deck["size"]
                else:
                    st["deck_exhausted"] = True
                    continue
            deck["remaining"] -= deck["draw"]
            _bump(st, "карта")
            if low or high:
                eff = rng.randint(min(low, high), max(low, high))
                if st["res"][seat]:
                    st["res"][seat][next(iter(st["res"][seat]))] += eff
                else:
                    st["score"][seat] += eff

    actions_block = blocks.get("actions")
    if actions_block:
        a = _choose_action(actions_block, st, seat, rng)
        if a:
            _apply_action(st, seat, a, rng)

    track = blocks.get("track")
    if track:
        moved = True
        if track.get("move_condition") == "correct_answer":
            p = p_correct if p_correct is not None else 0.7
            moved = rng.random() < p
        if moved:
            roll = rng.randint(1, _dice_faces(track)) + st["speed_bonus"].get(seat, 0)
            st["pos"][seat] += max(0, roll)
            _bump(st, "ход")
            _apply_special_cell(track, st, seat)
        else:
            _bump(st, "неверный ответ")

    if res_block:
        for item in res_block.get("items", []):
            name = item.get("name", "ресурс")
            bankrupt_at = int(_f(item, "bankrupt_at", 0))
            if st["res"][seat].get(name, 0) <= bankrupt_at and int(_f(item, "start", 10)) > bankrupt_at:
                if res_block.get("on_bankrupt", "eliminated") == "eliminated":
                    _eliminate(st, seat)
                break


def _apply_special_cell(track, st, seat):
    cells = {int(c.get("cell", -1)): c for c in track.get("special_cells", [])}
    cell = cells.get(st["pos"][seat])
    if not cell:
        return
    effect, val = cell.get("effect", "none"), int(cell.get("value", 1) or 1)
    if effect == "forward":
        st["pos"][seat] += val
    elif effect == "back":
        st["pos"][seat] = max(0, st["pos"][seat] - val)
    elif effect == "skip":
        st["skip"][seat] = st["skip"].get(seat, 0) + 1
    elif effect == "to_start":
        st["pos"][seat] = 0
    elif effect == "gain" and st["res"][seat]:
        st["res"][seat][next(iter(st["res"][seat]))] += val
    elif effect == "lose" and st["res"][seat]:
        st["res"][seat][next(iter(st["res"][seat]))] -= val


def _round_end(config, st, rng):
    blocks = config["blocks"]
    judge = blocks.get("judge_vote")
    if judge and st["active"]:
        st["judge"] = (st["judge"] + 1) % st["n"]
        candidates = [s for s in st["active"] if s != st["judge"]] or list(st["active"])
        pts = int(_f(judge, "points_per_round", 1))
        if judge.get("scoring_type") == "dice_roll":
            winner = rng.choice(candidates)
        elif judge.get("judge_model") == "anti_leader":
            weights = [1.0 / (1 + st["score"][s]) for s in candidates]
            winner = rng.choices(candidates, weights=weights, k=1)[0]
        else:
            winner = rng.choice(candidates)
        st["score"][winner] += pts
        _bump(st, "очко")

    timer = blocks.get("timer")
    if timer and st["threat"] is not None:
        st["threat"] -= int(_f(timer, "threat_speed", 1))
        if st["threat"] <= 0:
            return _timer_catch(config, st)
    return None


def _timer_catch(config, st):
    on_catch = config["blocks"]["timer"].get("on_catch", "game_over")
    if on_catch == "game_over":
        return "timer"
    if on_catch == "eliminate" and st["active"]:
        _eliminate(st, min(st["active"], key=lambda s: st["pos"].get(s, 0)))
    elif on_catch == "lose":
        for s in st["active"]:
            if st["res"][s]:
                st["res"][s][next(iter(st["res"][s]))] -= 1
    st["threat"] = int(_f(config["blocks"]["timer"], "threat_start", 10))
    return None


# --- определение исхода -----------------------------------------------------
def _metric_for(win_type):
    if win_type in ("first_to_position",):
        return "pos"
    if win_type in ("max_resource_at_end", "first_to_resource", "last_standing"):
        return "res"
    return "score"


def _seat_value(st, metric, seat):
    if metric == "pos":
        return st["pos"].get(seat, 0)
    if metric == "res":
        return _res_total(st, seat)
    return st["score"].get(seat, 0)


def _finalize(config, st, reason, rounds, turns, winner_seat, win_type):
    metric = _metric_for(win_type)
    pool = st["active"] if st["active"] else list(range(st["n"]))

    if reason == "win" and winner_seat is not None:
        winner, tie = winner_seat, False
    elif not st["active"] and win_type == "last_standing":
        winner, tie, reason = None, False, "all_eliminated"
    else:
        vals = {s: _seat_value(st, metric, s) for s in pool}
        top = max(vals.values()) if vals else 0
        winners = [s for s, v in vals.items() if v == top]
        tie = len(winners) > 1
        if reason == "round_cap" and win_type == "first_to_position":
            winner = None  # гонка не финишировала в лимит -> deadlock
        else:
            winner = winners[0] if winners else None

    return {
        "winner": winner, "tie": tie, "reason": reason,
        "rounds": rounds, "turns": turns,
        "total_actions": sum(st["actions"].values()),
        "final_pos": dict(st["pos"]), "final_score": dict(st["score"]),
        "final_res": {s: _res_total(st, s) for s in range(st["n"])},
        "eliminated": list(st["eliminated"]),
        "deck_exhausted": st["deck_exhausted"],
        "actions": dict(st["actions"]),
        "metric_used": metric,
    }


# --- одна партия ------------------------------------------------------------
def simulate_game(config, n, rng, p_correct=None):
    st = _setup(config, n, rng)
    win_type, threshold = _win_rule(config)
    blocks = config["blocks"]
    track = blocks.get("track")
    track_len = int(_f(track, "length", 40)) if track else 10 ** 9
    max_rounds = min(_max_rounds(config, n), MAX_ROUNDS_SAFETY)

    def reached(seat):
        if win_type == "first_to_position":
            return st["pos"][seat] >= track_len
        if win_type == "first_to_score":
            return st["score"][seat] >= threshold
        if win_type == "first_to_resource":
            return _res_total(st, seat) >= threshold
        return False

    rounds, turns, winner_seat, reason, finished = 0, 0, None, "round_cap", False

    while rounds < max_rounds and st["active"]:
        rounds += 1
        for seat in [s for s in range(st["n"]) if s in st["active"]]:
            if seat not in st["active"]:
                continue
            turns += 1
            _take_turn(config, st, seat, rng, p_correct)
            if reached(seat):
                winner_seat, reason, finished = seat, "win", True
                break
        if finished:
            break

        if _round_end(config, st, rng) == "timer":
            reason = "timer"
            break

        if win_type == "first_to_score":
            hit = [s for s in st["active"] if st["score"][s] >= threshold]
            if hit:
                winner_seat, reason, finished = max(hit, key=lambda s: st["score"][s]), "win", True
                break

        if len(st["active"]) <= 1 and (blocks.get("resources") or blocks.get("timer")):
            reason = "last_standing"
            break
        if st["deck_exhausted"] and blocks.get("rounds", {}).get("formula") == "until_deck_empty":
            reason = "deck_empty"
            break

    if not finished and reason == "round_cap" and win_type in ("max_score_at_end", "max_resource_at_end"):
        reason = "max_end"

    return _finalize(config, st, reason, rounds, turns, winner_seat, win_type)
