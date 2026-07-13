"""Авторитетная схема конструктора блоков (этап 2, раздел 5.1).

Игра описывается не «типом», а КОМБИНАЦИЕЙ включаемых блоков (Принцип 2). Здесь
схема задана как данные: форма на фронтенде, валидация, предзаполнение и (позже)
движок симуляции читают одну и ту же структуру. Вопросы сформулированы бытовым
языком, без математики (Принцип 3) — автор переносит числа из своих правил.

Типы полей, которые понимает рендерер формы:
  int / float — число (с min/max/шагом и единицей измерения)
  range       — диапазон «от … до …» (два числа)
  select      — выбор из вариантов
  bool        — да/нет
  text        — короткая строка
  grid        — параметр-неизвестный: известное значение ИЛИ сетка для сканирования
                (Принцип 4 — «не спрашиваем, а сканируем»)
  rows        — динамическая таблица/список строк с колонками-подполями
"""

# --- словари вариантов ------------------------------------------------------
DICE_OPTIONS = ["d4", "d6", "d8", "d10", "d12", "d20"]

CELL_EFFECTS = [
    {"value": "none", "label": "нет эффекта"},
    {"value": "forward", "label": "вперёд на N клеток"},
    {"value": "back", "label": "назад на N клеток"},
    {"value": "skip", "label": "пропуск хода"},
    {"value": "extra", "label": "дополнительный ход/бросок"},
    {"value": "gain", "label": "+N ресурса"},
    {"value": "lose", "label": "−N ресурса"},
    {"value": "to_start", "label": "возврат на старт"},
]


# --- глобальные поля (всегда видимы) ----------------------------------------
PLAYERS_FIELD = {
    "key": "players", "label": "Число игроков", "type": "range",
    "min_default": 2, "max_default": 4, "min": 1, "max": 12,
    "help": "Сколько игроков участвует в партии. Симуляция прогонит весь диапазон.",
}

# Условие победы — общий каркас, делает движок универсальным (не привязан к
# архетипу): автор прямо говорит, КАК выигрывают, а не угадывается по блокам.
WIN_FIELD = {
    "key": "win_type", "label": "Как определяется победитель", "type": "select",
    "options": [
        {"value": "auto", "label": "Авто (по включённым блокам)"},
        {"value": "first_to_position", "label": "Первым добраться до финиша (трек)"},
        {"value": "first_to_score", "label": "Первым набрать N очков"},
        {"value": "first_to_resource", "label": "Первым накопить N ресурса"},
        {"value": "max_score_at_end", "label": "Больше всех очков в конце"},
        {"value": "max_resource_at_end", "label": "Больше всех ресурса в конце"},
        {"value": "last_standing", "label": "Последний оставшийся в игре"},
    ], "default": "auto",
    "help": "Главное правило игры. От него зависит, что считает симуляция.",
}
WIN_THRESHOLD_FIELD = {
    "key": "win_threshold", "label": "Порог победы (N очков/ресурса)", "type": "int",
    "default": 10, "min": 1, "max": 99999,
    "help": "Сколько набрать для победы — для вариантов «первым набрать/накопить N».",
}

# Длину партии меряем В ХОДАХ, а не в минутах: в симуляции реальное время
# неизвестно, а число ходов/действий оценить можно.
TARGET_FIELDS = [
    {"key": "length_rounds", "label": "Целевая длина партии (ходов на игрока)", "type": "range",
     "min_default": 15, "max_default": 40, "min": 1, "max": 500,
     "help": "Минуты симуляция не измеряет — считаем ходы. Один «ход» = раунд действий игрока."},
    {"key": "tie_rate_max", "label": "Допустимая доля ничьих за 1-е место, %", "type": "int",
     "default": 10, "min": 0, "max": 100,
     "help": "Выше этого порога ничьи помечаются проблемой (нужен тай-брейк)."},
    {"key": "first_player_tolerance", "label": "Допуск перекоса очередности, ± %", "type": "int",
     "default": 3, "min": 0, "max": 50,
     "help": "На сколько win rate по позиции хода может отклоняться от честной доли."},
]


