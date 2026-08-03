"""Структурированные данные оценки по линзам Шелла (Часть 5 мастер-промпта).

Это машинно-читаемая «спинка» промпта: ядро линз по категориям, веса категорий
и каталог антипаттернов. Текстовый промпт (review/prompts) ведёт диалог и ставит
баллы; эти структуры нужны, чтобы потом программно собирать итог, считать общий
балл по весам и рисовать UI/триаж — без повторного парсинга промпта.

Веса вынесены отдельно (организация может менять их под свою политику).
"""

# Ядро линз под настольную обучающую фин-игру: категория → номера линз.
CORE_LENSES = {
    "education":     {"title": "Замысел и образовательная ценность", "lenses": [1, 3, 5, 17, 97, 98], "weight": 1.5},
    "economy":       {"title": "Экономика и баланс",                 "lenses": [28, 29, 30, 46, 47, 40, 41], "weight": 1.5},
    # Имя категории — КАНОНИЧЕСКОЕ, как в промптах агента линз и синтезатора.
    # Синтезатор сопоставляет категории по имени: прежнее «тест-готовность»
    # не нашлось бы, и категория молча выпала бы из обеих сумм формулы — вместе
    # с находками диагноста сразу по четырём категориям методички (смерть на
    # старте, софтлоки, эксплуатирующие стратегии, компоненты).
    "structure":     {"title": "Структурная целостность и физический интерфейс", "lenses": [13, 20], "weight": 1.25},
    "design":        {"title": "Целостность дизайна и цели",         "lenses": [7, 9, 12, 25, 26, 82], "weight": 1.0},
    "mechanics":     {"title": "Механика и решения игрока",          "lenses": [22, 23, 24, 32, 33, 42, 43], "weight": 1.0},
    "skill_chance":  {"title": "Навык, шанс, напряжение",            "lenses": [27, 31, 34, 35], "weight": 1.0},
    "player":        {"title": "Игрок, доступность, вовлечение",     "lenses": [16, 18, 48, 49, 61, 62], "weight": 1.0},
    "replayability": {"title": "Реиграбельность и нарратив",         "lenses": [4, 6, 65, 69, 70], "weight": 1.0},
    "social":        {"title": "Социальное взаимодействие",          "lenses": [36, 37, 38], "weight": 0.75},
}

# Каталог красных флагов (Приложение D): ключ → привязка к категории и где ловится.
# detect: 'text' | 'sim' | 'playtest' — где флаг можно подтвердить.
ANTIPATTERNS = {
    "ends_too_early":      {"title": "Слишком рано заканчивается", "category": "skill_chance", "detect": ["text", "sim"]},
    "no_planning_horizon": {"title": "Нет горизонта планирования", "category": "mechanics", "detect": ["text"]},
    "no_depth":            {"title": "Нет глубины после освоения", "category": "mechanics", "detect": ["text", "playtest"]},
    "empty_turn":          {"title": "Пустой ход", "category": "mechanics", "detect": ["text", "sim"]},
    "pseudo_choice":       {"title": "Псевдовыбор (иллюзия выбора)", "category": "mechanics", "detect": ["text"]},
    "dominant_strategy":   {"title": "Единственная доминирующая стратегия", "category": "economy", "detect": ["text", "sim"], "severity": "critical"},
    "snowball":            {"title": "Снежный ком / неудержимый лидер", "category": "economy", "detect": ["text", "sim"]},
    "death_spiral":        {"title": "Спираль смерти", "category": "economy", "detect": ["text", "sim"]},
    "too_random":          {"title": "Чрезмерное влияние рандома", "category": "skill_chance", "detect": ["text", "sim"]},
    "no_finale_tension":   {"title": "Нет напряжения в финале", "category": "skill_chance", "detect": ["text", "sim"]},
    "multiplayer_solitaire": {"title": "Мультиплеер-пасьянс (нет взаимодействия)", "category": "social", "detect": ["text"]},
    "quarterbacking":      {"title": "Квотербекинг", "category": "social", "detect": ["playtest"]},
    "kingmaking":          {"title": "Королевотворец", "category": "social", "detect": ["text", "playtest"]},
    "downtime":            {"title": "Простой между ходами", "category": "player", "detect": ["playtest", "sim"]},
    "theme_ignored":       {"title": "Игнорирование темы", "category": "replayability", "detect": ["text"]},
    "broken_economy":      {"title": "Сломанная экономика", "category": "economy", "detect": ["text", "sim"]},
    # специфично для обучающей фин-игры
    "mechanic_not_concept": {"title": "Механика ≠ обучаемая концепция", "category": "education", "detect": ["text"]},
    "rewards_antipattern": {"title": "Награда за антипаттерн поведения", "category": "education", "detect": ["text", "sim"], "severity": "critical"},
    "hidden_dominant_tool": {"title": "Скрытая численная доминанта инструмента", "category": "economy", "detect": ["text", "sim"]},
    "transfer_failure":    {"title": "Непереносимость знания", "category": "education", "detect": ["text", "playtest"]},
    "term_overload":       {"title": "Перегруз терминами на входе", "category": "player", "detect": ["text"]},
    "needs_facilitator":   {"title": "Зависимость от ведущего", "category": "structure", "detect": ["text"]},
}


def overall_score(category_scores: dict) -> dict:
    """Взвешенное среднее баллов категорий (Часть 5.3).

    `category_scores` — {ключ_категории: балл 1..10 или None для N/A}.
    Категории с None (целиком N/A) исключаются из обеих сумм. Округление до 3 знаков.
    """
    weighted_sum = 0.0
    weight_sum = 0.0
    breakdown = []
    for key, meta in CORE_LENSES.items():
        score = category_scores.get(key)
        if score is None:
            breakdown.append({"category": key, "title": meta["title"], "score": None, "weight": meta["weight"]})
            continue
        w = meta["weight"]
        weighted_sum += score * w
        weight_sum += w
        breakdown.append({"category": key, "title": meta["title"], "score": score,
                          "weight": w, "weighted": round(score * w, 3)})
    overall = round(weighted_sum / weight_sum, 3) if weight_sum else None
    return {"overall": overall, "weighted_sum": round(weighted_sum, 3),
            "weight_sum": round(weight_sum, 3), "breakdown": breakdown}
