# -*- coding: utf-8 -*-
"""Безэкранный симуляционный этап: цепочка целиком по готовому game_spec.

Обращений к моделям нет — все семь агентов подменены заглушками. Проверяется
то, за что отвечает оркестратор, а не они: порядок шагов, повтор круга после
авто-редизайна, выбор лучшего результата и — отдельно — что прогон чужого кода
без разрешения не происходит.

Последнее важнее остального. Прогон скелета выполняет код, написанный моделью,
с правами сервиса. В интерфейсе он закрыт нажатием человека; здесь нажимать
некому, и единственная защита — разрешение, выключенное по умолчанию.
"""
import os
import sys

sys.path.insert(0, ".")
os.environ["LLM_PROVIDER_FORCE"] = "mock"

from models import RedesignAttempt  # noqa: E402
from review import headless  # noqa: E402

checks = {}

SPEC = {
    "game_spec": {
        "core": {
            "players": {"min": 2, "max": 4},
            "mode": "cooperative",
            "elimination": False,
            "turn": {"order": "clockwise", "actions": ["explore", "collect"]},
            "randomness": [{"type": "card_draw"}],
            "resources": [{"name": "keys", "scope": "shared", "start": 0}],
            "win_condition": {"type": "threshold", "metric": "keys", "threshold": 3},
            "loss_condition": {"type": "deck_exhausted"},
            "catch_up": {"enabled": True, "mechanism": "общая копилка"},
            "play_time": None,
            "limits": {"max_rounds": 12},
        },
        "text": {"concept": "Кооперативный поиск.", "components": []},
    },
    "diagnostic_meta": {"actions_resolution": {"explore": "probabilistic"}},
}

STATS = [{"players": 2, "games": 1000, "win_rate": 0.51}]


class Calls(object):
    """Считает вызовы агентов и отдаёт заготовленные ответы."""

    def __init__(self, scores, changes=True):
        self.scores = list(scores)
        self.changes = changes
        self.n = {"skeleton": 0, "run": 0, "balance": 0, "diagnost": 0,
                  "lenses": 0, "synthesis": 0, "redesign": 0, "extra": 0}

    def install(self, module):
        agents = module

        def skeleton(core, diagnostic_meta=None, canonical_text=None, **kw):
            self.n["skeleton"] += 1
            return {"available": True, "simulatable": True,
                    "code": "print('skeleton')",
                    "meta": {"assumptions": [], "subjective_actions": []}}

        def run_skeleton(code, timeout=300):
            self.n["run"] += 1
            return {"ok": True, "stats": STATS, "diag": {"ties": 0}}

        def extra_runs(code, requests, timeout=300):
            self.n["extra"] += 1
            return {"done": len(requests)}

        def balance(core, stats, **kw):
            self.n["balance"] += 1
            return {"available": True,
                    "report": {"findings": [], "notes_for_lenses": ["заметка"]}}

        def build_input(*args, **kw):
            return {"stub": True}

        def triage(data, **kw):
            self.n["diagnost"] += 1
            return {"available": True, "triage": {"tests": []},
                    "kept_requests": [{"id": 1}], "dropped_requests": []}

        def findings(data, tri, extra_runs=None, dropped=None, **kw):
            return {"available": True,
                    "findings": {"findings": [], "notes_for_lenses": [],
                                 "coverage_summary": {"ok": 40}}}

        def lenses(data, **kw):
            self.n["lenses"] += 1
            return {"available": True, "report": {"categories": [], "issues": []}}

        def synthesis(data, **kw):
            self.n["synthesis"] += 1
            score = self.scores.pop(0) if len(self.scores) > 1 else self.scores[0]
            return {"available": True,
                    "report": {"overall_score": score, "top_priorities": ["чинить"]},
                    "reference": {"overall_score": score}}

        def should_trigger(finding_balance, synthesis=None):
            return {"trigger": True, "reason": "критичные флаги"}

        def redesign(spec_root, finding_balance, attempt_number=1, **kw):
            self.n["redesign"] += 1
            return {"available": True,
                    "result": {"changes": [{"field": "x"}] if self.changes else [],
                               "not_touched": ["y"],
                               "handed_to_recommendations": ["z"]}}

        def apply_to_spec(spec_root, result):
            out = json_copy(spec_root)
            out["game_spec"]["core"]["limits"]["max_rounds"] += 1
            return out

        agents.simulationist_agent.run = skeleton
        agents.sim_runner.run_skeleton = run_skeleton
        agents.sim_runner.run_extra_runs = extra_runs
        agents.stats_agent.run = balance
        agents.diagnost_agent.build_input = build_input
        agents.diagnost_agent.run_triage = triage
        agents.diagnost_agent.run_findings = findings
        agents.lens_agent.build_input = build_input
        agents.lens_agent.run = lenses
        agents.synth_agent.build_input = build_input
        agents.synth_agent.run = synthesis
        agents.redesign_agent.should_trigger = should_trigger
        agents.redesign_agent.run = redesign
        agents.redesign_agent.apply_to_spec = apply_to_spec
        agents.extractor_agent.subjective_actions = lambda root: []
        return self


