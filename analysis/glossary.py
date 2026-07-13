"""Детерминированный слой этапа 3: словарь финтерминов и читабельность (раздел 6.1).

Без токенов и внешних API. Две метрики:
  • покрытие словаря финансовой грамотности — какие финансовые темы реально
    затронуты в тексте (по корням-леммам, без pymorphy);
  • читабельность русского текста (адаптация Флеша/Флеша-Кинкейда по Оборневой)
    против заявленного возраста аудитории.

Это вход и для отчёта, и для промпта ИИ-рецензента (этап 3 комментирует факты).
"""

import re

from analysis.text_utils import collapse_spaces

_VOWELS = set("аеёиоуыэюя")
_WORD_RE = re.compile(r"[а-яёa-z]+", re.IGNORECASE)
_SENT_RE = re.compile(r"[.!?…]+")

# Корни финансовых терминов (стем-сопоставление по началу слова заменяет
# лемматизацию: «диверсификация/диверсифицировать» → один корень). Список —
# стартовый, расширяется по реальным играм сезона.
FINANCIAL_ROOTS = {
    "бюджет", "доход", "расход", "сбереж", "накопл", "кредит", "займ", "ссуд",
    "депозит", "вклад", "процент", "ставк", "инфляци", "дефляци", "налог", "ндс",
    "акциз", "пошлин", "субсиди", "дотаци", "пенси", "страхов", "ипотек", "лизинг",
    "факторинг", "инвестиц", "инвестор", "актив", "пассив", "капитал", "акци",
    "облигаци", "купон", "дивиденд", "брокер", "биржа", "котировк", "портфел",
    "диверсификаци", "ликвидн", "доходн", "рентабельн", "валют", "курс", "паритет",
    "хедж", "деривати", "фьючерс", "опцион", "своп", "эмисси", "номинал", "дефолт",
    "банкрот", "залог", "поручительств", "аннуитет", "рефинансир", "реструктуризац",
    "комисси", "тариф", "баланс", "выручк", "прибыл", "убыт", "издержк",
    "себестоим", "маржа", "оборот", "фонд", "резерв", "транзакц", "эквайринг",
    "кэшбэк", "овердрафт", "лимит", "долг", "обязательств", "монет", "купюр",
    "наличн", "безналичн", "перевод", "платёж", "платеж", "счёт-фактур",
    "финанс", "экономи", "деньг", "стоимост", "цен", "скидк", "наценк",
    "ритейл", "поставщик", "спрос", "предложен", "конкуренц", "монопол",
}

# Гласные на слог считаем грубо: одна гласная ~ один слог.
def _syllables(word: str) -> int:
    return sum(1 for ch in word.lower() if ch in _VOWELS) or 1


def term_coverage(text: str) -> dict:
    """Какие финансовые корни встретились в тексте. Возвращает found/count/density."""
    words = _WORD_RE.findall((text or "").lower())
    total = len(words) or 1
    found = {}
    for w in words:
        for root in FINANCIAL_ROOTS:
            if w.startswith(root):
                found[root] = found.get(root, 0) + 1
                break
    return {
        "found": sorted(found.keys()),
        "distinct_count": len(found),
        "total_hits": sum(found.values()),
        "density_per_1000": round(sum(found.values()) / total * 1000, 1),
        "vocab_size": len(FINANCIAL_ROOTS),
        "coverage_pct": round(len(found) / len(FINANCIAL_ROOTS) * 100, 1),
    }


def readability(text: str, target_age: int = 12) -> dict:
    """Метрики читабельности русского текста и сравнение с целевым возрастом.

    Использует адаптацию формул Флеша для русского (И. Оборнева):
      FRE  = 206.835 − 1.3·(слов/предлож.) − 60.1·(слогов/слово)   (выше — легче)
      grade = 0.5·(слов/предлож.) + 8.4·(слогов/слово) − 15.59      (класс школы)
    """
    text = collapse_spaces(text or "")
    words = _WORD_RE.findall(text)
    n_words = len(words)
    if n_words == 0:
        return {"words": 0, "available": False}

    n_sent = len([s for s in _SENT_RE.split(text) if s.strip()]) or 1
    n_syll = sum(_syllables(w) for w in words)

    words_per_sent = n_words / n_sent
    syll_per_word = n_syll / n_words

    fre = 206.835 - 1.3 * words_per_sent - 60.1 * syll_per_word
    grade = 0.5 * words_per_sent + 8.4 * syll_per_word - 15.59
    grade = max(1.0, grade)
    est_age = round(grade + 6)

    return {
        "available": True,
        "words": n_words,
        "sentences": n_sent,
        "avg_sentence_len": round(words_per_sent, 1),
        "avg_word_syllables": round(syll_per_word, 2),
        "flesch_reading_ease": round(fre, 1),
        "estimated_grade": round(grade, 1),
        "estimated_age": est_age,
        "target_age": target_age,
        "too_complex": est_age > target_age + 1,
    }


def analyze_content(text: str, target_age: int = 12) -> dict:
    """Сводка детерминированного слоя этапа 3 по тексту игры."""
    return {
        "glossary": term_coverage(text),
        "readability": readability(text, target_age=target_age),
    }