# --- блоки ------------------------------------------------------------------
BLOCKS = [
    {
        "key": "track", "title": "Трек", "icon": "🏁",
        "desc": "Гонка по дорожке из клеток к финишу.",
        "fields": [
            {"key": "length", "label": "Длина трассы (клеток)", "type": "int",
             "default": 40, "min": 2, "max": 500,
             "help": "Сколько клеток от старта до финиша."},
            {"key": "dice", "label": "Кубик", "type": "select",
             "options": DICE_OPTIONS, "default": "d6"},
            {"key": "move_condition", "label": "Когда игрок двигается", "type": "select",
             "options": [
                 {"value": "always", "label": "Всегда в свой ход"},
                 {"value": "correct_answer", "label": "Только при верном ответе"},
             ], "default": "always"},
            {"key": "p_correct", "label": "Вероятность верного ответа", "type": "grid",
             "default_values": [0.4, 0.7, 0.9], "min": 0.0, "max": 1.0, "step": 0.05,
             "depends": {"move_condition": "correct_answer"},
             "help": "Если объективно неизвестна — сканируем по сетке значений."},
            {"key": "special_cells", "label": "Спецклетки", "type": "rows",
             "row_label": "клетка",
             "columns": [
                 {"key": "cell", "label": "№ клетки", "type": "int", "min": 1, "max": 500},
                 {"key": "effect", "label": "Эффект", "type": "select", "options": CELL_EFFECTS},
                 {"key": "value", "label": "N", "type": "int", "default": 1, "min": 0, "max": 100},
             ]},
        ],
    },
    {
        "key": "resources", "title": "Ресурсы", "icon": "💰",
        "desc": "Экономика игрока: деньги, очки, жизни и т. п.",
        "fields": [
            {"key": "items", "label": "Ресурсы игрока", "type": "rows",
             "row_label": "ресурс",
             "columns": [
                 {"key": "name", "label": "Название", "type": "text", "placeholder": "монеты"},
                 {"key": "start", "label": "Старт", "type": "int", "default": 10, "min": -999, "max": 99999},
                 {"key": "per_turn", "label": "Δ за ход", "type": "int", "default": 0, "min": -999, "max": 999,
                  "help": "Доход (+) или расход (−) за ход без учёта событий."},
                 {"key": "bankrupt_at", "label": "Банкрот при", "type": "int", "default": 0, "min": -999, "max": 99999},
             ]},
            {"key": "on_bankrupt", "label": "При банкротстве", "type": "select",
             "options": [
                 {"value": "eliminated", "label": "Игрок выбывает"},
                 {"value": "continue", "label": "Продолжает (долг)"},
             ], "default": "eliminated"},
        ],
    },
    {
        "key": "decks", "title": "Колоды", "icon": "🃏",
        "desc": "Карты событий / вопросов.",
        "fields": [
            {"key": "items", "label": "Колоды", "type": "rows",
             "row_label": "колода",
             "columns": [
                 {"key": "name", "label": "Название", "type": "text", "placeholder": "вопросы"},
                 {"key": "size", "label": "Размер", "type": "int", "default": 50, "min": 1, "max": 9999},
                 {"key": "hand_size", "label": "Рука", "type": "int", "default": 0, "min": 0, "max": 99},
                 {"key": "draw_per_round", "label": "Добор/раунд", "type": "int", "default": 1, "min": 0, "max": 99},
                 {"key": "refill", "label": "Перетасовка сброса", "type": "bool", "default": True},
             ]},
            {"key": "effect_low", "label": "Мин. эффект карты на ресурс", "type": "int",
             "default": -5, "min": -999, "max": 999},
            {"key": "effect_high", "label": "Макс. эффект карты на ресурс", "type": "int",
             "default": 5, "min": -999, "max": 999},
        ],
    },
    {
        "key": "actions", "title": "Действия игрока", "icon": "🎯",
        "desc": "Что игрок выбирает в свой ход и как это меняет состояние. Делает скелет применимым почти к любой игре.",
        "fields": [
            {"key": "items", "label": "Доступные действия", "type": "rows",
             "row_label": "действие",
             "columns": [
                 {"key": "name", "label": "Действие", "type": "text", "placeholder": "инвестировать"},
                 {"key": "score_delta", "label": "Δ очков", "type": "int", "default": 0, "min": -999, "max": 999},
                 {"key": "resource_delta", "label": "Δ ресурса", "type": "int", "default": 0, "min": -999, "max": 999},
                 {"key": "position_delta", "label": "Δ позиции", "type": "int", "default": 0, "min": -99, "max": 99},
                 {"key": "cost", "label": "Стоимость", "type": "int", "default": 0, "min": 0, "max": 999},
                 {"key": "risk", "label": "Разброс ±", "type": "int", "default": 0, "min": 0, "max": 999},
             ]},
            {"key": "policy", "label": "Как игрок выбирает действие", "type": "select",
             "options": [
                 {"value": "random", "label": "Случайно (нейтральная модель скелета)"},
                 {"value": "greedy_score", "label": "Жадно к очкам"},
                 {"value": "greedy_resource", "label": "Жадно к ресурсу"},
             ], "default": "random",
             "help": "По умолчанию — случайно: проверяем пространство исходов, а не качество бота. "
                     "«Жадно» — чтобы увидеть, что будет, если игроки оптимизируют."},
        ],
    },
    {
        "key": "rounds", "title": "Раунды и роли", "icon": "🔄",
        "desc": "Структура партии: сколько раундов и как сменяются роли.",
        "fields": [
            {"key": "formula", "label": "Число раундов", "type": "select",
             "options": [
                 {"value": "fixed", "label": "Фиксированное"},
                 {"value": "players_times_laps", "label": "Игроки × круги"},
                 {"value": "until_deck_empty", "label": "Пока не кончится колода"},
             ], "default": "players_times_laps"},
            {"key": "fixed_rounds", "label": "Сколько раундов", "type": "int",
             "default": 10, "min": 1, "max": 999, "depends": {"formula": "fixed"}},
            {"key": "laps", "label": "Кругов (каждый игрок — судья N раз)", "type": "int",
             "default": 1, "min": 1, "max": 99, "depends": {"formula": "players_times_laps"}},
            {"key": "role_rotation", "label": "Ротация ролей", "type": "select",
             "options": [
                 {"value": "none", "label": "Нет ролей"},
                 {"value": "clockwise", "label": "По часовой стрелке"},
                 {"value": "random", "label": "Случайно"},
             ], "default": "none"},
        ],
    },
    {
        "key": "judge_vote", "title": "Голосование / судья", "icon": "⚖️",
        "desc": "Социальный выбор: кто получает очко за раунд.",
        "fields": [
            {"key": "scoring_type", "label": "Как начисляется очко", "type": "select",
             "options": [
                 {"value": "judge_pick", "label": "Судья выбирает одного игрока"},
                 {"value": "vote_distribution", "label": "Голоса распределяются"},
                 {"value": "dice_roll", "label": "По броску кубика"},
             ], "default": "judge_pick"},
            {"key": "points_per_round", "label": "Очков за раунд", "type": "int",
             "default": 1, "min": 1, "max": 99},
            {"key": "judge_model", "label": "Модель выбора судьи (допущение сервиса)", "type": "select",
             "options": [
                 {"value": "uniform_random", "label": "Равновероятно (честно)"},
                 {"value": "anti_leader", "label": "Реже выбирает лидера"},
             ], "default": "uniform_random",
             "help": "Вкус судьи не симулируется — это декларируемое допущение (Принцип 3)."},
        ],
    },
    {
        "key": "timer", "title": "Таймер / угроза", "icon": "⏳",
        "desc": "Внешнее давление: обвал, погоня, таймер.",
        "fields": [
            {"key": "threat_start", "label": "Стартовый запас (уровней/клеток)", "type": "int",
             "default": 10, "min": 1, "max": 999},
            {"key": "threat_speed", "label": "Скорость угрозы за раунд", "type": "int",
             "default": 1, "min": 1, "max": 99,
             "help": "На сколько угроза приближается каждый раунд."},
            {"key": "on_catch", "label": "Когда угроза настигает", "type": "select",
             "options": [
                 {"value": "game_over", "label": "Конец игры для всех"},
                 {"value": "eliminate", "label": "Выбывает отставший"},
                 {"value": "lose", "label": "Штраф ресурса"},
             ], "default": "game_over"},
        ],
    },
    {
        "key": "characters", "title": "Персонажи", "icon": "🎭",
        "desc": "Асимметричные старты (разные стартовые значения у ролей).",
        "fields": [
            {"key": "items", "label": "Персонажи", "type": "rows",
             "row_label": "персонаж",
             "columns": [
                 {"key": "name", "label": "Имя/роль", "type": "text", "placeholder": "Банкир"},
                 {"key": "start_resource", "label": "Старт. ресурс", "type": "int", "default": 10, "min": -999, "max": 99999},
                 {"key": "start_speed", "label": "Бонус скорости", "type": "int", "default": 0, "min": -99, "max": 99},
             ]},
        ],
    },
]

BLOCK_BY_KEY = {b["key"]: b for b in BLOCKS}
BLOCK_KEYS = [b["key"] for b in BLOCKS]