def json_copy(value):
    import json
    return json.loads(json.dumps(value, ensure_ascii=False))


class Progress(object):
    def __init__(self):
        self.steps = []

    def say(self, step, detail=None):
        self.steps.append(step)

    def check_cancelled(self):
        return None


def fresh(scores, changes=True):
    """Свежий модуль с подменёнными агентами: правки не текут между проверками."""
    import importlib
    module = importlib.reload(headless)
    Calls(scores, changes).install(module)
    return module


# --------------------------------------------------------------------------
# Разрешение на выполнение чужого кода
# --------------------------------------------------------------------------

module = fresh([8.0])
counter = Calls([8.0])
counter.install(module)
try:
    module.run(SPEC, allow_code_run=False)
    checks["без разрешения код не выполняется"] = False
except module.HeadlessError as error:
    checks["без разрешения код не выполняется"] = True
    checks["названа переменная разрешения"] = "SIM_API_ALLOW_RUN" in str(error)
    checks["объяснено, почему это закрыто"] = "написанный моделью" in str(error)

# И отказ обязан случиться ДО первого обращения к модели. Пока проверка стояла
# внутри run_skeleton, симуляционист успевал отработать и выставить счёт за код,
# который никто не запустит: без прогона нет статистики, а значит и всей
# остальной цепочки.
checks["без разрешения модель не зовут вовсе"] = counter.n["skeleton"] == 0
checks["и денег не тратят ни на одном агенте"] = sum(counter.n.values()) == 0

# Вырожденное число кругов — понятный отказ, а не падение на max() пустого
# списка где-то в глубине.
module = fresh([8.0])
try:
    module.run(SPEC, allow_code_run=True, max_redesign=0)
    checks["ноль кругов отвергается"] = False
except module.HeadlessError as error:
    checks["ноль кругов отвергается"] = "не меньше одного" in str(error)

# Умолчание проверяем у самой сигнатуры: забытый аргумент на вызывающей стороне
# не должен незаметно включать выполнение чужого кода.
import inspect  # noqa: E402

checks["по умолчанию разрешения нет"] = (
    inspect.signature(headless.run).parameters["allow_code_run"].default is False)

# --------------------------------------------------------------------------
# Успешный круг
# --------------------------------------------------------------------------

module = fresh([8.0])
counter = Calls([8.0])
counter.install(module)
progress = Progress()
out = module.run(SPEC, progress=progress, allow_code_run=True)

checks["порог взят с первого круга"] = out["passed"] is True
checks["кругов сделан один"] = out["rounds_made"] == 1
checks["балл отдан"] = out["score"] == 8.0
checks["агенты позваны по разу"] = (counter.n["skeleton"] == 1
                                    and counter.n["synthesis"] == 1)
checks["авто-редизайн не звался"] = counter.n["redesign"] == 0
checks["шаги названы по порядку"] = progress.steps == [
    "сборка скелета", "прогон скелета", "оценка баланса", "диагност",
    "линзы Шелла", "синтез"]
checks["вердикт говорит о приёмке"] = "принята" in out["verdict"]
checks["в отчёте есть все части круга"] = all(
    out["best"].get(k) is not None
    for k in ("skeleton", "stats", "balance", "diagnost", "lenses", "synthesis"))

# --------------------------------------------------------------------------
# Круг с авто-редизайном
# --------------------------------------------------------------------------

module = fresh([4.0, 7.5])
counter = Calls([4.0, 7.5])
counter.install(module)
out = module.run(SPEC, allow_code_run=True)

checks["низкий балл запускает редизайн"] = counter.n["redesign"] == 1
checks["после правки круг повторён"] = counter.n["skeleton"] == 2
checks["второй круг взял порог"] = out["passed"] is True
checks["кругов сделано два"] = out["rounds_made"] == 2
checks["правка применена к спецификации"] = (
    out["spec"]["game_spec"]["core"]["limits"]["max_rounds"] == 13)
checks["исходная спецификация не испорчена"] = (
    SPEC["game_spec"]["core"]["limits"]["max_rounds"] == 12)

# --------------------------------------------------------------------------
# Порог не взят
# --------------------------------------------------------------------------

module = fresh([3.0, 4.0, 5.0])
counter = Calls([3.0, 4.0, 5.0])
counter.install(module)
out = module.run(SPEC, allow_code_run=True)

