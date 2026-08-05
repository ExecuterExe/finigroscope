"""
==============================================================================
СКЕЛЕТ-СИМУЛЯТОР НАСТОЛЬНОЙ ИГРЫ v4 (проверка технического костяка)
==============================================================================
Прогоняет игровой ЦИКЛ много раз и собирает статистику:
  • базовую (винрейт по местам, достижимость победы, длительность, действия) —
    формат ПОЛНОСТЬЮ совместим с v3, его читает «Оценщик статистик»;
  • диагностическую (софтлоки, траектории, эффекты действий, петли, карты,
    пути победы, персоны) — её читает «Агент-диагност».

ПРАВИЛА ПРАВКИ:
  • Блок «ДВИЖОК» НЕ ТРОГАТЬ.
  • Блок «СКЕЛЕТ» — заполнить под игру, НЕ меняя имена/сигнатуры функций.
  • Непредсказуемое/субъективное моделируется СЛУЧАЙНОЙ величиной,
    каждое упрощение помечать  # ДОПУЩЕНИЕ: ...
==============================================================================
"""

import random
import statistics
import json
import math
from dataclasses import dataclass, field

# ============================================================================
# КОНФИГУРАЦИЯ (заменить под свою игру)
# ============================================================================
GAMES_PER_CONFIG = 2000       # партий на конфигурацию числа игроков
PLAYER_COUNTS = [2, 3, 4]     # min / типичное / max число игроков
MAX_ROUNDS = 200              # ПРЕДОХРАНИТЕЛЬ от бесконечной партии — НЕ удалять
# True, если скелет САМ управляет очередью хода (фазовые игры: судья, одновременное
# вскрытие, аукцион). Тогда движок не вызывает advance() и не трогает state.current,
# а скелет обязан сам двигать current и увеличивать state.round.
MANUAL_TURN_ORDER = False
SEED = 42                     # фиксируем случайность ради повторяемости

# --- План прогонов. Базовый прогон обязателен и всегда первый. -------------
# Диагност/оркестратор ДОПИСЫВАЕТ сюда доп. прогоны, не меняя базовый.
RUN_PLAN = [
    {"id": "base", "player_counts": None, "config": {}},
    {"id": "persona_passive",    "player_counts": None, "config": {"persona": "passive"}},
    {"id": "persona_aggressive", "player_counts": None, "config": {"persona": "aggressive"}},
    {"id": "persona_expert",     "player_counts": None, "config": {"persona": "expert"}},
    # смешанные составы: персона только у части мест, остальные играют случайно
    {"id": "mixed_expert_seat0", "player_counts": None,
     "config": {"persona": "expert", "persona_seats": [0]}},
    {"id": "mixed_coalition_01", "player_counts": None,
     "config": {"persona": "coalition", "persona_seats": [0, 1], "coalition_seats": [0, 1]}},
]
GAMES_PER_EXTRA_RUN = 600     # доп. прогоны короче базового — они точечные

# Лимиты поиска эксплойт-петли (защита от комбинаторного взрыва)
EXPLOIT_MAX_DEPTH = 6
EXPLOIT_MAX_NODES = 5000


# ============================================================================
# СОСТОЯНИЕ ИГРЫ (расширяйте игровые поля под свою игру)
# ============================================================================
@dataclass
class GameState:
    num_players: int
    current: int = 0
    round: int = 1
    phase: str = "main"
    active: list = field(default_factory=list)

    # --- игровые поля (ПРИМЕР; замените на свои сущности) ---
    money: dict = field(default_factory=dict)
    position: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    # --- служебный учёт для статистики (НЕ трогать) ---
    total_actions: int = 0
    action_counts: dict = field(default_factory=dict)
    per_seat_actions: dict = field(default_factory=dict)
    eliminated_order: list = field(default_factory=list)


# ============================================================================
# СКЕЛЕТ — ЗАПОЛНИТЬ ПОД СВОЮ ИГРУ (имена и сигнатуры не менять)
# ПРИМЕР: «Денежная гонка» — трек 12 клеток, цель — позиция 24, деньги < 0 = вылет
# ============================================================================
GOAL = 24
TRACK = 12


