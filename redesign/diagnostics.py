"""Режим «Диагност»: каталог «аномалия → рецепт» (раздел 7.1 проектного документа).

Детерминированный словарь соответствий без ИИ. На вход — метрики одной
конфигурации (из simulation.metrics) и целевые показатели автора; на выход —
список находок с конкретными рекомендациями по доработке.
"""


def _finding(severity, anomaly, detail, recipe):
    return {"severity": severity, "anomaly": anomaly, "detail": detail, "recipe": recipe}


def diagnose(metrics: dict, targets: dict = None) -> list:
    """Сопоставляет метрики с каталогом аномалий. Возвращает список находок."""
    targets = targets or {}
    findings = []
    n = metrics.get("n_players", 0)
    if not metrics or not n:
        return findings

    # --- deadlock / недостижимая победа ---
    no_winner = metrics.get("no_winner_rate", 0)
    if no_winner > 0.01:
        findings.append(_finding(
            "critical" if no_winner > 0.1 else "warn",
            "Партии без победителя (deadlock)",
            f"{no_winner*100:.1f}% партий упёрлись в лимит раундов без победителя.",
            "Добавьте лимит раундов с принудительным финалом или правило, гарантирующее завершение."))

    # --- ничьи за 1-е место ---
    tie = metrics.get("tie_rate", 0)
    tie_max = targets.get("tie_rate_max")
    threshold = (tie_max / 100.0) if tie_max is not None else 0.10
    if tie > threshold:
        findings.append(_finding(
            "critical" if tie > 0.3 else "warn",
            "Высокая доля ничьих за 1-е место",
            f"{tie*100:.1f}% партий заканчиваются ничьёй"
            + (f" (порог автора {tie_max}%)." if tie_max is not None else "."),
            "Введите тай-брейк: финальный раунд «внезапной смерти» или дополнительный критерий."))

    # --- перекос очерёдности ---
    edge = metrics.get("first_player_edge", 0)
    max_dev = metrics.get("max_seat_deviation", 0)
    tol = targets.get("first_player_tolerance")
    tol_frac = (tol / 100.0) if tol is not None else 0.05
    if max_dev > tol_frac:
        wr = metrics.get("win_rate_by_seat", {})
        worst = max(wr, key=lambda s: abs(wr[s] - metrics["fair_share"])) if wr else 0
        findings.append(_finding(
            "warn",
            "Перекос win rate по позиции хода",
            f"Место {worst} выигрывает {wr.get(worst,0)*100:.1f}% вместо честных "
            f"{metrics['fair_share']*100:.1f}% (отклонение {max_dev*100:.1f} п.п.).",
            "Компенсация очерёдности: бонус последнему игроку или порядок «змейкой»."))

    # --- длина партии (в ходах) vs цель ---
    rounds = metrics.get("rounds", {})
    lr = targets.get("length_rounds")
    if lr and rounds.get("median"):
        med = rounds["median"]
        if med < lr[0] * 0.7:
            findings.append(_finding("warn", "Партия короче целевой",
                f"Медиана ~{med} ходов на игрока против цели {lr[0]}–{lr[1]}.",
                "Удлините трек / число раундов или поднимите порог победы."))
        elif med > lr[1] * 1.4:
            findings.append(_finding("warn", "Партия длиннее целевой",
                f"Медиана ~{med} ходов на игрока против цели {lr[0]}–{lr[1]}.",
                "Укоротите трек / число раундов или снизьте порог победы."))

    # --- исчерпание колоды ---
    deck = metrics.get("deck_exhausted_rate", 0)
    if deck > 0.05:
        findings.append(_finding("warn", "Колода исчерпывается до конца партии",
            f"{deck*100:.1f}% партий упираются в пустую колоду.",
            "Увеличьте колоду до расчётного размера или включите перетасовку сброса."))

    # --- снежный ком (отрыв лидера) ---
    gap = metrics.get("leader_gap", {})
    final = metrics.get("final_metric_by_seat", {})
    if gap.get("median") and final:
        spreads = [abs(v.get("mean", 0)) for v in final.values()]
        avg_level = (sum(spreads) / len(spreads)) if spreads else 0
        # консервативно: отрыв лидера превышает сам средний уровень показателя
        if avg_level > 2 and gap["median"] > avg_level:
            findings.append(_finding("warn", "Возможный «снежный ком»",
                f"Медианный отрыв лидера {gap['median']} превышает средний уровень показателя (~{avg_level:.0f}).",
                "Прогрессивные издержки лидера или догоняющие механики для отстающих."))

    # --- мёртвые/доминирующие действия (только осмысленные выборы игрока) ---
    shares = metrics.get("action_share", {})
    CHOICE_ACTIONS = {"move", "invest"}  # служебные draw/award/skip — не выбор игрока
    choices = {a: s for a, s in shares.items() if a in CHOICE_ACTIONS}
    if len(choices) > 1:
        for name, share in choices.items():
            local = share / (sum(choices.values()) or 1)
            if local < 0.05:
                findings.append(_finding("info", "Почти мёртвое действие",
                    f"Выбор «{name}» используется в {local*100:.1f}% решений.",
                    "Усильте это действие или уберите — оно почти не влияет на игру."))
            elif local > 0.9:
                findings.append(_finding("warn", "Доминирующее действие",
                    f"Выбор «{name}» занимает {local*100:.1f}% решений.",
                    "Дайте жизнеспособные альтернативы остальным действиям."))

    return findings
