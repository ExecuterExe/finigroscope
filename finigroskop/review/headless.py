# -*- coding: utf-8 -*-
"""Симуляционный этап по готовому game_spec — без экранов и без базы.

Зачем отдельный модуль. Весь конвейер ФинИгроСкопа завязан на `Document`:
загруженный .docx, из которого зеркало понимания и извлеченец добывают
`game_spec`. У «Генератора игр» документа нет и не будет — у него game_spec
получается сборкой из принятых модулей. Проводить его через зеркало и
извлеченца значило бы разбирать текст, который мы сами же и написали, платя
за это дважды.

Поэтому здесь та же цепочка агентов, но входом служит сам game_spec:

    симуляционист → прогон кода → оценщик статистик → диагност
        → линзы Шелла → синтезатор → (авто-редизайнер, если балл ниже порога)

Что этот модуль НЕ делает:

  • не пишет в базу — ни строки. Результат возвращается целиком, а хранить его
    (и хранить ли) решает вызывающая сторона;
  • не создаёт документов и сессий;
  • не показывает экранов: цепочка идёт сама, автор видит только итог.

ГЛАВНОЕ ОГРАНИЧЕНИЕ, и оно намеренное. Прогон скелета — единственное место
сервиса, где выполняется код, написанный моделью. В интерфейсе он закрыт явным
нажатием человека. Здесь нажимать некому, поэтому выполнение включается
отдельным разрешением (`allow_code_run`), и по умолчанию оно ВЫКЛЮЧЕНО.
Автоматический прогон чужого кода по сетевому запросу — это удалённое
выполнение кода с правами сервиса, и включать его молча нельзя. Без разрешения
цепочка честно останавливается на этом шаге и говорит, чего не хватает.
"""

from __future__ import annotations

from review import diagnost as diagnost_agent
from review import extractor as extractor_agent
from review import lens_evaluator as lens_agent
from review import redesigner as redesign_agent
from review import simulationist as simulationist_agent
from review import stats_evaluator as stats_agent
from review import synthesizer as synth_agent
from simulation import runner as sim_runner

# Сколько раз игру правит авто-редизайнер, прежде чем мы отдаём что есть.
# ЗЕРКАЛО модели RedesignAttempt.MAX_ACCEPTED_ATTEMPTS: значения обязаны
# совпадать, иначе безэкранный прогон и экранный будут жить по разным правилам.
# Расхождение ловит tests/headless_check.py.
MAX_REDESIGN_ATTEMPTS = 3

# Порог приёмки — тот же, что у синтезатора и у генератора.
PASSING_SCORE = 6.0


class HeadlessError(Exception):
    """Цепочку выполнить не удалось. Текст пригоден для показа автору."""

    user_facing = True


class _Silent(object):
    """Заглушка отчёта о ходе работы: цепочку можно гонять и без него."""

    def say(self, step, detail=None):
        return None

    def check_cancelled(self):
        return None


def core_of(spec_root):
    return ((spec_root or {}).get("game_spec") or {}).get("core") or {}


def _subjective_actions(spec_root, sim_meta):
    """Действия с «мягкими» числами — объединение двух источников.

    Извлеченец (или сборщик генератора) классифицирует действие как
    subjective_judgment по описанию, а симуляционист заявляет, что реально
    смоделировал случайной величиной. Источники расходятся, и объединение —
    безопасное направление: лишняя осторожность даёт warn вместо problem, а
    пропуск — ложный флаг доминирующей стратегии по артефакту допущения.
    """
    declared = (sim_meta or {}).get("subjective_actions") or []
    return sorted(set(extractor_agent.subjective_actions(spec_root)) | set(declared))


# --------------------------------------------------------------------------
# Шаги
# --------------------------------------------------------------------------

def build_skeleton(spec_root, provider_name=None):
    """Симуляционист: game_spec.core → код скелета."""
    outcome = simulationist_agent.run(
        core_of(spec_root),
        diagnostic_meta=(spec_root or {}).get("diagnostic_meta"),
        provider_name=provider_name)

    if not outcome.get("available"):
        raise HeadlessError("Симуляционист недоступен: %s"
                            % (outcome.get("error") or "причина не названа"))
    if not outcome.get("simulatable"):
        raise HeadlessError(
            "Игра не поддаётся симуляции: %s%s"
            % (outcome.get("reason") or "причина не названа",
               (" Не хватает: " + ", ".join(outcome.get("missing") or []))
               if outcome.get("missing") else ""))
    if not outcome.get("code"):
        raise HeadlessError("Симуляционист не вернул кода скелета.")
    return outcome


