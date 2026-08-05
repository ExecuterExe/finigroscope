# -*- coding: utf-8 -*-
"""Проверка волны 3: фоновые задачи, изоляция конфигураций, миграции.

Три группы, по одной на каждую проблему исходного списка.

П8 — фоновое выполнение. Проверяется не «работает ли пул» (это видно и так), а
то, ради чего он написан: повторный запрос НЕ создаёт вторую задачу (иначе
двойной клик удваивает счёт за модель), падение внутри задачи не роняет сервер,
а отражается в статусе, отмена доводится до конца, и задачи, застигнутые
перезапуском, не висят в «идёт» вечно.

П10 — тестовый прогон обязан ходить в СВОЮ базу. Проверяется буквально: путь
боевой базы и путь тестовой не должны совпасть.

П7 — миграции. Три сценария в отдельных процессах: чистый файл, унаследованная
база без отметки о ревизии (данные обязаны уцелеть) и повторный запуск.

Единственный набор, который работает в НАСТОЯЩЕМ асинхронном режиме: остальные
идут синхронно (см. config.jobs_sync), иначе каждую сквозную проверку пришлось
бы учить ждать поток.
"""
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, ".")
os.environ["LLM_PROVIDER_FORCE"] = "mock"
os.environ["FINIGROSKOP_JOBS_SYNC"] = "0"

import config  # noqa: E402
import jobs  # noqa: E402
from models import Document, Job, User, db  # noqa: E402

checks = {}

# ============================================================================
# ЧАСТЬ 1. П10 — тестовый прогон не трогает боевую базу
# ============================================================================
test_db = config.database_path()
checks["П10: у тестов своя база"] = test_db.endswith("finigroskop-test.db")
checks["П10: тестовый прогон распознан"] = config.is_test_run() is True
checks["П10: у тестов свои загрузки"] = config.upload_dir().endswith("uploads-test")

os.environ["FINIGROSKOP_DB"] = "instance/explicit.db"
checks["П10: FINIGROSKOP_DB сильнее эвристики"] = (
    config.database_path().endswith("explicit.db"))
del os.environ["FINIGROSKOP_DB"]

checks["П10: очистка в тестах разрешена"] = config.reset_allowed() is True
os.environ["FINIGROSKOP_RESET"] = "0"
checks["П10: явный запрет сильнее"] = config.reset_allowed() is False
del os.environ["FINIGROSKOP_RESET"]

# Воспроизведение найденной дыры: llm_settings_flow_check намеренно СНИМАЕТ
# LLM_PROVIDER_FORCE (иначе он не проверит, что выбор в интерфейсе работает) —
# и на одной этой переменной уходил в боевую базу. Признак «скрипт лежит в
# tests/» обязан удержать его в тестовой.
_saved = os.environ.pop("LLM_PROVIDER_FORCE")
checks["П10: без LLM_PROVIDER_FORCE база всё равно тестовая"] = (
    config.database_path() == test_db)
checks["П10: признак по пути запуска работает"] = config.is_test_run() is True
os.environ["LLM_PROVIDER_FORCE"] = _saved

# Обычный запуск (не из tests/, без переменных) — база боевая, очистка запрещена.
# Проверяем отдельным процессом: в этом определить нельзя, признак прогона уже
# сработал по пути запуска.
_clean_env = {k: v for k, v in os.environ.items()
              if not k.startswith("FINIGROSKOP_") and k != "LLM_PROVIDER_FORCE"}
_clean_env["PYTHONIOENCODING"] = "utf-8"
probe = subprocess.run(
    [sys.executable, "-c",
     "import sys; sys.path.insert(0,'.'); import config; "
     "print(config.database_path()); print(config.reset_allowed()); "
     "print(config.jobs_sync())"],
    env=_clean_env, capture_output=True, text=True, encoding="utf-8")
prod_db, prod_reset, prod_sync = probe.stdout.strip().splitlines()
checks["П10: боевая база — другой файл"] = prod_db != test_db
checks["П10: боевая база называется finigroskop.db"] = prod_db.endswith("finigroskop.db")
checks["П10: в боевом режиме очистка запрещена"] = prod_reset == "False"
checks["П10: в боевом режиме задачи идут в пуле"] = prod_sync == "False"

# ============================================================================
# ЧАСТЬ 2. П8 — фоновые задачи
# ============================================================================
import app as A  # noqa: E402

checks["П8: пул включён (не синхронный режим)"] = config.jobs_sync() is False

