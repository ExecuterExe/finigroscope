"""Проверка структуры документа: разделы, порядок, оформление, наполненность.

Ключевые свойства:
  • Сопоставление заголовков с эталоном идёт по нормализованному тексту с
    необязательным номером и необязательным «(хвостом в скобках)» — устойчиво
    к мелким расхождениям оформления.
  • Один файл может содержать НЕСКОЛЬКО игр: эталонная структура повторяется
    N раз. Движок режет документ на отдельные игры по «откату назад» в нумерации
    разделов (встретили раздел с индексом ≤ последнего — началась новая игра).
  • Оформление (полужирность, шрифт) и наполненность (объём текста) проверяются
    по эффективным значениям из docx_parser (с учётом наследования от стиля).
"""

import re

from analysis import docx_parser
from analysis.reference import (
    SECTION_MIN_CHARS,
    critical_indices,
    parent_flags,
    strict_formatting,
)
from analysis.text_utils import normalize

# Разрешённые шрифты по требованиям оформления (строгий режим — эссе).
ALLOWED_FONTS = ("Arial Narrow", "Times New Roman")
# Строки длиннее этого заголовком не считаем (защита от ложных срабатываний).
MAX_HEADER_LEN = 180
# Фразы-заглушки, оставшиеся от шаблона (содержания по сути нет).
PLACEHOLDER_MARKERS = (
    "опишите здесь", "введите текст", "ваш текст", "напишите здесь",
    "текст сюда", "укажите здесь", "заполните",
)


# --- сопоставление заголовков ----------------------------------------------
def _build_pattern(title: str):
    """Строит regex для заголовка: номер опционален, хвост в скобках опционален."""
    norm = normalize(title)
    m = re.match(r"^(\d+(?:\.\d+)*)\.?\s+(.*)$", norm)
    if m:
        number, rest = m.group(1), m.group(2)
    else:
        number, rest = None, norm

    paren = re.match(r"^(.*?)\s*(\(.*\))\s*$", rest)
    if paren:
        main_re = re.escape(paren.group(1).strip()).replace(r"\ ", r"\s+")
        tail_re = re.escape(paren.group(2).strip()).replace(r"\ ", r"\s+")
        body_re = f"{main_re}(?:\\s+{tail_re})?"
    else:
        body_re = re.escape(rest).replace(r"\ ", r"\s+")

    if number:
        num_re = "(?:" + re.escape(number) + r"\.?\s+)?"
    else:
        num_re = ""
    return re.compile("^" + num_re + body_re)


def compile_patterns(structure):
    return [_build_pattern(h) for h in structure]


def match_header(line: str, patterns) -> int:
    """Индекс эталонного раздела, которому соответствует строка, или -1.

    Если совпало несколько шаблонов — берём с самым длинным совпадением
    (самый специфичный заголовок).
    """
    return _match_cov(line, patterns)[0]


def _match_cov(line, patterns):
    """(индекс, покрытие) — какую долю строки занял матч заголовка."""
    if len(line) > MAX_HEADER_LEN:
        return -1, 0.0
    norm = normalize(line)
    best_idx, best_len = -1, -1
    for i, pat in enumerate(patterns):
        m = pat.match(norm)
        if m and m.end() > best_len:
            best_idx, best_len = i, m.end()
    cov = best_len / max(1, len(norm)) if best_idx != -1 else 0.0
    return best_idx, cov


# Минимальное покрытие строки матчем, чтобы считать абзац заголовком по одному
# только тексту (отсекает тело, где заголовочная фраза лишь начинает абзац).
_HEADER_COV = 0.7


def header_signal(paras, patterns):
    """Каким признаком в этом документе размечены заголовки: style / bold / cov.

    Требование 006 предписывает стили; но многие оформляют заголовки просто
    полужирным. Выбираем сигнал по факту: если совпавшие заголовки размечены
    стилем — 'style'; иначе если полужирным — 'bold'; иначе опираемся только на
    покрытие строки — 'cov'.
    """
    styled = bold = False
    for p in paras:
        if _match_cov(p.text, patterns)[0] == -1:
            continue
        if p.is_heading_style:
            styled = True
            break
        if p.bold:
            bold = True
    if styled:
        return "style"
    return "bold" if bold else "cov"


def detect_header(para, patterns, signal="cov"):
    """Индекс раздела для абзаца-заголовка или -1 (с учётом сигнала разметки).

    Заголовок принимается, если абзац матчит эталон И размечен «как заголовок»
    по сигналу документа (стиль/полужирный), ЛИБО матч покрывает почти всю
    строку. Так реальные заголовки ловятся и в стилевых, и в «жирных» шаблонах,
    а тело (не размеченное, с фразой в начале длинного абзаца) — нет.
    """
    idx, cov = _match_cov(para.text, patterns)
    if idx == -1:
        return -1
    if cov >= _HEADER_COV:
        return idx
    if signal == "style" and para.is_heading_style:
        return idx
    if signal == "bold" and para.bold:
        return idx
    return -1


