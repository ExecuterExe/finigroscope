"""
==============================================================================
 СКЕЛЕТ-СИМУЛЯТОР НАСТОЛЬНОЙ ИГРЫ  (проверка технического костяка)
==============================================================================
 Прогоняет игровой ЦИКЛ много раз и собирает статистику (винрейт по местам,
 достижимость победы, длительность, частоту действий, разброс финансов,
 а также статистическую значимость и размер эффекта по ключевым метрикам).
 Это проверка СКЕЛЕТА, а не содержания: графику/арт/поле модель не оценивает.

 Запуск: вставьте файл целиком на https://www.online-python.com/ и нажмите Run.
 Зависимостей нет - только стандартная библиотека.

 ПРАВИЛА ПРАВКИ:
   - Блок «ДВИЖОК» НЕ ТРОГАТЬ - он гоняет цикл, считает статистику и держит
     предохранитель от бесконечной партии.
   - Блок «СКЕЛЕТ» - заполнить под игру, НЕ меняя имена/сигнатуры функций.
     Можно добавлять поля в GameState и свои хелперы.
   - Блок «ПРИМЕР» - заменить на свою игру.
   - Непредсказуемое/субъективное моделируется СЛУЧАЙНОЙ величиной
     (см. действие "invest"). Каждое упрощение помечать  # ДОПУЩЕНИЕ: ...

 !!! ASCII В КОДЕ !!! Вне комментариев - только ASCII. Прямые кавычки " ' (не
     ёлочки), дефис - , многоточие ... . Имена и ключи - латиницей. Иначе
     Python даёт синтаксическую ошибку. Без match/case и без аннотаций вида
     int | None.
==============================================================================
"""

import random
import statistics
import json
from dataclasses import dataclass, field
import math

# ============================================================================
# КОНФИГУРАЦИЯ  (заменить под свою игру)
# ============================================================================
GAMES_PER_CONFIG = 2000          # партий на конфигурацию числа игроков
PLAYER_COUNTS = [2, 3, 4]        # min / типичное / max число игроков
MAX_ROUNDS = 200                 # ПРЕДОХРАНИТЕЛЬ от бесконечной партии - НЕ удалять
SEED = 42                        # фиксируем случайность ради повторяемости


# ============================================================================
# СОСТОЯНИЕ ИГРЫ  (расширяйте игровые поля под свою игру)
# ============================================================================
@dataclass
class GameState:
    num_players: int
    current: int = 0                                   # чей ход; 0..n-1 - это "место"/seat
    round: int = 1
    phase: str = "main"
    active: list = field(default_factory=list)         # кто ещё в игре (seat'ы)

    # --- игровые поля (ПРИМЕР; замените на свои сущности - линза 22) ---
    money: dict = field(default_factory=dict)          # seat -> деньги
    position: dict = field(default_factory=dict)       # seat -> позиция на треке
    extra: dict = field(default_factory=dict)          # любые доп. сущности

    # --- служебный учёт для статистики (НЕ трогать) ---
    total_actions: int = 0
    action_counts: dict = field(default_factory=dict)
    per_seat_actions: dict = field(default_factory=dict)
    eliminated_order: list = field(default_factory=list)


# ============================================================================
# СКЕЛЕТ - ЗАПОЛНИТЬ ПОД СВОЮ ИГРУ  (имена и сигнатуры не менять)
# ----------------------------------------------------------------------------
# Ниже - рабочий ПРИМЕР «Денежная гонка»: трек из 12 клеток, цель - дойти до
# позиции 24 (два круга); при деньгах < 0 игрок вылетает. Удалите пример и
# опишите свою игру теми же функциями.
# ============================================================================

GOAL = 24          # ПРИМЕР: порог победы по позиции
TRACK = 12         # ПРИМЕР: длина круга


def setup_game(num_players, rng, config):
    """Стартовые условия. Вернуть готовый GameState. (Скелет, п.6 чек-листа)"""
    st = GameState(num_players=num_players)
    st.active = list(range(num_players))
    st.current = 0
    for s in range(num_players):
        st.money[s] = 10           # ПРИМЕР: стартовый капитал
        st.position[s] = 0
        st.per_seat_actions[s] = 0
    return st


def is_game_over(state):
    """True, если пора завершать (есть победитель или остался один). (п.10)"""
    if any(state.position.get(s, 0) >= GOAL for s in state.active):
        return True
    if len(state.active) <= 1:
        return True
    return False