def run_skeleton(code, allow_code_run):
    """Прогон скелета. Закрыт разрешением — см. предупреждение в шапке модуля."""
    if not allow_code_run:
        raise HeadlessError(
            "Прогон скелета не разрешён. Это единственное место сервиса, где "
            "выполняется код, написанный моделью, и включается оно явно: "
            "SIM_API_ALLOW_RUN=1 в окружении ФинИгроСкопа. Без прогона нет "
            "статистики, а без неё — ни оценки баланса, ни диагноста.")

    outcome = sim_runner.run_skeleton(code)
    if not outcome.get("ok"):
        raise HeadlessError("Скелет не отработал: %s"
                            % (outcome.get("error") or "причина не названа"))
    if not outcome.get("stats"):
        raise HeadlessError("Прогон скелета не дал статистики (STATS_JSON пуст).")
    return outcome


def evaluate_balance(spec_root, stats, sim_meta, provider_name=None):
    """Оценщик статистик: STATS_JSON → Finding_balance."""
    outcome = stats_agent.run(
        core_of(spec_root), stats,
        diagnostic_meta=(spec_root or {}).get("diagnostic_meta"),
        assumptions=(sim_meta or {}).get("assumptions") or [],
        subjective_actions=_subjective_actions(spec_root, sim_meta),
        provider_name=provider_name)

    if not outcome.get("available"):
        raise HeadlessError("Оценщик статистик недоступен: %s"
                            % (outcome.get("error") or "причина не названа"))
    return outcome


def run_diagnost(spec_root, stats, diag, balance, sim_meta, code,
                 allow_code_run, attempt_number=1, provider_name=None):
    """Диагност в два прохода. Дополнительные прогоны заказывает он сам.

    Бюджет прогонов режет КОД (см. diagnost.run_triage), а не модель. Если
    выполнение кода не разрешено, доп. прогоны просто не делаются: их тесты
    получат честное n/a, а не исчезнут из отчёта.
    """
    data = diagnost_agent.build_input(
        spec_root, stats, diag, balance, sim_meta, attempt_number=attempt_number)

    triage = diagnost_agent.run_triage(data, provider_name=provider_name)
    if not triage.get("available"):
        raise HeadlessError("Диагност недоступен на триаже: %s"
                            % (triage.get("error") or "причина не названа"))

    kept = triage.get("kept_requests") or []
    extra_runs = {}
    if kept and allow_code_run:
        extra_runs = sim_runner.run_extra_runs(code, kept)

    findings = diagnost_agent.run_findings(
        data, triage["triage"], extra_runs=extra_runs,
        dropped=triage.get("dropped_requests") or [],
        provider_name=provider_name)
    if not findings.get("available"):
        raise HeadlessError("Диагност недоступен на вердиктах: %s"
                            % (findings.get("error") or "причина не названа"))

    return {
        "triage": triage,
        "findings": findings,
        # Три РАЗНЫХ состояния, и путать их нельзя. «Не заказывали» — обычное
        # дело: диагносту хватило основного прогона. «Заказали и не выполнили» —
        # повод сказать автору, что часть тестов ушла в n/a. Первая версия
        # возвращала одно bool на оба случая и предупреждала там, где
        # предупреждать было не о чем.
        "extra_runs_requested": len(kept),
        "extra_runs_made": bool(extra_runs),
        "extra_runs_skipped": bool(kept) and not extra_runs,
    }


def run_lenses(spec_root, balance, findings, sim_meta, provider_name=None):
    """Линзы Шелла по игре ЦЕЛИКОМ — не по модулю: описание уже собрано."""
    data = lens_agent.build_input(
        spec_root,
        stats_notes=(balance.get("report") or {}).get("notes_for_lenses"),
        diagnost_notes=(findings.get("findings") or {}).get("notes_for_lenses"),
        sim_meta=sim_meta,
        coverage_summary=(findings.get("findings") or {}).get("coverage_summary"))

    outcome = lens_agent.run(data, provider_name=provider_name)
    if not outcome.get("available"):
        raise HeadlessError("Оценщик по линзам недоступен: %s"
                            % (outcome.get("error") or "причина не названа"))
    return outcome