checks["кругов не больше лимита"] = out["rounds_made"] == module.MAX_REDESIGN_ATTEMPTS
checks["порог честно не взят"] = out["passed"] is False
checks["показан лучший круг"] = out["score"] == 5.0 and out["best_round"] == 3
checks["сказано, что это не готовая игра"] = "не готовая игра" in out["verdict"]

# --------------------------------------------------------------------------
# Редизайну нечего чинить
# --------------------------------------------------------------------------

module = fresh([4.0], changes=False)
counter = Calls([4.0], changes=False)
counter.install(module)
out = module.run(SPEC, allow_code_run=True)

checks["без правок круг не повторяется"] = out["rounds_made"] == 1
checks["и это не считается приёмкой"] = out["passed"] is False

# --------------------------------------------------------------------------
# Отказы агентов доходят словами
# --------------------------------------------------------------------------

module = fresh([8.0])
module.simulationist_agent.run = lambda core, **kw: {
    "available": True, "simulatable": False, "reason": "нет условия победы",
    "missing": ["win_condition.threshold"]}
try:
    module.run(SPEC, allow_code_run=True)
    checks["несимулируемая игра останавливает цепочку"] = False
except module.HeadlessError as error:
    checks["несимулируемая игра останавливает цепочку"] = True
    checks["причина названа словами автора"] = "нет условия победы" in str(error)
    checks["названо, чего не хватает"] = "win_condition.threshold" in str(error)

# --------------------------------------------------------------------------
# Сорвавшийся круг не отменяет оплаченные предыдущие
# --------------------------------------------------------------------------

module = fresh([4.0, 9.9])
counter = Calls([4.0, 9.9])
counter.install(module)
calls = {"n": 0}
исходный = module.simulationist_agent.run


def падает_на_втором(core, **kw):
    calls["n"] += 1
    if calls["n"] > 1:
        return {"available": True, "simulatable": False,
                "reason": "после правки ядро перестало сходиться", "missing": []}
    return исходный(core, **kw)


module.simulationist_agent.run = падает_на_втором
out = module.run(SPEC, allow_code_run=True)

checks["сбой второго круга не теряет первый"] = out["rounds_made"] == 1
checks["лучший из завершённых показан"] = out["score"] == 4.0
checks["сорвавшийся круг назван отдельным полем"] = out["broke_on"]["round"] == 2
checks["причина срыва сохранена"] = "перестало сходиться" in out["broke_on"]["reason"]
checks["в вердикте сказано про оплаченную работу"] = "не потеряна" in out["verdict"]

# А вот сбой ПЕРВОГО круга показывать нечем — ошибка идёт наружу как была.
module = fresh([4.0])
counter = Calls([4.0])
counter.install(module)
module.simulationist_agent.run = lambda core, **kw: {
    "available": True, "simulatable": False, "reason": "нет ядра", "missing": []}
try:
    module.run(SPEC, allow_code_run=True)
    checks["сбой первого круга не прячется"] = False
except module.HeadlessError as error:
    checks["сбой первого круга не прячется"] = "нет ядра" in str(error)


# --------------------------------------------------------------------------
# Дополнительные прогоны: «не заказывали» и «заказали, но не сделали» — разное
# --------------------------------------------------------------------------

module = fresh([8.0])
counter = Calls([8.0])
counter.install(module)
out = module.run(SPEC, allow_code_run=True)
best = out["best"]
checks["заказанные прогоны выполнены"] = (best["extra_runs_requested"] == 1
                                          and best["extra_runs_made"] is True)
checks["пропущенных прогонов нет"] = best["extra_runs_skipped"] is False

# Диагност ничего не заказал — это норма, а не повод предупреждать.
module = fresh([8.0])
counter = Calls([8.0])
counter.install(module)
module.diagnost_agent.run_triage = lambda data, **kw: {
    "available": True, "triage": {"tests": []},
    "kept_requests": [], "dropped_requests": []}
out = module.run(SPEC, allow_code_run=True)
best = out["best"]
checks["без заказа прогонов ничего не пропущено"] = (
    best["extra_runs_requested"] == 0 and best["extra_runs_skipped"] is False)


# --------------------------------------------------------------------------
# Согласие констант
# --------------------------------------------------------------------------

checks["лимит кругов совпадает с моделью"] = (
    headless.MAX_REDESIGN_ATTEMPTS == RedesignAttempt.MAX_ACCEPTED_ATTEMPTS)

for label, ok in checks.items():
    print(("OK  " if ok else "FAIL") + " | " + label)
assert all(checks.values()), "часть проверок провалилась"
print(f"\nВСЁ ОК ({len(checks)} проверок)")
