# -*- coding: utf-8 -*-
"""Проверка сегментации на реальных файлах пользователя."""
import sys
sys.path.insert(0, ".")
from analysis import stage1

FILES = [
    ("essay", r"C:\Users\Eugene\Desktop\НСПК\Фин-игры\Фин-игры эссе - версия от 1 февраля.docx"),
    ("card",  r"C:\Users\Eugene\Desktop\НСПК\Фин-игры\Шаблон_карточки фин-игры.docx"),
]

for doc_type, path in FILES:
    print("=" * 70)
    print(f"ФАЙЛ: {path}")
    print(f"как {doc_type}")
    try:
        rep = stage1.segment(path, doc_type)
    except Exception as e:
        print("  ОШИБКА:", repr(e))
        continue
    print(f"  Тип: {rep['doc_type_title']} | игр найдено: {rep['games_count']}")
    for g in rep["games"]:
        print(f"  --- Игра {g['index']}: разделов {g['found_count']}/{g['total']} "
              f"({g['structure_pct']}%), абзацев {g['paragraphs']}, title_hint={g['title_hint']!r}")
        print("      превью:", g["preview"][:200])
    print()