def get_legal_actions(state):
    """Допустимые действия ТЕКУЩЕГО игрока в текущей фазе. (линза 24)"""
    s = state.current
    if s not in state.active:
        return []
    actions = ["move"]                 # ПРИМЕР: ходить можно всегда
    if state.money[s] >= 3:            # ПРИМЕР: "инвестировать" - при деньгах
        actions.append("invest")
    return actions


def choose_action(state, legal_actions, rng):
    """
    Выбор действия. По умолчанию - СЛУЧАЙНО: так моделируем непредсказуемость
    живого игрока и субъективные решения. (линза 32)
    """
    return rng.choice(legal_actions)


def apply_action(state, action, rng):
    """Применить действие: движение, экономика, спец-события. (линзы 22/24/46)"""
    s = state.current
    if action == "move":
        roll = rng.randint(1, 6)                 # ПРИМЕР: кубик
        state.position[s] += roll
        tile = state.position[s] % TRACK
        kind = tile % 3
        if kind == 0:
            state.money[s] += 2                  # ПРИМЕР: доход
        elif kind == 1:
            state.money[s] -= 1                  # ПРИМЕР: расход
        else:
            # ПРИМЕР спец-события: непредсказуемость моделируем рандомом
            event = rng.choice(["+3", "-2", "skip"])
            if event == "+3":
                state.money[s] += 3
            elif event == "-2":
                state.money[s] -= 2
    elif action == "invest":
        state.money[s] -= 3
        # ДОПУЩЕНИЕ: исход "инвестиции" зависит от субъективных факторов
        # (креатив/удача/оценка), не заданных числом, - моделируем СЛУЧАЙНОЙ
        # величиной (нормальное распределение).
        payoff = max(0, round(rng.gauss(4, 3)))
        state.money[s] += payoff

    if state.money[s] < 0:                        # ПРИМЕР: условие вылета (п.5)
        _eliminate(state, s)


def determine_result(state):
    """
    Зафиксировать исход. Вернуть словарь СТРОГО этого вида - ключи не менять:
        winner          : seat победителя или None (None = победы никто не достиг)
        reason          : причина завершения (строка)
        rounds          : длительность в раундах
        total_actions   : всего действий
        eliminated      : вылетевшие seat'ы в порядке вылета
        final_scores    : {seat: финальная метрика}
        action_counts   : {название_действия: сколько раз}
        per_seat_actions: {seat: сколько действий}
    """
    goal_winners = sorted(
        [s for s in state.active if state.position.get(s, 0) >= GOAL],
        key=lambda s: -state.position[s],
    )
    if goal_winners:
        winner, reason = goal_winners[0], "goal_reached"
    elif len(state.active) == 1:
        winner, reason = state.active[0], "last_standing"
    elif len(state.active) == 0:
        winner, reason = None, "all_eliminated"
    else:
        winner, reason = None, "round_cap"        # за лимит раундов победы не случилось

    return {
        "winner": winner,
        "reason": reason,
        "rounds": state.round,
        "total_actions": state.total_actions,
        "eliminated": list(state.eliminated_order),
        "final_scores": {s: state.money.get(s, 0) for s in range(state.num_players)},
        "action_counts": dict(state.action_counts),
        "per_seat_actions": dict(state.per_seat_actions),
    }


# --- вспомогательные функции игры (свои хелперы добавляйте здесь) ---
def _eliminate(state, seat):
    if seat in state.active:
        state.active.remove(seat)
        state.eliminated_order.append(seat)


# ============================================================================
# ДВИЖОК - НЕ МЕНЯТЬ
# ============================================================================
def advance(state):
    """Передать ход следующему активному игроку; считать раунды (приближённо)."""
    n = state.num_players
    start = state.current
    for step in range(1, n + 1):
        nxt = (start + step) % n
        if nxt in state.active:
            if nxt <= start:          # обернулись через начало стола -> новый раунд
                state.round += 1
            state.current = nxt
            return
    state.round += 1                  # активных не осталось


def run_single_game(num_players, rng, config):
    state = setup_game(num_players, rng, config)
    hard_guard = MAX_ROUNDS * max(1, num_players) * 20
    steps = 0
    while not is_game_over(state) and state.round <= MAX_ROUNDS:
        steps += 1
        if steps > hard_guard:
            break
        legal = get_legal_actions(state)
        if not legal:
            advance(state)
            continue
        actor = state.current
        action = choose_action(state, legal, rng)
        apply_action(state, action, rng)

        name = action if isinstance(action, str) else str(action)
        state.total_actions += 1
        state.action_counts[name] = state.action_counts.get(name, 0) + 1
        state.per_seat_actions[actor] = state.per_seat_actions.get(actor, 0) + 1

        if is_game_over(state):
            break
        advance(state)
    return determine_result(state)


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


