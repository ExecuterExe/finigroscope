"""Пакетный прогон симуляции: сетки игроков и параметров-неизвестных + диагностика.

Берёт канонический конфиг конструктора, прогоняет N партий для каждого числа
игроков (и для каждого значения сетки p_correct — Принцип 4: «не спрашиваем,
а сканируем»), агрегирует метрики и применяет режим «Диагност».
"""

import random

from redesign import diagnostics
from simulation import engine, metrics

DEFAULT_GAMES = 4000
MAX_GAMES = 30000


def _player_counts(config):
    p = config.get("players") or {}
    lo = int(p.get("min", 2) or 2)
    hi = int(p.get("max", lo) or lo)
    lo, hi = max(1, min(lo, hi)), max(lo, hi)
    if lo == hi:
        return [lo]
    mid = (lo + hi) // 2
    return sorted(set([lo, mid, hi]))


def _p_grid(config):
    """Значения вероятности верного ответа для сканирования (или [None])."""
    track = config.get("blocks", {}).get("track") or {}
    if track.get("move_condition") != "correct_answer":
        return [None]
    pc = track.get("p_correct")
    if isinstance(pc, dict):
        if pc.get("mode") == "known":
            return [float(pc.get("value", 0.7))]
        vals = pc.get("values") or [0.4, 0.7, 0.9]
        return [float(v) for v in vals]
    return [0.7]


def run(config: dict, n_games: int = DEFAULT_GAMES, seed: int = 42) -> dict:
    """Полный прогон. Возвращает структуру для отчёта и графиков."""
    n_games = max(200, min(int(n_games), MAX_GAMES))
    if not config.get("blocks"):
        return {"runnable": False, "reason": "Не отмечено ни одного блока — нечего симулировать."}

    master = random.Random(seed)
    targets = config.get("targets", {})
    player_counts = _player_counts(config)
    p_grid = _p_grid(config)
    scan_p = len(p_grid) > 1 or p_grid[0] is not None

    configs = []
    for npl in player_counts:
        for p in p_grid:
            rng = random.Random(master.randint(0, 10**9))
            results = [engine.simulate_game(config, npl, rng, p_correct=p)
                       for _ in range(n_games)]
            m = metrics.aggregate(results, npl)
            m["p_correct"] = p
            m["findings"] = diagnostics.diagnose(m, targets)
            configs.append(m)

    # сводные флаги по всем конфигурациям (уникальные рецепты)
    seen = set()
    summary_findings = []
    for c in configs:
        for f in c["findings"]:
            key = (f["anomaly"], f["severity"])
            if key not in seen:
                seen.add(key)
                summary_findings.append(f)
    severity_rank = {"critical": 0, "warn": 1, "info": 2}
    summary_findings.sort(key=lambda f: severity_rank.get(f["severity"], 3))

    return {
        "runnable": True,
        "games_per_config": n_games,
        "player_counts": player_counts,
        "scan_p": scan_p,
        "p_grid": [p for p in p_grid if p is not None],
        "configs": configs,
        "summary_findings": summary_findings,
        "blocks_used": sorted(config.get("blocks", {}).keys()),
    }