def setup_game(num_players, rng, config):
    """Стартовые условия. Обязан учитывать config: fixed_deal, disable_catchup."""
    st = GameState(num_players=num_players)
    st.active = list(range(num_players))
    st.current = 0
    fixed = (config or {}).get("fixed_deal")
    for s in range(num_players):
        st.money[s] = 10
        st.position[s] = 0
        st.per_seat_actions[s] = 0
        # ПРИМЕР карт на руке: нужны для тестов баланса артефактов
        if fixed and s in fixed:
            st.extra.setdefault("hand", {})[s] = list(fixed[s])
        else:
            st.extra.setdefault("hand", {})[s] = [
                rng.choice(["bonus", "trap", "swap", "shield"]) for _ in range(2)
            ]
    st.extra["catchup_on"] = not (config or {}).get("disable_catchup", False)
    return st


def is_game_over(state):
    if any(state.position.get(s, 0) >= GOAL for s in state.active):
        return True
    if len(state.active) <= 1:
        return True
    return False


def get_legal_actions(state):
    s = state.current
    if s not in state.active:
        return []
    actions = ["move"]
    if state.money[s] >= 3:
        actions.append("invest")
    return actions


def choose_action(state, legal_actions, rng):
    """БАЗОВЫЙ выбор — ВСЕГДА случайный. Эталон для «Оценщика статистик»."""
    return rng.choice(legal_actions)


def apply_action(state, action, rng):
    s = state.current
    if action == "move":
        roll = rng.randint(1, 6)
        state.position[s] += roll
        tile = state.position[s] % TRACK
        kind = tile % 3
        if kind == 0:
            state.money[s] += 2
        elif kind == 1:
            state.money[s] -= 1
        else:
            event = rng.choice(["+3", "-2", "skip"])
            if event == "+3":
                state.money[s] += 3
            elif event == "-2":
                state.money[s] -= 2
    elif action == "invest":
        state.money[s] -= 3
        # ДОПУЩЕНИЕ: исход инвестиции субъективен — моделируем случайной величиной
        payoff = max(0, round(rng.gauss(4, 3)))
        state.money[s] += payoff
    # ПРИМЕР catch_up: отстающий получает поддержку, если механика включена
    if state.extra.get("catchup_on") and state.position.get(s, 0) < state.round:
        state.money[s] += 1
    if state.money[s] < 0:
        _eliminate(state, s)


def determine_result(state):
    goal_winners = sorted(
        [s for s in state.active if state.position.get(s, 0) >= GOAL],
        key=lambda s: -state.position[s],
    )
    if goal_winners:
        winner, reason, path = goal_winners[0], "goal_reached", "race"
    elif len(state.active) == 1:
        winner, reason, path = state.active[0], "last_standing", "attrition"
    elif len(state.active) == 0:
        winner, reason, path = None, "all_eliminated", None
    else:
        winner, reason, path = None, "round_cap", None
    return {
        "winner": winner,
        "reason": reason,
        "rounds": state.round,
        "total_actions": state.total_actions,
        "eliminated": list(state.eliminated_order),
        "final_scores": {s: state.money.get(s, 0) for s in range(state.num_players)},
        "action_counts": dict(state.action_counts),
        "per_seat_actions": dict(state.per_seat_actions),
        "win_path": path,
    }


# --- ХУКИ ДИАГНОСТИКИ (обязательны к заполнению) ---------------------------

def snapshot_metric(state):
    """Метрика ПОБЕДЫ по каждому месту (core.win_condition.metric)."""
    return {s: state.position.get(s, 0) for s in range(state.num_players)}


def snapshot_resources(state):
    """Тратимые РЕСУРСЫ по каждому месту (core.resources). {} если ресурсов нет."""
    return {s: state.money.get(s, 0) for s in range(state.num_players)}


def hand_snapshot(state):
    """Карты/артефакты на руках. {} если карт в игре нет."""
    return {s: list(v) for s, v in state.extra.get("hand", {}).items()}


def state_signature(state):
    """Хешируемая подпись состояния — для поиска петель внутри хода."""
    return (
        state.current,
        state.phase,
        tuple(sorted(state.position.items())),
        tuple(sorted(state.money.items())),
        tuple(sorted(state.active)),
    )