# --- Вспомогательные функции для статистических тестов (чистый stdlib) ---
def _normal_cdf(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def _chi2_pvalue(chi2_stat, df):
    # Аппроксимация Уилсона-Хилферти: переводит хи-квадрат в стандартную нормаль
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
    seat_fairness = {"chi2": round(seat_chi2, 3), "p_value": seat_p,
                     "significant": seat_p < 0.05, "df": seat_df}
    seat_fairness_effect = _cramers_v(seat_chi2, total_wins, num_players)
    seat_ci = {s: _wilson_ci(wins[s], n) for s in range(num_players)}

    duration_cv = round(rounds_stats["stdev"] / rounds_stats["mean"], 4) \
        if rounds_stats["mean"] else 0.0
    duration_skew = _skewness(rounds)

    k_actions = len(action_freq) or 1
    if action_freq:
        action_chi2, action_p, action_df = _chi_square_gof(
            list(action_freq.values()),
            [total_act / k_actions] * k_actions)
    else:
        action_chi2, action_p, action_df = 0.0, 1.0, 0
    action_dominance = {"chi2": round(action_chi2, 3), "p_value": action_p,
                        "significant": action_p < 0.05, "df": action_df}
    action_dominance_effect = _cramers_v(action_chi2, total_act, k_actions)

    score_gini = _gini([final_stats_by_seat[s]["mean"] for s in range(num_players)])
    elimination_rate = round(games_with_elim / n, 4)

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
        "seat_fairness": seat_fairness,
        "seat_fairness_effect": seat_fairness_effect,
        "seat_ci": seat_ci,
        "duration_cv": duration_cv,
        "duration_skew": duration_skew,
        "action_dominance": action_dominance,
        "action_dominance_effect": action_dominance_effect,
        "score_gini": score_gini,
        "elimination_rate": elimination_rate,
    }


def print_report(all_stats):
    print("=" * 64)
    print("ОТЧЁТ СИМУЛЯЦИИ СКЕЛЕТА ИГРЫ")
    print("=" * 64)
    for st in all_stats:
        print("\n---", st["num_players"], "игрока(ов) -", st["games"], "партий ---")
        print("Винрейт по местам (seat):       ", st["win_rate_by_seat"])
        print("Партий без победителя:          ", st["no_winner_rate"],
              " (высокое => победа трудно/недостижима)")
        print("Причины завершения:             ", st["end_reason_share"])
        print("Длительность, раунды:           ", st["rounds"])
        print("Действий за партию:             ", st["actions_per_game"])
        print("Доля использования действий:    ", st["action_share"],
              " (~0 => мёртвое; доминирование => дисбаланс)")
        print("Финальные очки по местам:       ", st["final_score_by_seat"])
        print("Среднее вылетов за партию:      ", st["avg_eliminated_per_game"])
        print("Справедливость мест (хи-квадрат):     ", st["seat_fairness"],
              "| эффект (V Крамера):", st["seat_fairness_effect"])
        print("Доверительные интервалы по местам:     ", st["seat_ci"])
        print("Вариация длительности (CV):            ", st["duration_cv"],
              " | скошенность:", st["duration_skew"])
        print("Доминирование действий (хи-квадрат):   ", st["action_dominance"],
              "| эффект (V Крамера):", st["action_dominance_effect"])
        print("Неравенство очков между местами (Gini):", st["score_gini"])
        print("Доля партий хотя бы с одним вылетом:   ", st["elimination_rate"])
    print("\n" + "=" * 64)
    print("JSON ДЛЯ ОЦЕНКИ БАЛАНСА - скопируйте всё ниже и отправьте во вторую LLM-оценку:")
    print("=" * 64)
    print(json.dumps(all_stats, ensure_ascii=False, indent=2))


def main():
    master = random.Random(SEED)
    all_stats = []
    for npl in PLAYER_COUNTS:
        rng = random.Random(master.randint(0, 10**9))
        results = [run_single_game(npl, rng, {}) for _ in range(GAMES_PER_CONFIG)]
        all_stats.append(aggregate(results, npl))
    print_report(all_stats)


if __name__ == "__main__":
    main()