def run_synthesis(lenses, balance, findings, stats, sim_meta, attempt_number,
                  previous_score, attempts_left, not_touched, handed,
                  provider_name=None):
    """Синтезатор. Балл считает КОД, суждение — агент."""
    data = synth_agent.build_input(
        lenses.get("report") or {},
        balance.get("report") or {},
        findings.get("findings") or {},
        stats=stats, sim_meta=sim_meta,
        redesign_not_touched=not_touched,
        redesign_recommendations=handed,
        attempt_number=attempt_number,
        previous_score=previous_score,
        attempts_left=attempts_left)

    outcome = synth_agent.run(data, provider_name=provider_name)
    if not outcome.get("available"):
        raise HeadlessError("Синтезатор недоступен: %s"
                            % (outcome.get("error") or "причина не названа"))
    return outcome


# --------------------------------------------------------------------------
# Цепочка целиком
# --------------------------------------------------------------------------

def run(spec_root, progress=None, allow_code_run=False, provider_name=None,
        max_redesign=MAX_REDESIGN_ATTEMPTS):
    """Весь симуляционный этап. Возвращает отчёт со всеми промежуточными шагами.

    Круг повторяется, пока балл ниже порога и авто-редизайнер что-то правит.
    Правка применяется к game_spec, и следующий круг начинается с нового
    скелета: изменённое ядро делает прежние статистику и вердикты
    недействительными — считать по ним значило бы смешать числа новой игры с
    оценками старой.
    """
    progress = progress or _Silent()

    # Разрешение проверяем ДО первого обращения к модели, а не там, где оно
    # понадобится. Без прогона кода нет статистики, а без неё вся цепочка —
    # оценка баланса, диагност, линзы, синтез — работать не может: круг
    # оборвётся на втором шаге. Пока проверка стояла внутри run_skeleton,
    # симуляционист успевал отработать и выставить счёт за код, который никто
    # не запустит.
    if not allow_code_run:
        raise HeadlessError(
            "Прогон скелета не разрешён, а без него разбор невозможен: не будет "
            "ни статистики, ни оценки баланса, ни диагноста. Это единственное "
            "место сервиса, где выполняется код, написанный моделью, поэтому "
            "включается оно явно: SIM_API_ALLOW_RUN=1 в окружении ФинИгроСкопа. "
            "Ни одного обращения к модели не сделано — платить за разбор, "
            "который всё равно оборвётся, незачем.")

    if max_redesign < 1:
        raise HeadlessError(
            "Число кругов должно быть не меньше одного, получено %s." % max_redesign)

    spec = spec_root
    attempts = []
    not_touched, handed = [], []
    previous_score = None

    for attempt in range(1, max_redesign + 1):
        progress.check_cancelled()
        attempts_left = max_redesign - attempt

        try:
            round_report = _one_round(
                spec, progress, allow_code_run, provider_name, attempt,
                previous_score, attempts_left, not_touched, handed)
        except HeadlessError as error:
            # Круг стоит полутора десятков обращений к моделям. Если сорвался
            # ПЕРВЫЙ — показывать нечего, и ошибка идёт наружу как была. Если
            # сорвался второй или третий, у нас уже есть оплаченный результат
            # предыдущего: выбрасывать его вместе со сбоем значит потерять
            # работу, за которую уже заплачено, и оставить автора ни с чем.
            if not attempts:
                raise
            return _finish(spec, attempts, passed=False, broke_on=(attempt, str(error)))

        attempts.append(round_report)

        score = round_report["score"]
        if score is not None and score >= PASSING_SCORE:
            return _finish(spec, attempts, passed=True)

        if attempt >= max_redesign:
            break

        progress.check_cancelled()
        progress.say("авто-редизайн", detail="балл ниже порога, ищем минимальную правку")

        fix = _try_redesign(spec, round_report, attempt, provider_name)
        if fix is None:
            # Чинить нечего: либо редизайн не нужен по своим правилам, либо он
            # ничего не предложил. Повторять круг незачем — он даст то же самое.
            break

        spec = fix["spec"]
        not_touched += fix["result"].get("not_touched") or []
        handed += fix["result"].get("handed_to_recommendations") or []
        round_report["redesign"] = fix["result"]
        previous_score = score

    return _finish(spec, attempts, passed=False)


