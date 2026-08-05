"""Прогон этапа 1 на синтетическом документе с печатью отчёта в консоль."""

import sys
sys.path.insert(0, ".")

from analysis import stage1
from tests.make_sample import build_sample

ICON = {"ok": "✅", "warn": "⚠️", "missing": "❌"}


def main():
    report = stage1.analyze(build_sample(), "essay", use_semantics=False)
    print(f"Тип: {report['doc_type_title']} | абзацев: {report['paragraphs_total']} | "
          f"игр в файле: {report['games_count']}\n")

    for game in report["games"]:
        print(f"=== {game['title']} === структура {game['structure_pct']}% "
              f"({game['found_count']}/{game['total']}), предупреждений: {game['warnings']}")
        for s in game["sections"]:
            line = f"  {ICON[s['status']]} {s['name']}"
            extra = []
            if s["found"]:
                extra.append(f"{s['char_count']} симв.")
            if s["format_errors"]:
                extra.append("; ".join(s["format_errors"]))
            if extra:
                line += "  — " + " | ".join(extra)
            print(line)

        p = game["params"]
        print("  Извлечено:")
        for key, val in p["values"].items():
            print(f"    • {key}: {val}")
        if p["missing"]:
            print("  Не извлечено (сформулируйте явно):", ", ".join(p["missing"]))
        print("  Черновик параметров этапа 2:", p["stage2_draft"])
        print()


if __name__ == "__main__":
    main()