cl = A.app.test_client()
cl.get("/dashboard")          # создаёт гостя этой сессии — он же владелец
with A.app.app_context():
    owner = User.query.filter(User.tg_tag.like("@guest-%")).order_by(
        User.id.desc()).first()
    d = Document(user_id=owner.id, filename="j.docx", stored_path="j",
                 file_hash="jobs-check", doc_type="essay", version=1)
    db.session.add(d)
    db.session.commit()
    DOC = d.id


def wait_for(job_id, timeout=15):
    """Ждёт финального статуса задачи."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with A.app.app_context():
            job = db.session.get(Job, job_id)
            if job is not None and job.status in Job.FINAL:
                return job.status, job.error
        time.sleep(0.05)
    return "timeout", None


# --- ИДЕМПОТЕНТНОСТЬ: двойной клик не удваивает расходы на модель ------------
slow_calls = {"n": 0}
release = {"go": False}


def slow(job_id=None):
    slow_calls["n"] += 1
    while not release["go"]:
        time.sleep(0.02)


with A.app.app_context():
    first = jobs.submit(DOC, 1, "mirror", slow)
    first_id = first.id
    second_id = jobs.submit(DOC, 1, "mirror", slow).id
    checks["П8: повторный запрос вернул ТУ ЖЕ задачу"] = first_id == second_id
    checks["П8: вторая задача не создана"] = Job.query.filter_by(
        document_id=DOC, game_index=1, step="mirror").count() == 1

release["go"] = True
checks["П8: задача завершилась"] = wait_for(first_id)[0] == Job.DONE
checks["П8: тело вызвано ровно один раз"] = slow_calls["n"] == 1

# Шаг завершён — новый запрос обязан создать НОВУЮ задачу (перезапуск шага).
with A.app.app_context():
    again_id = jobs.submit(DOC, 1, "mirror", lambda job_id=None: None).id
checks["П8: завершённый шаг перезапускается"] = again_id != first_id
wait_for(again_id)

# --- ПАДЕНИЕ внутри задачи не роняет сервер ---------------------------------
def boom(job_id=None):
    raise ValueError("нарочная поломка внутри задачи")


with A.app.app_context():
    boom_id = jobs.submit(DOC, 1, "skeleton", boom).id
status, error = wait_for(boom_id)
checks["П8: падение отражено статусом"] = status == Job.FAILED
checks["П8: текст ошибки сохранён"] = "нарочная поломка" in (error or "")
checks["П8: трассировка сохранена"] = "Traceback" in (error or "")

# Сервер жив: следующая задача исполняется как ни в чём не бывало.
with A.app.app_context():
    ok_id = jobs.submit(DOC, 1, "balance", lambda job_id=None: None).id
checks["П8: сервер пережил падение задачи"] = wait_for(ok_id)[0] == Job.DONE

# --- ОТМЕНА ------------------------------------------------------------------
gate = {"open": False}
reached = {"after": False}


def two_stage(job_id=None):
    while not gate["open"]:
        time.sleep(0.02)
    jobs.check_cancelled(job_id)      # точка отмены МЕЖДУ этапами
    reached["after"] = True


with A.app.app_context():
    cancel_id = jobs.submit(DOC, 1, "lenses", two_stage).id
time.sleep(0.3)                        # дать потоку дойти до ожидания
with A.app.app_context():
    checks["П8: задача действительно идёт"] = (
        db.session.get(Job, cancel_id).status == Job.RUNNING)
    checks["П8: отмена принята"] = jobs.request_cancel(cancel_id) is True
gate["open"] = True
checks["П8: задача отменена"] = wait_for(cancel_id)[0] == Job.CANCELLED
checks["П8: этап после точки отмены не выполнился"] = reached["after"] is False

with A.app.app_context():
    checks["П8: завершённую отменять нечего"] = jobs.request_cancel(cancel_id) is False

# Отмена ДО начала работы гасит задачу сразу, не дожидаясь потока.
with A.app.app_context():
    queued = Job(document_id=DOC, game_index=1, step="synthesis", status=Job.QUEUED)
    db.session.add(queued)
    db.session.commit()
    jobs.request_cancel(queued.id)
    checks["П8: очередь гасится сразу"] = (
        db.session.get(Job, queued.id).status == Job.CANCELLED)

# --- ПЕРЕЗАПУСК СЕРВЕРА: осиротевшие задачи не висят вечно -------------------
with A.app.app_context():
    orphan = Job(document_id=DOC, game_index=1, step="diagnost_triage",
                 status=Job.RUNNING)
    db.session.add(orphan)
    db.session.commit()
    orphan_id = orphan.id
    healed = jobs.recover_orphans()
    row = db.session.get(Job, orphan_id)
    checks["П8: осиротевшая задача найдена"] = healed >= 1
    checks["П8: осиротевшая помечена failed"] = row.status == Job.FAILED
    checks["П8: причина понятна автору"] = "перезапустил" in (row.error or "")
    checks["П8: завершённые задачи не тронуты"] = (
        db.session.get(Job, boom_id).error or "").startswith("ValueError")

# --- ОПРОС СОСТОЯНИЯ (он же основа будущего API интеграции) ------------------
with A.app.app_context():
    payload = db.session.get(Job, boom_id).as_dict()
checks["П8: в ответе есть шаг и игра"] = (
    payload["step"] == "skeleton" and payload["game_index"] == 1)
checks["П8: длительность посчитана"] = isinstance(payload["duration_sec"], float)

resp = cl.get(f"/jobs/{boom_id}.json")
checks["П8: статус доступен по HTTP"] = resp.status_code == 200
checks["П8: HTTP отдаёт статус задачи"] = resp.get_json()["status"] == Job.FAILED
checks["П8: неизвестная задача — 404"] = cl.get("/jobs/999999.json").status_code == 404

listing = cl.get(f"/documents/{DOC}/jobs/1.json").get_json()
checks["П8: карта задач игры отдаётся"] = len(listing["jobs"]) >= 5
checks["П8: карта задач в порядке постановки"] = (
    [j["id"] for j in listing["jobs"]] == sorted(j["id"] for j in listing["jobs"]))

checks["П8: у каждого шага есть название"] = all(
    jobs.step_title(s) != s for s in jobs.STEP_TITLES)
checks["П8: у каждого шага есть подсказка"] = all(
    jobs.step_hint(s) for s in jobs.STEP_TITLES)
checks["П8: неизвестный шаг не роняет экран"] = jobs.step_title("???") == "???"

# ============================================================================
# ЧАСТЬ 2б. Платный цикл автозапуска
# ============================================================================
# Найдено на живом сервере и стоило восьми обращений к модели подряд без единого
# действия человека. Условие автозапуска смотрело только на «результата нет» и
# «задача не идёт прямо сейчас». Пока страница была статической, это означало
# один вызов на одно открытие экрана. С плашкой прогресса, которая перезагружает
# страницу сама, получился цикл: шаг завершился, ничего не записав (модель
# недоступна), страница перезагрузилась, экран запустил шаг снова — и так до
# бесконечности.
#
# Проверяется на самом дешёвом шаге конвейера и в СИНХРОННОМ режиме: важно не
# «сколько потоков», а сколько раз экран решит позвать модель.
import re  # noqa: E402

os.environ["FINIGROSKOP_JOBS_SYNC"] = "1"
os.environ["LLM_PROVIDER_FORCE"] = "null"          # агенты честно «недоступны»

ESSAY = r"C:\Users\Eugene\Desktop\НСПК\Фин-игры\Фин-игры эссе - версия от 1 февраля.docx"
with open(ESSAY, "rb") as fh:
    up = cl.post("/upload", data={"doc_type": "essay", "file": (fh, "loop.docx")},
                 content_type="multipart/form-data", follow_redirects=True)
LOOP_DOC = int(re.search(r"/documents/(\d+)/games", up.request.path).group(1))

for _ in range(4):                                  # четыре «перезагрузки» подряд
    cl.get(f"/documents/{LOOP_DOC}/mirror/1")

with A.app.app_context():
    started = Job.query.filter_by(document_id=LOOP_DOC, game_index=1,
                                  step="mirror").count()
checks["ЦИКЛ: четыре загрузки экрана дали ОДИН запуск"] = started == 1

html = cl.get(f"/documents/{LOOP_DOC}/mirror/1").get_data(as_text=True)
checks["ЦИКЛ: экран не врёт про «агент читает»"] = "Агент читает игру…" not in html
checks["ЦИКЛ: видно, что шаг не выполнился"] = ("Шаг не выполнился" in html
                                                or "🛑" in html)
checks["ЦИКЛ: есть кнопка прогнать заново"] = "Прогнать заново" in html or (
    "Попробовать снова" in html)

# Явная кнопка перезапуска работает — и она единственный способ повторить шаг.
cl.post(f"/documents/{LOOP_DOC}/mirror/1/retry", follow_redirects=True)
with A.app.app_context():
    after_retry = Job.query.filter_by(document_id=LOOP_DOC, game_index=1,
                                      step="mirror").count()
checks["ЦИКЛ: кнопка запускает шаг заново"] = after_retry == started + 1

cl.get(f"/documents/{LOOP_DOC}/mirror/1")
with A.app.app_context():
    checks["ЦИКЛ: после ретрая экран снова не самозапускается"] = (
        Job.query.filter_by(document_id=LOOP_DOC, game_index=1,
                            step="mirror").count() == after_retry)

# Само правило — отдельно от экранов.
now = datetime.utcnow()
with A.app.app_context():
    checks["ЦИКЛ: без задач автозапуск разрешён"] = jobs.may_autostart(
        LOOP_DOC, 9, "lenses") is True
    fresh = Job(document_id=LOOP_DOC, game_index=9, step="lenses", status=Job.DONE,
                created_at=now)
    db.session.add(fresh)
    db.session.commit()
    checks["ЦИКЛ: после завершённой задачи автозапуск закрыт"] = jobs.may_autostart(
        LOOP_DOC, 9, "lenses") is False
    checks["ЦИКЛ: после сбоя автозапуск тоже закрыт"] = jobs.may_autostart(
        LOOP_DOC, 9, "lenses", now - timedelta(seconds=1)) is False
    # Новый круг авто-редизайна заводит НОВУЮ запись — её автозапуск разрешён.
    checks["ЦИКЛ: новая попытка запускается сама"] = jobs.may_autostart(
        LOOP_DOC, 9, "lenses", now + timedelta(seconds=1)) is True

# У каждого шага, который экран запускает сам, обязан быть путь перезапуска —
# иначе сбой станет тупиком.
AUTOSTARTED = ("mirror", "extraction", "gate2", "skeleton", "balance",
               "diagnost_triage", "lenses", "synthesis")
checks["ЦИКЛ: у всех автозапускаемых шагов есть перезапуск"] = all(
    jobs.retry_endpoint(s) for s in AUTOSTARTED)
with A.app.test_request_context():
    checks["ЦИКЛ: маршруты перезапуска существуют"] = all(
        A.url_for(jobs.retry_endpoint(s), doc_id=1, game_index=1)
        for s in AUTOSTARTED)

os.environ["FINIGROSKOP_JOBS_SYNC"] = "0"
os.environ["LLM_PROVIDER_FORCE"] = "mock"

# ============================================================================
# ЧАСТЬ 3. П7 — миграции
# ============================================================================
BOOT = "import sys; sys.path.insert(0,'.'); import app; print('BOOTED')"


def boot(db_path):
    """Поднимает приложение в отдельном процессе на указанном файле базы."""
    env = dict(os.environ, FINIGROSKOP_DB=db_path, LLM_PROVIDER_FORCE="mock",
               FINIGROSKOP_RESET="0", FINIGROSKOP_JOBS_SYNC="1",
               PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, "-c", BOOT], env=env,
                          capture_output=True, text=True, encoding="utf-8")


def tables(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {r[0] for r in conn.execute(
            "select name from sqlite_master where type='table'")}
    finally:
        conn.close()


def tags(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return list(conn.execute("select tg_tag from users order by id"))
    finally:
        conn.close()


os.makedirs("instance", exist_ok=True)
FRESH = "instance/t-migrate-fresh.db"
LEGACY = "instance/t-migrate-legacy.db"
for leftover in (FRESH, LEGACY):
    if os.path.exists(leftover):
        os.remove(leftover)

# --- чистый файл --------------------------------------------------------------
r = boot(FRESH)
checks["П7: старт на пустой базе"] = "BOOTED" in r.stdout
checks["П7: схема поднята миграциями"] = "alembic_version" in tables(FRESH)
checks["П7: таблица задач создана"] = "jobs" in tables(FRESH)

# --- унаследованная база: данные обязаны пережить обновление -------------------
# Так выглядит база, созданная прежним `db.create_all()`: таблицы есть, отметки
# о ревизии нет, новых таблиц (jobs, lens_reports) не существует.
conn = sqlite3.connect(LEGACY)
conn.execute("create table users (id integer primary key, tg_tag text, "
             "password_hash text, role text)")
conn.execute("insert into users (tg_tag, password_hash, role) "
             "values ('@legacy', 'x', 'participant')")
conn.commit()
conn.close()

r = boot(LEGACY)
checks["П7: старт на унаследованной базе"] = "BOOTED" in r.stdout
t = tables(LEGACY)
checks["П7: недостающие таблицы достроены"] = {"jobs", "lens_reports"} <= t
checks["П7: отметка о ревизии поставлена"] = "alembic_version" in t
before = tags(LEGACY)
checks["П7: ДАННЫЕ НЕ ПОТЕРЯНЫ"] = before == [("@legacy",)]

# --- повторный запуск идемпотентен --------------------------------------------
r = boot(LEGACY)
checks["П7: повторный старт не ломает базу"] = (
    "BOOTED" in r.stdout and tags(LEGACY) == before)

for label, ok in checks.items():
    print(("OK  " if ok else "FAIL") + " | " + label)
assert all(checks.values()), "часть проверок провалилась"
print(f"\nВСЁ ОК ({len(checks)} проверок)")
