"""Извлечение значений игры из текста (раздел 4.3 проектного документа).

Принципиальное отличие от наивного keyword-поиска: мы не считаем слова, а
пытаемся ИЗВЛЕЧЬ конкретные значения. Если значение извлеклось — концепция
действительно описана (а не просто упомянута), и оно сразу предзаполняет
конструктор этапа 2. Если не извлеклось — это замечание автору «сформулируйте
явно», а не «накрутите ключевых слов».

Накрутка ключевиками против такого подхода не работает: «победа» без указания,
кто и при каком условии побеждает, в значение не превратится.
"""

import re

from analysis.text_utils import collapse_spaces

_DASH = r"[-–—−]"


def _sentence_around(text: str, start: int, end: int) -> str:
    """Возвращает предложение, в которое попало совпадение [start:end]."""
    left = max(text.rfind(".", 0, start), text.rfind("!", 0, start),
               text.rfind("?", 0, start), text.rfind(";", 0, start))
    right_candidates = [p for p in (text.find(".", end), text.find("!", end),
                                    text.find("?", end), text.find(";", end)) if p != -1]
    right = min(right_candidates) if right_candidates else len(text)
    snippet = text[left + 1: right].strip()
    return collapse_spaces(snippet)[:240]


def _players(text):
    m = re.search(rf"(\d+)\s*(?:{_DASH}|до)\s*(\d+)\s*(?:игрок|участник|человек|команд)", text, re.I)
    if m:
        return {"min": int(m.group(1)), "max": int(m.group(2)), "raw": m.group(0)}
    m = re.search(r"(\d+)\s*(?:игрок|участник|человек|команд)", text, re.I)
    if m:
        n = int(m.group(1))
        return {"min": n, "max": n, "raw": m.group(0)}
    return None


def _duration(text):
    m = re.search(rf"(\d+)\s*(?:{_DASH}|до)\s*(\d+)\s*(?:мин|час)", text, re.I)
    if m:
        return {"min": int(m.group(1)), "max": int(m.group(2)), "raw": m.group(0)}
    m = re.search(r"(\d+)\s*(?:минут|мин\b|час)", text, re.I)
    if m:
        n = int(m.group(1))
        return {"min": n, "max": n, "raw": m.group(0)}
    return None


def _age(text):
    m = re.search(r"\b(\d{1,2})\s*\+", text)
    if m:
        return {"value": f"{m.group(1)}+", "raw": m.group(0)}
    m = re.search(r"(?:возраст\w*|от)\s*(\d{1,2})\s*(?:лет|год)", text, re.I)
    if m:
        return {"value": f"{m.group(1)}+", "raw": m.group(0)}
    return None


def _dice(text):
    faces = set()
    for m in re.finditer(r"\b[dдD]\s?(\d{1,2})\b", text):
        faces.add(int(m.group(1)))
    for m in re.finditer(r"\bк(\d{1,2})\b", text):
        faces.add(int(m.group(1)))
    result = sorted(f"d{f}" for f in faces)
    if not result and re.search(r"\bкубик|\bкост[ьи]|\bгран[ьи]", text, re.I):
        return {"faces": [], "note": "кубик упомянут, но число граней не извлечено"}
    if result:
        return {"faces": result}
    return None


def _decks(text):
    sizes = []
    for m in re.finditer(r"(\d+)\s*(?:карт|карточ|колод)", text, re.I):
        sizes.append(int(m.group(1)))
    if sizes:
        return {"sizes": sorted(set(sizes), reverse=True), "raw_count": len(sizes)}
    return None


def _rewards(text):
    out = []
    pattern = rf"([+\-−]\s?\d+)\s*(балл\w*|очк\w*|монет\w*|дублон\w*|жетон\w*|рубл\w*|ресурс\w*|жизн\w*|здоров\w*)"
    for m in re.finditer(pattern, text, re.I):
        amount = m.group(1).replace(" ", "").replace("−", "-")
        out.append({"amount": amount, "unit": m.group(2).lower()})
    return out or None


def _win_condition(text):
    m = re.search(
        r"(выигрыва\w+|побежда\w+|победител\w+|выигрывает\s+тот|побеждает\s+(?:тот|игрок|команд)\w*)",
        text, re.I)
    if m:
        return {"raw": _sentence_around(text, m.start(), m.end())}
    return None


def _end_condition(text):
    m = re.search(
        r"(игра\s+(?:заканчива\w+|завершае\w+|оканчива\w+)|конец\s+игры|окончани\w+\s+игры|"
        r"партия\s+(?:заканчива\w+|завершае\w+)|игра\s+длится)",
        text, re.I)
    if m:
        return {"raw": _sentence_around(text, m.start(), m.end())}
    return None


# человекочитаемые названия параметров, которые мы ожидаем найти
_EXPECTED_LABELS = {
    "players": "число игроков",
    "win_condition": "условие победы",
    "end_condition": "условие окончания игры",
    "duration": "длительность партии",
}


def extract_params(full_text: str) -> dict:
    """Извлекает значения из текста игры. Возвращает values / missing / stage2_draft."""
    text = collapse_spaces(full_text or "")

    values = {
        "players": _players(text),
        "duration": _duration(text),
        "age": _age(text),
        "dice": _dice(text),
        "decks": _decks(text),
        "rewards": _rewards(text),
        "win_condition": _win_condition(text),
        "end_condition": _end_condition(text),
    }

    missing = [label for key, label in _EXPECTED_LABELS.items() if not values.get(key)]

    return {
        "values": {k: v for k, v in values.items() if v},
        "missing": missing,
        "stage2_draft": _stage2_draft(values),
    }


def _stage2_draft(values: dict) -> dict:
    """Черновик параметров для конструктора этапа 2 из извлечённых значений."""
    draft = {"blocks": []}
    if values.get("players"):
        draft["players"] = {"min": values["players"]["min"], "max": values["players"]["max"]}
    if values.get("decks"):
        draft["blocks"].append("decks")
        draft["decks"] = {"sizes": values["decks"]["sizes"]}
    if values.get("dice"):
        draft["blocks"].append("track")  # кубик чаще всего у трека/гонки
        draft["dice"] = values["dice"].get("faces", [])
    targets = {}
    if values.get("duration"):
        targets["game_minutes"] = [values["duration"]["min"], values["duration"]["max"]]
    if targets:
        draft["targets"] = targets
    return draft
