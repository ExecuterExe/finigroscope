# -*- coding: utf-8 -*-
"""Живой список моделей любого провайдера: python tests/llm_list_models.py <имя>

Зачем: имена моделей у вендоров меняются (и снимаются) регулярно, поэтому
константа в коде — всегда лишь разумное умолчание. Этот скрипт спрашивает
список у самого вендора и заодно показывает, какие провайдеры настроены.

Примеры:
    python tests/llm_list_models.py            # что вообще есть и что настроено
    python tests/llm_list_models.py xai
    python tests/llm_list_models.py openrouter --all   # не только бесплатные
"""
import sys

sys.path.insert(0, ".")

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from review import llm_provider  # noqa: E402


def show_catalog():
    # Без эмодзи: консоль Windows работает в cp1251, и любой символ вне этой
    # кодировки валит скрипт с UnicodeEncodeError. Кириллица проходит, эмодзи нет.
    print("Провайдеры в реестре:\n")
    for p in llm_provider.provider_catalog():
        if not p["requires"]:
            mark = "[-]"                 # заглушке null настраивать нечего
        else:
            mark = "[настроен]" if p["configured"] else "[не настроен]"
        key = f" (нужен {p['requires']})" if p["requires"] else ""
        print(f"  {p['name']:<11} {mark:<14} {p['title']}{key}")
        if p["default_model"]:
            print(f"  {'':<11} модель по умолчанию: {p['default_model']}")
    print("\nСписок моделей: python tests/llm_list_models.py <имя провайдера>")


def show_models(name, free_only):
    if name not in llm_provider.available_providers():
        print(f"Неизвестный провайдер «{name}». Доступные: "
              f"{', '.join(llm_provider.available_providers())}")
        return 1

    provider = llm_provider.get_provider(name)
    lister = getattr(provider, "list_models", None)
    if lister is None:
        print(f"Провайдер «{name}» не умеет отдавать список моделей.")
        return 1

    print(f"Спрашиваем список моделей у «{provider.TITLE}»…\n")
    try:
        # free_only поддерживает только OpenRouter — у остальных этого фильтра нет.
        models = lister(free_only=free_only) if name == "openrouter" else lister()
    except TypeError:
        models = lister()
    except Exception as exc:
        print(f"Не получилось: {exc}")
        return 1

    for m in models:
        mark = "  <- модель по умолчанию" if m == provider.DEFAULT_MODEL else ""
        print(f"  {m}{mark}")
    print(f"\nВсего: {len(models)}")
    if provider.DEFAULT_MODEL:
        print(f"Модель по умолчанию в коде: {provider.DEFAULT_MODEL}")
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    free_only = "--all" not in sys.argv
    if not args:
        show_catalog()
        sys.exit(0)
    sys.exit(show_models(args[0].lower(), free_only))