def _one_round(spec, progress, allow_code_run, provider_name, attempt,
               previous_score, attempts_left, not_touched, handed):
    """Один круг: от скелета до балла."""
    progress.say("сборка скелета", detail="симуляционист пишет модель игры")
    skeleton = build_skeleton(spec, provider_name)
    sim_meta = skeleton.get("meta") or {}

    progress.check_cancelled()
    progress.say("прогон скелета", detail="считаем партии")
    run_out = run_skeleton(skeleton["code"], allow_code_run)
    stats, diag = run_out["stats"], run_out.get("diag")

    progress.check_cancelled()
    progress.say("оценка баланса", detail="агент разбирает статистику")
    balance = evaluate_balance(spec, stats, sim_meta, provider_name)

    progress.check_cancelled()
    progress.say("диагност", detail="разбор по тестам методички, два прохода")
    diagnost = run_diagnost(spec, stats, diag, balance.get("report") or {},
                            sim_meta, skeleton["code"], allow_code_run,
                            attempt_number=attempt, provider_name=provider_name)

    progress.check_cancelled()
    progress.say("линзы Шелла", detail="качественная оценка игры целиком")
    lenses = run_lenses(spec, balance, diagnost, sim_meta, provider_name)

    progress.check_cancelled()
    progress.say("синтез", detail="сводим оценки в итоговый балл")
    synthesis = run_synthesis(lenses, balance, diagnost, stats, sim_meta,
                              attempt, previous_score, attempts_left,
                              not_touched, handed, provider_name)

    report = synthesis.get("report") or {}
    return {
        "attempt": attempt,
        "skeleton": {"meta": sim_meta, "code": skeleton["code"]},
        "stats": stats,
        "diag": diag,
        "balance": balance.get("report"),
        "diagnost": diagnost["findings"].get("findings"),
        "coverage": (diagnost["findings"].get("findings") or {}).get("coverage_summary"),
        "extra_runs_requested": diagnost["extra_runs_requested"],
        "extra_runs_made": diagnost["extra_runs_made"],
        "extra_runs_skipped": diagnost["extra_runs_skipped"],
        "lenses": lenses.get("report"),
        "synthesis": report,
        "reference": synthesis.get("reference"),
        "score": report.get("overall_score"),
        "redesign": None,
    }


def _try_redesign(spec, round_report, attempt, provider_name):
    """Авто-редизайн, если он вообще нужен. None — чинить нечего."""
    balance = round_report.get("balance") or {}
    synthesis = round_report.get("synthesis") or {}

    trigger = redesign_agent.should_trigger(balance, synthesis)
    if not trigger.get("trigger"):
        return None

    outcome = redesign_agent.run(
        spec, balance, attempt_number=attempt,
        findings_diagnost=(round_report.get("diagnost") or {}).get("findings"),
        findings_lenses=(round_report.get("lenses") or {}).get("issues"),
        synthesis_critique=synthesis.get("top_priorities"),
        provider_name=provider_name)

    if not outcome.get("available"):
        return None
    result = outcome.get("result") or {}
    if not result.get("changes"):
        return None

    return {"spec": redesign_agent.apply_to_spec(spec, result), "result": result}


def _finish(spec, attempts, passed, broke_on=None):
    best = max(attempts, key=lambda a: (a["score"] is not None, a["score"] or 0))
    score = best["score"]

    if broke_on:
        number, reason = broke_on
        verdict = (
            "Круг %d сорвался: %s Показан лучший из завершённых — %s. Оплаченная "
            "работа предыдущих кругов не потеряна."
            % (number, reason,
               "балла нет" if score is None else "%.3f из 10" % score))
    elif passed:
        verdict = ("Игра принята: %.3f при пороге %.1f." % (score, PASSING_SCORE))
    elif score is None:
        verdict = "Балл не получен ни в одном круге."
    else:
        verdict = (
            "Порог не взят за %d %s. Лучший результат — %.3f из 10 (круг %d). "
            "Это не готовая игра, а лучшее из полученного: решать, дорабатывать "
            "её дальше или менять ответы опросника, вам."
            % (len(attempts), "круг" if len(attempts) == 1 else "круга",
               score, best["attempt"]))

    return {
        "ok": True,
        "passed": passed,
        "threshold": PASSING_SCORE,
        "score": score,
        "rounds_made": len(attempts),
        "rounds_allowed": MAX_REDESIGN_ATTEMPTS,
        "best_round": best["attempt"],
        # Сорвавшийся круг называется отдельным полем, а не только в тексте:
        # вызывающая сторона должна отличать «порог не взят» от «оборвалось».
        "broke_on": ({"round": broke_on[0], "reason": broke_on[1]}
                     if broke_on else None),
        "spec": spec,
        "rounds": attempts,
        "best": best,
        "verdict": verdict,
    }