# --- нарезка на игры --------------------------------------------------------
def split_games(paras, patterns):
    """Режет абзацы на отдельные игры. Возвращает список игр.

    Каждая игра — словарь {section_index: {...}} с накопленным содержимым.
    Новая игра начинается, когда встречен раздел с индексом ≤ последнего
    (откат нумерации назад = повтор структуры).
    """
    games = []
    current = None
    last_idx = -1
    last_section = None  # индекс раздела, в который сейчас льётся текст
    signal = header_signal(paras, patterns)

    def new_game():
        nonlocal current, last_idx, last_section
        current = {}
        last_idx = -1
        last_section = None
        games.append(current)

    for para in paras:
        idx = detect_header(para, patterns, signal)
        if idx != -1:
            if current is None or idx <= last_idx:
                new_game()
            current[idx] = {
                "found": True,
                "header_para": para,
                "char_count": 0,
                "content": "",
            }
            last_idx = idx
            last_section = idx
        elif current is not None and last_section is not None:
            sect = current[last_section]
            sect["char_count"] += len(para.text)
            sect["content"] += " " + para.text

    return games


def segment_games(paras, patterns):
    """Только границы игр в документе (для сегментации без полного анализа).

    Возвращает список игр: {"found": set(индексов разделов), "start": позиция
    первого заголовка игры в `paras`, "end": позиция начала следующей игры}.
    Новая игра — при откате нумерации назад (idx <= последнего).
    """
    games = []
    current = None
    last_idx = -1
    signal = header_signal(paras, patterns)
    for pos, para in enumerate(paras):
        idx = detect_header(para, patterns, signal)
        if idx == -1:
            continue
        if current is None or idx <= last_idx:
            if games:
                games[-1]["end"] = pos
            current = {"found": set(), "start": pos, "end": len(paras)}
            games.append(current)
            last_idx = -1
        current["found"].add(idx)
        last_idx = idx
    return games


# --- проверки одной игры ----------------------------------------------------
def _format_errors(para):
    errors = []
    if not para.bold:
        errors.append("не полужирный")
    font = para.font_name
    if font is None:
        errors.append("шрифт не определён")
    elif font not in ALLOWED_FONTS:
        errors.append(f"шрифт «{font}» вместо Arial Narrow / Times New Roman")
    return errors


def _content_issue(content: str):
    """Проблема наполненности раздела: empty / placeholder / short / None."""
    stripped = (content or "").strip()
    core = stripped.strip("-—–…·•*.,:;() ").strip()
    if not core:
        return "empty"
    if any(m in stripped.lower() for m in PLACEHOLDER_MARKERS):
        return "placeholder"
    if len(stripped) < SECTION_MIN_CHARS:
        return "short"
    return None


_ISSUE_LABEL = {
    "empty": "раздел пуст",
    "placeholder": "осталась заглушка из шаблона",
    "short": "слишком мало текста",
}


def evaluate_game(game, structure, parents, doc_type):
    """Per-section отчёт по одной игре с учётом режима (мягкий/строгий).

    Мягкий режим (карточка): проверяем только наличие и наполненность, оформление
    не трогаем. Строгий (эссе): добавляем проверку оформления заголовков. В обоих
    режимах разделы, критичные для симуляции, при отсутствии/пустоте помечаются
    'critical' (жёсткий стоп) и роняют флаг can_simulate.
    """
    strict = strict_formatting(doc_type)
    critical = critical_indices(doc_type)
    sections = []
    found = warnings = criticals = 0
    can_simulate = True

    for idx, name in enumerate(structure):
        data = game.get(idx)
        is_critical = idx in critical

        if not data:
            if is_critical:
                can_simulate = False
                criticals += 1
            sections.append({
                "name": name, "found": False, "critical": is_critical,
                "status": "critical" if is_critical else "missing",
                "format_errors": [], "char_count": 0, "content_issue": "empty",
                "style_applied": None,
            })
            continue

        found += 1
        para = data["header_para"]

        issue = None if parents[idx] else _content_issue(data["content"])

        fmt_errors = []
        if strict:
            fmt_errors = _format_errors(para)
            if not para.is_heading_style:
                fmt_errors = fmt_errors + ["не оформлен стилем заголовка"]

        not_filled = issue is not None
        if is_critical and not_filled:
            status = "critical"
            can_simulate = False
            criticals += 1
        elif not_filled or fmt_errors:
            status = "warn"
            warnings += 1
        else:
            status = "ok"

        sections.append({
            "name": name, "found": True, "critical": is_critical, "status": status,
            "format_errors": fmt_errors, "char_count": data["char_count"],
            "content_issue": issue, "content_issue_label": _ISSUE_LABEL.get(issue),
            "style_applied": para.is_heading_style,
        })

    total = len(structure)
    return {
        "sections": sections,
        "found_count": found,
        "total": total,
        "warnings": warnings,
        "criticals": criticals,
        "can_simulate": can_simulate,
        "structure_pct": round(found / total * 100, 1) if total else 0.0,
        "clean_count": sum(1 for s in sections if s["status"] == "ok"),
    }


def game_full_text(game) -> str:
    """Весь текст игры (заголовки + содержимое) — вход для извлечения значений."""
    parts = []
    for data in game.values():
        parts.append(data["header_para"].text)
        if data["content"]:
            parts.append(data["content"])
    return " ".join(parts)


def section_texts(game, structure) -> dict:
    """Сопоставление имени эталонного раздела → его текст в этой игре."""
    out = {}
    for idx, data in game.items():
        out[structure[idx]] = data["content"].strip()
    return out


def analyze_structure(source, structure, doc_type):
    """Полный структурный разбор документа: список игр с результатами.

    `source` — путь или файловый объект .docx. Возвращает (games_report, paras).
    """
    paras = docx_parser.parse(source)
    patterns = compile_patterns(structure)
    parents = parent_flags(structure)
    raw_games = split_games(paras, patterns)

    games = []
    for raw in raw_games:
        report = evaluate_game(raw, structure, parents, doc_type)
        report["_raw"] = raw
        games.append(report)
    return games, paras