def clone_state(state):
    """Глубокая копия состояния для теневого просчёта."""
    st = GameState(num_players=state.num_players)
    st.current, st.round, st.phase = state.current, state.round, state.phase
    st.active = list(state.active)
    st.money = dict(state.money)
    st.position = dict(state.position)
    st.extra = {k: (dict(v) if isinstance(v, dict) else v) for k, v in state.extra.items()}
    if "hand" in st.extra:
        st.extra["hand"] = {k: list(v) for k, v in state.extra["hand"].items()}
    st.total_actions = state.total_actions
    st.action_counts = dict(state.action_counts)
    st.per_seat_actions = dict(state.per_seat_actions)
    st.eliminated_order = list(state.eliminated_order)
    return st


# --- ПЕРСОНЫ (сигнатура с config; базовый choose_action НЕ трогать) ---------

def choose_action_passive(state, legal_actions, rng, config):
    return _greedy_pick(state, legal_actions, w_metric=0.0, w_resource=1.0)


def choose_action_aggressive(state, legal_actions, rng, config):
    return _greedy_pick(state, legal_actions, w_metric=1.0, w_resource=-0.2)


def choose_action_novice(state, legal_actions, rng, config):
    return rng.choice(legal_actions)


def choose_action_expert(state, legal_actions, rng, config):
    return _greedy_pick(state, legal_actions, w_metric=1.0, w_resource=0.3)


def choose_action_coalition(state, legal_actions, rng, config):
    # ДОПУЩЕНИЕ: в этой игре нет адресных действий на другого игрока —
    # сговор не выразим механически, поведение совпадает с базовым.
    return rng.choice(legal_actions)


def _eliminate(state, seat):
    if seat in state.active:
        state.active.remove(seat)
        state.eliminated_order.append(seat)


# ============================================================================
# ДВИЖОК — НЕ МЕНЯТЬ
# ============================================================================
_PROBE_RNG = random.Random(999983)   # отдельный поток для теневых просчётов


def _greedy_pick(state, legal_actions, w_metric=1.0, w_resource=0.0):
    """Общая эвристика персон: теневой просчёт каждого действия и выбор лучшего
    по взвешенной сумме (метрика победы, ресурсы). Не расходует боевой rng."""
    if not legal_actions:
        return None
    actor = state.current
    best, best_val, vals = legal_actions[0], None, []
    for a in legal_actions:
        try:
            shadow = clone_state(state)
            m0 = snapshot_metric(state).get(actor, 0)
            r0 = snapshot_resources(state).get(actor, 0)
            apply_action(shadow, a, _PROBE_RNG)
            m1 = snapshot_metric(shadow).get(actor, 0)
            r1 = snapshot_resources(shadow).get(actor, 0)
            val = w_metric * (m1 - m0) + w_resource * (r1 - r0)
        except Exception:
            continue
        vals.append(val)
        if best_val is None or val > best_val:
            best, best_val = a, val
    # если все варианты неразличимы (эффект действия отложен или субъективен),
    # жадность не даёт информации — выбираем случайно, иначе персона выродится
    # в детерминированное "всегда первое действие" и сломает всю статистику
    if not vals or max(vals) - min(vals) < 1e-9:
        return _PROBE_RNG.choice(legal_actions)
    return best


_PERSONAS = {
    "passive": choose_action_passive,
    "aggressive": choose_action_aggressive,
    "novice": choose_action_novice,
    "expert": choose_action_expert,
    "coalition": choose_action_coalition,
}


def _pick_action(state, legal, rng, config):
    cfg = config or {}
    persona = cfg.get("persona")
    seats = cfg.get("persona_seats")          # None = персона у всех игроков
    if seats is not None and state.current not in seats:
        persona = cfg.get("persona_others")   # у остальных: другая персона или None = случайно
    fn = _PERSONAS.get(persona)
    if fn is None:
        return choose_action(state, legal, rng)
    return fn(state, legal, rng, cfg)


