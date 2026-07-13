"""Семантическая проверка «о том ли раздел» через локальные эмбеддинги (раздел 4.4).

Текст раздела и эталонное описание его ожидаемого содержания кодируются локальной
моделью (sentence-transformers, напр. rubert-tiny2 / multilingual-MiniLM — CPU,
без внешних API), считается косинусная близость. Низкая близость → флаг «раздел,
возможно, не раскрывает заявленную тему».

Изящная деградация (Принцип «не ломаться»): если sentence-transformers/torch не
установлены, модуль возвращает available=False, а конвейер просто пропускает этот
слой. Включается установкой:  pip install sentence-transformers
"""

import math

from analysis.reference import SECTION_DESCRIPTIONS_ESSAY

# Порог косинусной близости, ниже которого раздел помечается подозрительным.
SIMILARITY_FLAG = 0.30

_model = None
_load_failed = False


def is_available() -> bool:
    """Пытается лениво загрузить модель. False — эмбеддинги недоступны."""
    global _model, _load_failed
    if _model is not None:
        return True
    if _load_failed:
        return False
    try:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
        return True
    except Exception:
        _load_failed = True
        return False


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def check_sections(section_texts_by_index: dict, doc_type: str) -> dict:
    """Близость текста разделов к их эталонным описаниям.

    `section_texts_by_index` — {section_index: text}. Поддержано только эссе
    (для карточки идей эталонные описания пока не заданы).
    Возвращает {"available": bool, "scores": {index: {"similarity","low"}}}.
    """
    if doc_type != "essay" or not is_available():
        return {"available": False, "scores": {}}

    descriptions = SECTION_DESCRIPTIONS_ESSAY
    items = [(idx, txt) for idx, txt in section_texts_by_index.items()
             if idx in descriptions and txt and len(txt) > 30]
    if not items:
        return {"available": True, "scores": {}}

    section_vecs = _model.encode([txt for _, txt in items])
    desc_vecs = _model.encode([descriptions[idx] for idx, _ in items])

    scores = {}
    for (idx, _), sv, dv in zip(items, section_vecs, desc_vecs):
        sim = _cosine(sv, dv)
        scores[idx] = {"similarity": round(sim, 3), "low": sim < SIMILARITY_FLAG}
    return {"available": True, "scores": scores}
