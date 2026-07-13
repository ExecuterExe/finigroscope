"""Агрегация результатов партий в метрики и флаги (раздел 5.3 проектного документа).

Акцент на РАЗМЕРЕ ЭФФЕКТА, а не на p-value: при N=10000 «статзначимым» становится
любое микроотличие, поэтому показываем отклонение win rate от честной доли и
доверительные интервалы, а не только χ².
"""

import math
import statistics


def _stats(xs):
    if not xs:
        return {"mean": 0, "median": 0, "min": 0, "max": 0, "p05": 0, "p95": 0}
    xs_sorted = sorted(xs)
    n = len(xs_sorted)
    def pct(p):
        k = max(0, min(n - 1, int(round(p * (n - 1)))))
        return xs_sorted[k]
    return {
        "mean": round(statistics.mean(xs), 2),
        "median": round(statistics.median(xs), 2),
        "min": min(xs),
        "max": max(xs),
        "p05": pct(0.05),
        "p95": pct(0.95),
    }


def _wilson_ci(k, n, z=1.96):
    """Доверительный интервал Вилсона для доли (надёжнее нормального на краях)."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def _histogram(xs, bins=12):
    if not xs:
        return {"edges": [], "counts": []}
    lo, hi = min(xs), max(xs)
    if lo == hi:
        return {"edges": [lo, lo + 1], "counts": [len(xs)]}
    width = (hi - lo) / bins
    counts = [0] * bins
    for x in xs:
        idx = min(bins - 1, int((x - lo) / width))
        counts[idx] += 1
    edges = [round(lo + i * width, 1) for i in range(bins + 1)]
    return {"edges": edges, "counts": counts}


def aggregate(results, n_players):
    """Сводит список результатов партий в метрики для одной конфигурации игроков."""
    n = len(results)
    if n == 0:
        return {}

    wins = {s: 0 for s in range(n_players)}
    ties = 0
    no_winner = 0
    reasons = {}
    rounds = []
    turns = []
    actions_per_game = []
    deck_exhausted = 0
    elim_total = 0
    final_metric_by_seat = {s: [] for s in range(n_players)}
    action_freq = {}

    # какой показатель определяет лидера — движок сообщает явно (metric_used)
    metric_map = {"pos": "final_pos", "score": "final_score", "res": "final_res"}
    metric_key = metric_map.get(results[0].get("metric_used"), "final_score")

    for r in results:
        if r["tie"]:
            ties += 1
        if r["winner"] is None:
            no_winner += 1
        else:
            wins[r["winner"]] = wins.get(r["winner"], 0) + 1
        reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
        rounds.append(r["rounds"])
        turns.append(r.get("turns", r["rounds"]))
        actions_per_game.append(r.get("total_actions", 0))
        if r["deck_exhausted"]:
            deck_exhausted += 1
        elim_total += len(r["eliminated"])
        for s, v in r[metric_key].items():
            final_metric_by_seat[int(s)].append(v)
        for a, c in r["actions"].items():
            action_freq[a] = action_freq.get(a, 0) + c

    fair = 1.0 / n_players
    win_rate = {s: wins[s] / n for s in range(n_players)}
    ci = {s: _wilson_ci(wins[s], n) for s in range(n_players)}

    # χ² на равномерность + размер эффекта (макс. отклонение от честной доли)
    expected = n / n_players
    chi2 = sum((wins[s] - expected) ** 2 / expected for s in range(n_players)) if expected else 0
    max_dev = max(abs(win_rate[s] - fair) for s in range(n_players))
    first_player_edge = win_rate[0] - fair

    total_act = sum(action_freq.values()) or 1
    leader_gaps = []
    for r in results:
        vals = list(r[metric_key].values())
        if len(vals) >= 2:
            srt = sorted(vals, reverse=True)
            leader_gaps.append(srt[0] - srt[1])

    return {
        "n_players": n_players,
        "games": n,
        "fair_share": round(fair, 4),
        "win_rate_by_seat": {s: round(win_rate[s], 4) for s in range(n_players)},
        "win_rate_ci": {s: [round(ci[s][0], 4), round(ci[s][1], 4)] for s in range(n_players)},
        "first_player_edge": round(first_player_edge, 4),
        "max_seat_deviation": round(max_dev, 4),
        "chi2": round(chi2, 2),
        "tie_rate": round(ties / n, 4),
        "no_winner_rate": round(no_winner / n, 4),
        "end_reason_share": {k: round(v / n, 4) for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])},
        "rounds": _stats(rounds),
        "rounds_hist": _histogram(rounds),
        "turns": _stats(turns),
        "actions_per_game": _stats(actions_per_game),
        "actions_hist": _histogram(actions_per_game),
        "deck_exhausted_rate": round(deck_exhausted / n, 4),
        "avg_eliminated": round(elim_total / n, 3),
        "final_metric_by_seat": {s: _stats(final_metric_by_seat[s]) for s in range(n_players)},
        "leader_gap": _stats(leader_gaps),
        "action_share": {a: round(c / total_act, 4)
                         for a, c in sorted(action_freq.items(), key=lambda kv: -kv[1])},
        "metric_kind": metric_key,
    }