def advance(state):
    n = state.num_players
    start = state.current
    for step in range(1, n + 1):
        nxt = (start + step) % n
        if nxt in state.active:
            if nxt <= start:
                state.round += 1
            state.current = nxt
            return
    state.round += 1


def _all_blocked(state):
    saved = state.current
    blocked = True
    for s in list(state.active):
        state.current = s
        if get_legal_actions(state):
            blocked = False
            break
    state.current = saved
    return blocked


def _leader_of(metric_map):
    """Единоличный лидер или None при равенстве (для предопределённости)."""
    if not metric_map:
        return None
    best = max(metric_map.values())
    leaders = [s for s, v in metric_map.items() if v == best]
    return leaders[0] if len(leaders) == 1 else None


def _leader_set(metric_map):
    """Множество лидеров — смена состава и есть смена лидерства."""
    if not metric_map:
        return frozenset()
    best = max(metric_map.values())
    return frozenset(s for s, v in metric_map.items() if v == best)


def _downsample_trajectory(traj):
    if not traj:
        return {}
    out, n = {}, len(traj)
    for label, frac in (("q25", 0.25), ("q50", 0.50), ("q75", 0.75), ("q100", 1.0)):
        idx = min(n - 1, max(0, int(round(frac * n)) - 1))
        rnd, metric, res = traj[idx]
        out[label] = {"round": rnd, "metric": dict(metric), "resources": dict(res)}
    return out


def run_single_game(num_players, rng, config):
    config = config or {}
    state = setup_game(num_players, rng, config)
    initial_hand = hand_snapshot(state)

    hard_guard = MAX_ROUNDS * max(1, num_players) * 20
    steps = 0
    softlock_events = 0
    full_block_events = 0
    pass_events = 0
    lead_changes = 0
    effect_sum, effect_res, effect_any, effect_cnt = {}, {}, {}, {}
    elim_rounds = {}
    seen_elim = set()

    traj = [(state.round, snapshot_metric(state), snapshot_resources(state))]
    last_round = state.round
    last_leader = _leader_set(traj[0][1])

    while not is_game_over(state) and state.round <= MAX_ROUNDS:
        steps += 1
        if steps > hard_guard:
            break

        legal = get_legal_actions(state)
        if not legal:
            # у текущего места нет ходов. Это НЕ софтлок, если ходить может кто-то ещё:
            # в фазовых играх «сейчас не твоя фаза» — нормальное состояние.
            if _all_blocked(state):
                softlock_events += 1
                full_block_events += 1
                break                      # партия физически не может продолжаться
            pass_events += 1
            advance(state)                 # аварийный сдвиг и в ручном режиме тоже
            continue

        actor = state.current
        m_before = snapshot_metric(state)
        before = m_before.get(actor, 0)
        before_total = sum(m_before.values())
        before_r = snapshot_resources(state).get(actor, 0)
        action = _pick_action(state, legal, rng, config)
        apply_action(state, action, rng)
        m_after = snapshot_metric(state)
        after = m_after.get(actor, 0)
        after_total = sum(m_after.values())
        after_r = snapshot_resources(state).get(actor, 0)

        # логируем ТИП действия: параметр после ":" отбрасываем, иначе
        # каждая карта станет отдельным "действием" и action_share потеряет смысл
        name = (action if isinstance(action, str) else str(action)).split(":")[0]
        state.total_actions += 1
        state.action_counts[name] = state.action_counts.get(name, 0) + 1
        state.per_seat_actions[actor] = state.per_seat_actions.get(actor, 0) + 1
        effect_sum[name] = effect_sum.get(name, 0.0) + (after - before)
        effect_res[name] = effect_res.get(name, 0.0) + (after_r - before_r)
        effect_any[name] = effect_any.get(name, 0.0) + (after_total - before_total)
        effect_cnt[name] = effect_cnt.get(name, 0) + 1

        for s in state.eliminated_order:
            if s not in seen_elim:
                seen_elim.add(s)
                elim_rounds[s] = state.round

        if is_game_over(state):
            break
        if not MANUAL_TURN_ORDER:
            advance(state)

        if state.round != last_round:
            snap = snapshot_metric(state)
            traj.append((state.round, snap, snapshot_resources(state)))
            ld = _leader_set(snap)
            if ld and last_leader and ld != last_leader:
                lead_changes += 1
            last_leader, last_round = ld, state.round

    traj.append((state.round, snapshot_metric(state), snapshot_resources(state)))
    res = determine_result(state)
    res["_diag"] = {
        "softlock_events": softlock_events,
        "full_block_events": full_block_events,
        "pass_events": pass_events,
        "lead_changes": lead_changes,
        "effect_sum": effect_sum,
        "effect_res": effect_res,
        "effect_any": effect_any,
        "effect_cnt": effect_cnt,
        "elim_rounds": elim_rounds,
        "trajectory": _downsample_trajectory(traj),
        "initial_hand": initial_hand,
        "resources_end": snapshot_resources(state),
    }
    return res


