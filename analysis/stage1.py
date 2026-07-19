"""Этап 1 — оркестратор документного анализа.

Связывает разбор структуры, извлечение значений и (опционально) семантику в один
отчёт. Поддерживает несколько игр в одном файле. Результат — обычные словари/JSON,
готовые к сохранению в БД и отрисовке на фронтенде.
"""

from analysis import docx_parser, structure_check
from analysis.reference import DOC_TYPES, strict_formatting
from analysis.text_utils import leading_number

# Сколько символов текста показывать в превью карточки игры.
PREVIEW_CHARS = 650


def segment(source, doc_type: str) -> dict:
    """Лёгкая сегментация файла на игры — БЕЗ полного анализа.

    Определяет, сколько игр в документе (по повтору эталонной структуры), и для
    каждой готовит краткую карточку: сколько разделов найдено, возможное название
    и превью первого текста. Полный документный анализ здесь НЕ запускается —
    он вызывается позже по кнопке для выбранной игры.
    """
    if doc_type not in DOC_TYPES:
        raise ValueError(f"Неизвестный тип документа: {doc_type!r}")
    structure = DOC_TYPES[doc_type]["structure"]
    total = len(structure)

    paras = docx_parser.parse(source)
    patterns = structure_check.compile_patterns(structure)
    raw = structure_check.segment_games(paras, patterns)

    games = []
    for i, g in enumerate(raw, start=1):
        chunk = paras[g["start"]:g["end"]]

        # возможное название игры: короткая строка прямо перед первым заголовком
        title_hint = None
        if g["start"] > 0:
            prev = paras[g["start"] - 1].text
            if prev and len(prev) <= 90 and structure_check.match_header(prev, patterns) == -1:
                title_hint = prev

        # превью: первые абзацы игры до лимита символов
        preview_parts, used = [], 0
        for p in chunk:
            if not p.text:
                continue
            preview_parts.append(p.text)
            used += len(p.text)
            if used >= PREVIEW_CHARS or len(preview_parts) >= 10:
                break
        preview = " ".join(preview_parts)
        if len(preview) > PREVIEW_CHARS:
            preview = preview[:PREVIEW_CHARS].rsplit(" ", 1)[0] + "…"

        games.append({
            "index": i,
            "found_count": len(g["found"]),
            "total": total,
            "structure_pct": round(len(g["found"]) / total * 100) if total else 0,
            "title_hint": title_hint,
            "preview": preview,
            "paragraphs": sum(1 for p in chunk if p.text),
        })

    return {
        "doc_type": doc_type,
        "doc_type_title": DOC_TYPES[doc_type]["title"],
        "games_count": len(games),
        "games": games,
    }


def game_text_for_agent(source, doc_type: str, game_index: int) -> str:
    """Текст выбранной игры по разделам эталона — вход для ИИ-агентов (этап 2+).

    Не анализ и не оценка — просто сериализация того, что документный этап уже
    прочитал из .docx (заголовок раздела + его содержимое), в текст для промпта.
    Не найденные/пустые разделы помечаются явно, чтобы агент не путал «раздела
    нет в документе» с «раздел есть, но пуст» — хотя для промпта разница не
    критична, а вот для доверия к ответу агента — важна.
    """
    if doc_type not in DOC_TYPES:
        raise ValueError(f"Неизвестный тип документа: {doc_type!r}")
    structure = DOC_TYPES[doc_type]["structure"]

    games_raw, _ = structure_check.analyze_structure(source, structure, doc_type)
    if not (1 <= game_index <= len(games_raw)):
        raise ValueError(f"Игра {game_index} не найдена в документе")
    raw = games_raw[game_index - 1]["_raw"]

    lines = []
    for idx, name in enumerate(structure):
        data = raw.get(idx)
        lines.append(f"### {name}")
        content = data["content"].strip() if data else ""
        lines.append(content if content else "(раздел не найден или пуст в документе)")
        lines.append("")
    return "\n".join(lines).strip()


def analyze(source, doc_type: str, use_semantics: bool = False) -> dict:
    """Документный анализ по ЗАДАННОЙ структуре разделов.

    Проверяет ТОЛЬКО структуру эталона: наличие разделов, порядок, оформление
    (шрифт/полужирность), объём текста между разделами. Никакого поиска по
    ключевым словам, извлечения параметров из текста или семантики — работаем
    исключительно с заданным списком разделов.

    `source`   — путь или файловый объект .docx
    `doc_type` — 'essay' | 'card'
    """
    if doc_type not in DOC_TYPES:
        raise ValueError(f"Неизвестный тип документа: {doc_type!r}")
    structure = DOC_TYPES[doc_type]["structure"]
    strict = strict_formatting(doc_type)

    games_raw, paras = structure_check.analyze_structure(source, structure, doc_type)

    games = []
    for gi, game in enumerate(games_raw, start=1):
        game.pop("_raw", None)
        games.append({
            "index": gi,
            "title": f"Игра {gi}",
            "structure_pct": game["structure_pct"],
            "found_count": game["found_count"],
            "total": game["total"],
            "warnings": game["warnings"],
            "criticals": game["criticals"],
            "can_simulate": game["can_simulate"],
            "clean_count": game["clean_count"],
            "sections": game["sections"],
        })

    # оформление на уровне документа проверяем только в строгом режиме (эссе)
    formatting = _doc_formatting(source, paras, structure) if strict else None

    return {
        "doc_type": doc_type,
        "doc_type_title": DOC_TYPES[doc_type]["title"],
        "strict": strict,
        "games_count": len(games),
        "paragraphs_total": len(paras),
        "games": games,
        "formatting": formatting,
    }


# порог доли абзацев основного текста, выровненных по ширине
_JUSTIFY_OK = 0.6
_ALIGN_JUSTIFY = 3  # WD_ALIGN_PARAGRAPH.JUSTIFY


def _doc_formatting(source, paras, structure) -> dict:
    """Документное оформление для строгой проверки эссе (раздел 4.1 требований)."""
    patterns = structure_check.compile_patterns(structure)
    signal = structure_check.header_signal(paras, patterns)

    body, top_levels, sub_levels = [], set(), set()
    for p in paras:
        idx = structure_check.detect_header(p, patterns, signal)
        if idx == -1:
            body.append(p)
            continue
        depth = len(leading_number(structure[idx])) or 1
        (top_levels if depth == 1 else sub_levels).add(p.heading_level)

    aligned = [p for p in body if p.alignment is not None]
    justify_ratio = (
        round(sum(1 for p in aligned if p.alignment == _ALIGN_JUSTIFY) / len(aligned), 2)
        if aligned else None
    )

    top = {l for l in top_levels if l}
    sub = {l for l in sub_levels if l}
    hierarchy_ok = bool(top) and bool(sub) and max(top) < min(sub)

    issues = []
    if justify_ratio is not None and justify_ratio < _JUSTIFY_OK:
        issues.append(f"основной текст в основном не выровнен по ширине ({int(justify_ratio*100)}%)")
    if not hierarchy_ok:
        issues.append("нет иерархии заголовков (разделы и подпункты не разведены стилями Заголовок 1/2)")

    meta = docx_parser.document_meta(source)
    return {
        "justify_ratio": justify_ratio,
        "hierarchy_ok": hierarchy_ok,
        "margins": meta.get("margins"),
        "issues": issues,
    }
