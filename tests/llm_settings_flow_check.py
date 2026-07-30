# -*- coding: utf-8 -*-
"""Проверка страницы «Настройки ИИ»: выбор LLM работает из интерфейса.

Главное, что проверяется: выбор на странице РЕАЛЬНО меняет провайдера, к которому
пойдут агенты (а не просто рисует галочку), ключи API в интерфейс не утекают,
проверка связи делает настоящий вызов без кэша, а список моделей берётся у вендора.
"""
import os
import sys
import tempfile

sys.path.insert(0, ".")

_tmp = tempfile.mkdtemp(prefix="finigro-ui-")
os.environ["LLM_SETTINGS_PATH"] = os.path.join(_tmp, "llm_settings.json")
os.environ["LLM_PROVIDER"] = "openrouter"          # значение «установки» по умолчанию
os.environ.pop("LLM_PROVIDER_FORCE", None)         # UI-выбор не должен игнорироваться
os.environ["XAI_API_KEY"] = "xai-secret-must-not-leak"

from review import llm_provider, llm_settings  # noqa: E402
from review.llm_provider import LLMProvider, register  # noqa: E402

probe = {"calls": 0, "use_cache": []}


class ProbeProvider(LLMProvider):
    """Подставной вендор: считает вызовы и отдаёт список моделей без сети."""

    name = "probe"
    TITLE = "Тестовый вендор"
    KEY_ENV = None
    DEFAULT_MODEL = "probe-1"

    def __init__(self, cache=None, **options):
        super().__init__(cache=cache, **options)
        self.model = options.get("model") or self.DEFAULT_MODEL

    def _complete(self, system, user, **opts):
        probe["calls"] += 1
        return "работает"

    def complete(self, system, user, use_cache=True, **opts):
        probe["use_cache"].append(use_cache)
        return super().complete(system, user, use_cache=use_cache, **opts)

    def list_models(self):
        return ["probe-1", "probe-2-turbo"]


register("probe", ProbeProvider)

import app as A  # noqa: E402

llm_settings.clear()
c = A.app.test_client()
checks = {}

# --- 1) страница показывает всех провайдеров и активного ----------------------
html = c.get("/settings/llm").get_data(as_text=True)
checks["страница открылась"] = "Какой ИИ работает в сервисе" in html
for name in ("openrouter", "gemini", "xai", "deepseek", "anthropic", "openai", "groq", "custom"):
    checks[f"в списке есть {name}"] = f'value="{name}"' in html
checks["активен провайдер из .env"] = "OpenRouter" in html and ".env (LLM_PROVIDER)" in html
checks["видно, что у xai ключ есть"] = "ключ есть" in html
checks["ключ API не утёк в HTML"] = "xai-secret-must-not-leak" not in html
checks["показан путь к файлу выбора"] = "llm_settings.json" in html

# --- 2) выбор в интерфейсе реально переключает провайдера ---------------------
r = c.post("/settings/llm", data={"provider": "xai", "model": ""}, follow_redirects=True)
after = r.get_data(as_text=True)
checks["флеш о переключении"] = "переключены на «xAI Grok»" in after
checks["источник решения — интерфейс"] = "выбор в интерфейсе" in after
with A.app.app_context():
    checks["агенты получат xai"] = isinstance(llm_provider.get_provider(),
                                              llm_provider.XAIProvider)
checks["выбор сохранён в файл"] = llm_settings.load().get("provider") == "xai"

# --- 3) закрепление модели ----------------------------------------------------
c.post("/settings/llm", data={"provider": "xai", "model": "grok-4.3"}, follow_redirects=True)
with A.app.app_context():
    prov = llm_provider.get_provider()
    checks["модель закреплена"] = prov.model == "grok-4.3"
    checks["закреплённая модель отключает каскад"] = prov.model_explicit is True

# --- 4) выбор без ключа: предупреждаем, но не блокируем -----------------------
os.environ.pop("DEEPSEEK_API_KEY", None)
warn = c.post("/settings/llm", data={"provider": "deepseek", "model": ""},
              follow_redirects=True).get_data(as_text=True)
checks["предупреждение про ключ"] = "DEEPSEEK_API_KEY" in warn and "не настроен" in warn
checks["выбор всё равно сохранён"] = llm_settings.load().get("provider") == "deepseek"

# --- 5) неизвестный провайдер отбивается -------------------------------------
bad = c.post("/settings/llm", data={"provider": "нетакого"},
             follow_redirects=True).get_data(as_text=True)
checks["неизвестный провайдер отклонён"] = "Неизвестный провайдер" in bad
checks["прошлый выбор не сломан"] = llm_settings.load().get("provider") == "deepseek"

# --- 6) сброс к .env ----------------------------------------------------------
reset = c.post("/settings/llm", data={"action": "reset"}, follow_redirects=True).get_data(as_text=True)
checks["сброс сработал"] = "Выбор сброшен" in reset
checks["файл выбора удалён"] = llm_settings.load() == {}
with A.app.app_context():
    checks["вернулись к .env"] = llm_provider.resolve_provider_name()[0] == "openrouter"

# --- 7) проверка связи: настоящий вызов, кэш отключён ------------------------
c.post("/settings/llm", data={"provider": "probe", "model": ""}, follow_redirects=True)
test_html = c.post("/settings/llm/test", follow_redirects=True).get_data(as_text=True)
checks["проверка связи прошла"] = "Связь есть" in test_html and "работает" in test_html
checks["вызов действительно был"] = probe["calls"] == 1
checks["проверка идёт без кэша"] = probe["use_cache"] == [False]
checks["в ответе видно провайдера и модель"] = "probe" in test_html and "probe-1" in test_html

# --- 8) список моделей приходит от провайдера --------------------------------
data = c.get("/settings/llm/models?provider=probe").get_json()
checks["список моделей отдан"] = data["ok"] and data["models"] == ["probe-1", "probe-2-turbo"]
checks["количество моделей посчитано"] = data["count"] == 2

# ошибка вендора не роняет страницу, а возвращается текстом
ProbeProvider.list_models = lambda self: (_ for _ in ()).throw(RuntimeError("квота кончилась"))
err = c.get("/settings/llm/models?provider=probe").get_json()
checks["ошибка списка обработана"] = (not err["ok"]) and "квота кончилась" in err["error"]

# --- 9) активный провайдер виден в шапке любой страницы ----------------------
dash = c.get("/dashboard").get_data(as_text=True)
checks["провайдер виден в шапке"] = "Тестовый вендор" in dash and "/settings/llm" in dash

llm_settings.clear()

for label, ok in checks.items():
    print(("OK  " if ok else "FAIL") + " | " + label)
assert all(checks.values()), "часть проверок провалилась"
print(f"\nВСЁ ОК ({len(checks)} проверок)")