def _stats(xs):
    if not xs:
        return {"mean": 0, "median": 0, "min": 0, "max": 0, "stdev": 0}
    return {
        "mean": round(statistics.mean(xs), 3),
        "median": round(statistics.median(xs), 3),
        "min": min(xs),
        "max": max(xs),
        "stdev": round(statistics.pstdev(xs), 3),
    }


def _normal_cdf(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def _chi2_pvalue(chi2_stat, df):
    if df <= 0 or chi2_stat < 0:
        return 1.0
    x = chi2_stat / df
    z = (x ** (1 / 3) - (1 - 2 / (9 * df))) / math.sqrt(2 / (9 * df))
    return round(1 - _normal_cdf(z), 4)


def _chi_square_gof(observed, expected):
    chi2 = sum((o - e) ** 2 / e for o, e in zip(observed, expected) if e > 0)
    df = len(observed) - 1
    return chi2, _chi2_pvalue(chi2, df), df


def _wilson_ci(count, n, z=1.96):
    if n == 0:
        return {"lower": 0.0, "upper": 0.0}
    phat = count / n
    denom = 1 + z ** 2 / n
    center = phat + z ** 2 / (2 * n)
    margin = z * math.sqrt(phat * (1 - phat) / n + z ** 2 / (4 * n ** 2))
    return {"lower": round(max(0.0, (center - margin) / denom), 4),
            "upper": round(min(1.0, (center + margin) / denom), 4)}


def _cramers_v(chi2_stat, n, k):
    if n == 0 or k <= 1:
        return 0.0
    return round(math.sqrt(chi2_stat / (n * (k - 1))), 4)


def _gini(values):
    vals = sorted(values)
    n = len(vals)
    if n == 0 or sum(vals) == 0:
        return 0.0
    cumulative = sum((i + 1) * v for i, v in enumerate(vals))
    return round((2 * cumulative) / (n * sum(vals)) - (n + 1) / n, 4)


def _skewness(xs):
    n = len(xs)
    if n < 3:
        return 0.0
    mean = statistics.mean(xs)
    sd = statistics.pstdev(xs)
    if sd == 0:
        return 0.0
    m3 = sum((x - mean) ** 3 for x in xs) / n
    return round(m3 / (sd ** 3), 4)


def aggregate(results, num_players):
    """Базовая статистика — формат ПОЛНОСТЬЮ совместим с v3."""
    n = len(results)
    wins = {s: 0 for s in range(num_players)}
    no_winner = 0
    reasons, action_freq = {}, {}
    rounds, actions = [], []
    final_by_seat = {s: [] for s in range(num_players)}
    total_eliminated = 0
    games_with_elim = 0

    for r in results:
        w = r["winner"]
        if w is None:
            no_winner += 1
        else:
            wins[w] = wins.get(w, 0) + 1
        reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
        rounds.append(r["rounds"])
        actions.append(r["total_actions"])
        for a, c in r["action_counts"].items():
            action_freq[a] = action_freq.get(a, 0) + c
        for s, v in r["final_scores"].items():
            final_by_seat[int(s)].append(v)
        total_eliminated += len(r["eliminated"])
        if r["eliminated"]:
            games_with_elim += 1

    total_act = sum(action_freq.values()) or 1
    rounds_stats = _stats(rounds)
    final_stats_by_seat = {s: _stats(final_by_seat[s]) for s in range(num_players)}
    total_wins = sum(wins.values())

    if total_wins > 0:
        seat_chi2, seat_p, seat_df = _chi_square_gof(
            [wins[s] for s in range(num_players)],
            [total_wins / num_players] * num_players)
    else:
        seat_chi2, seat_p, seat_df = 0.0, 1.0, num_players - 1

    k_actions = len(action_freq) or 1
    if action_freq:
        action_chi2, action_p, action_df = _chi_square_gof(
            list(action_freq.values()), [total_act / k_actions] * k_actions)
    else:
        action_chi2, action_p, action_df = 0.0, 1.0, 0

    return {
        "games": n,
        "num_players": num_players,
        "win_rate_by_seat": {s: round(wins[s] / n, 4) for s in range(num_players)},
        "no_winner_rate": round(no_winner / n, 4),
        "end_reason_share": {k: round(v / n, 4) for k, v in reasons.items()},
        "rounds": rounds_stats,
        "actions_per_game": _stats(actions),
        "action_share": {a: round(c / total_act, 4)
                         for a, c in sorted(action_freq.items(), key=lambda kv: -kv[1])},
        "final_score_by_seat": final_stats_by_seat,
        "avg_eliminated_per_game": round(total_eliminated / n, 3),
        "seat_fairness": {"chi2": round(seat_chi2, 3), "p_value": seat_p,
                          "significant": seat_p < 0.05, "df": seat_df},
        "seat_fairness_effect": _cramers_v(seat_chi2, total_wins, num_players),
        "seat_ci": {s: _wilson_ci(wins[s], n) for s in range(num_players)},
        "duration_cv": round(rounds_stats["stdev"] / rounds_stats["mean"], 4)
                       if rounds_stats["mean"] else 0.0,
        "duration_skew": _skewness(rounds),
        "action_dominance": {"chi2": round(action_chi2, 3), "p_value": action_p,
                             "significant": action_p < 0.05, "df": action_df},
        "action_dominance_effect": _cramers_v(action_chi2, total_act, k_actions),
        "score_gini": _gini([final_stats_by_seat[s]["mean"] for s in range(num_players)]),
        "elimination_rate": round(games_with_elim / n, 4),
    }


def aggregate_diagnostics(results, num_players):
    """Диагностическая статистика — читает агент-диагност. На базовую не влияет."""
    n = len(results) or 1
    soft = sum(1 for r in results if r["_diag"]["softlock_events"] > 0)
    full = sum(1 for r in results if r["_diag"]["full_block_events"] > 0)
    passes = sum(1 for r in results if r["_diag"]["pass_events"] > 0)

    eff_sum, eff_res, eff_any, eff_cnt = {}, {}, {}, {}
    for r in results:
        for a, v in r["_diag"]["effect_sum"].items():
            eff_sum[a] = eff_sum.get(a, 0.0) + v
            eff_res[a] = eff_res.get(a, 0.0) + r["_diag"]["effect_res"].get(a, 0.0)
            eff_any[a] = eff_any.get(a, 0.0) + r["_diag"]["effect_any"].get(a, 0.0)
            eff_cnt[a] = eff_cnt.get(a, 0) + r["_diag"]["effect_cnt"].get(a, 0)
    action_effect = {a: round(eff_sum[a] / eff_cnt[a], 4) for a in eff_sum if eff_cnt.get(a)}
    action_effect_resource = {a: round(eff_res[a] / eff_cnt[a], 4)
                              for a in eff_res if eff_cnt.get(a)}
    # эффект на игру в целом: ловит действия, которые дают очки ДРУГОМУ игроку
    action_effect_any = {a: round(eff_any[a] / eff_cnt[a], 4)
                         for a in eff_any if eff_cnt.get(a)}

    # предопределённость: лидер на середине == итоговый победитель
    mid_hits, mid_total = 0, 0
    for r in results:
        t = r["_diag"]["trajectory"]
        if not t or r["winner"] is None:
            continue
        ld = _leader_of(t.get("q50", {}).get("metric", {}))
        if ld is None:
            continue
        mid_total += 1
        if ld == r["winner"]:
            mid_hits += 1

    # спираль смерти: во сколько раз разрыв лидер/аутсайдер вырос между q25 и q100
    spirals = []
    for r in results:
        t = r["_diag"]["trajectory"]
        a, b = t.get("q25", {}).get("metric"), t.get("q100", {}).get("metric")
        if not a or not b:
            continue
        ga, gb = max(a.values()) - min(a.values()), max(b.values()) - min(b.values())
        if ga > 0:
            spirals.append(gb / ga)

    # инфляция: суммарный ресурс/метрика в обороте q25 -> q100
    infl = []
    for r in results:
        t = r["_diag"]["trajectory"]
        a, b = t.get("q25", {}).get("resources"), t.get("q100", {}).get("resources")
        if a and b and sum(a.values()) > 0:
            infl.append(sum(b.values()) / sum(a.values()))

    # застой: партии по round_cap, где метрика не менялась в последней четверти
    cap = [r for r in results if r["reason"] == "round_cap"]
    stagnant = 0
    for r in cap:
        t = r["_diag"]["trajectory"]
        if t.get("q75", {}).get("metric") == t.get("q100", {}).get("metric"):
            stagnant += 1

    # беспомощность после вылета
    idle = []
    for r in results:
        for s, er in r["_diag"]["elim_rounds"].items():
            if r["rounds"] > 0:
                idle.append((r["rounds"] - er) / r["rounds"])

    # пути победы
    paths = {}
    for r in results:
        p = r.get("win_path")
        if p:
            paths[p] = paths.get(p, 0) + 1
    tot_paths = sum(paths.values()) or 1

    # карты: винрейт при наличии карты в стартовой руке
    card_games, card_wins = {}, {}
    pair_games, pair_wins = {}, {}
    for r in results:
        hand = r["_diag"]["initial_hand"] or {}
        for s, cards in hand.items():
            won = (r["winner"] == s)
            uniq = sorted(set(cards))
            for c in uniq:
                card_games[c] = card_games.get(c, 0) + 1
                if won:
                    card_wins[c] = card_wins.get(c, 0) + 1
            for i in range(len(uniq)):
                for j in range(i + 1, len(uniq)):
                    key = f"{uniq[i]}+{uniq[j]}"
                    pair_games[key] = pair_games.get(key, 0) + 1
                    if won:
                        pair_wins[key] = pair_wins.get(key, 0) + 1

    MIN_CARD, MIN_PAIR = 30, 15
    card_winrate = {c: round(card_wins.get(c, 0) / g, 4)
                    for c, g in card_games.items() if g >= MIN_CARD}
    pair_winrate = {p: round(pair_wins.get(p, 0) / g, 4)
                    for p, g in pair_games.items() if g >= MIN_PAIR}
    card_coverage = {
        "cards_seen": len(card_games),
        "cards_with_enough_data": len(card_winrate),
        "pairs_seen": len(pair_games),
        "pairs_with_enough_data": len(pair_winrate),
        "min_games_card": MIN_CARD, "min_games_pair": MIN_PAIR,
        "sufficient": len(card_winrate) > 0,
        "pair_sufficient": len(pair_winrate) > 0,
    }

    return {
        "num_players": num_players,
        "games": len(results),
        "softlock_rate": round(soft / n, 4),
        "full_block_rate": round(full / n, 4),
        "pass_rate": round(passes / n, 4),
        "action_effect": action_effect,
        "action_effect_resource": action_effect_resource,
        "action_effect_any": action_effect_any,
        "predetermination_rate": round(mid_hits / mid_total, 4) if mid_total else None,
        "spiral_ratio": (dict(_stats(spirals), samples=len(spirals))
                         if len(spirals) >= 30 else None),
        "resource_inflation": _stats(infl) if infl else None,
        "stagnation_rate": round(stagnant / len(cap), 4) if cap else None,
        "idle_after_elimination": _stats(idle) if idle else None,
        "lead_changes": _stats([r["_diag"]["lead_changes"] for r in results]),
        "win_path_share": {k: round(v / tot_paths, 4) for k, v in paths.items()},
        "card_coverage": card_coverage,
        "card_winrate": dict(sorted(card_winrate.items(), key=lambda kv: -kv[1])),
        "pair_winrate": dict(sorted(pair_winrate.items(), key=lambda kv: -kv[1])[:20]),
        "final_score_stdev": round(statistics.pstdev(
            [v for r in results for v in r["final_scores"].values()]), 4)
            if results else 0.0,
    }


def exploit_search(num_players, rng, config):
    """Поиск петли ВНУТРИ хода: путь, возвращающий состояние в уже виденную
    подпись, но с бОльшей метрикой. Глубина и число узлов ограничены."""
    state = setup_game(num_players, rng, config or {})
    found, counter = [], {"nodes": 0}

    def dfs(st, path, seen):
        if found or counter["nodes"] > EXPLOIT_MAX_NODES or len(path) > EXPLOIT_MAX_DEPTH:
            return
        try:
            sig = state_signature(st)
            metric = sum(snapshot_metric(st).values())
        except Exception:
            return
        if sig in seen and metric > seen[sig] + 1e-9:
            found.append({"path": list(path), "gain": round(metric - seen[sig], 4)})
            return
        nxt_seen = dict(seen)
        nxt_seen[sig] = min(nxt_seen.get(sig, metric), metric)
        try:
            legal = get_legal_actions(st)
        except Exception:
            return
        for a in legal:
            counter["nodes"] += 1
            if counter["nodes"] > EXPLOIT_MAX_NODES:
                return
            try:
                child = clone_state(st)
                apply_action(child, a, _PROBE_RNG)
            except Exception:
                continue
            dfs(child, path + [a], nxt_seen)

    dfs(state, [], {})
    return {
        "exploit_found": bool(found),
        "examples": found[:3],
        "nodes_visited": counter["nodes"],
        "depth_cap": EXPLOIT_MAX_DEPTH,
        "search_exhausted": counter["nodes"] <= EXPLOIT_MAX_NODES,
    }


def print_report(base_stats, diag_payload):
    print("=" * 64)
    print("ОТЧЁТ СИМУЛЯЦИИ СКЕЛЕТА ИГРЫ")
    print("=" * 64)
    for st in base_stats:
        print(f"\n--- {st['num_players']} игрока(ов) · {st['games']} партий ---")
        print("Винрейт по местам:      ", st["win_rate_by_seat"])
        print("Партий без победителя:  ", st["no_winner_rate"])
        print("Причины завершения:     ", st["end_reason_share"])
        print("Длительность, раунды:   ", st["rounds"])
        print("Доля действий:          ", st["action_share"])
        print("Справедливость мест:    ", st["seat_fairness"])
        print("Gini по очкам:          ", st["score_gini"])

    print("\n" + "=" * 64)
    print("STATS_JSON  (для «Оценщика статистик» — формат v3, не менять)")
    print("=" * 64)
    print(json.dumps(base_stats, ensure_ascii=False, indent=2))

    print("\n" + "=" * 64)
    print("DIAG_JSON  (для «Агента-диагноста»)")
    print("=" * 64)
    print(json.dumps(diag_payload, ensure_ascii=False, indent=2))


def main():
    master = random.Random(SEED)
    base_stats, diag_runs = [], []

    for plan in RUN_PLAN:
        counts = plan.get("player_counts") or PLAYER_COUNTS
        games = GAMES_PER_CONFIG if plan["id"] == "base" else GAMES_PER_EXTRA_RUN
        for npl in counts:
            rng = random.Random(master.randint(0, 10 ** 9))
            results = [run_single_game(npl, rng, plan["config"]) for _ in range(games)]
            if plan["id"] == "base":
                base_stats.append(aggregate(results, npl))
            diag_runs.append({
                "run_id": plan["id"],
                "config": plan["config"],
                "base_view": aggregate(results, npl),
                "diagnostics": aggregate_diagnostics(results, npl),
            })

    probe_rng = random.Random(SEED + 1)
    exploit = {str(npl): exploit_search(npl, probe_rng, {}) for npl in PLAYER_COUNTS}

    print_report(base_stats, {"runs": diag_runs, "exploit_search": exploit})


if __name__ == "__main__":
    main()
