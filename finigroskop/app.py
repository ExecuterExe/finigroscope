"""ФинИгроСкоп — Flask-приложение.

На текущем этапе реализован публичный «фасад» сервиса:
  • приветственная (лендинг) страница с описанием трёх этапов конвейера;
  • вход без регистрации (telegram-ник + персональный пароль);
  • заглушка личного кабинета, доступная только после входа.

Всё, что за пределами лендинга и страницы входа, требует авторизации.
"""

import hashlib
import hmac
import json
import os
import uuid
from functools import wraps

from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

import config
import jobs
import migrate
from analysis import stage1
from analysis.reference import DOC_TYPES
from models import (
    BalanceReport,
    DiagnosisReport,
    Document,
    GameSkeleton,
    GameSpec,
    Job,
    LensReport,
    MirrorSession,
    RedesignAttempt,
    Report,
    SynthesisReport,
    User,
    db,
)
from review import diagnost as diagnost_agent
from review import extractor as extractor_agent
from review import headless
from review import lens_evaluator as lens_agent
from review import lens_module, lens_queue
from review import llm_provider, llm_settings
from review import mirror as mirror_agent
from review import redesigner as redesign_agent
from review import simulationist as simulationist_agent
from review import spec_describe
from review import stats_evaluator as stats_agent
from review import synthesizer as synth_agent
from simulation import runner as sim_runner

# Секреты (LLM_PROVIDER, GEMINI_API_KEY, ...) читаются из .env В ЭТОЙ ПАПКЕ —
# см. .env.example. Файл .env в git не попадает (см. .gitignore).
#
# Путь указан явно, а не поиском вверх по дереву: пустой load_dotenv() берёт
# первый .env, найденный от app.py и выше. Сегодня это нужный файл, но рядом в
# экосистеме лежит generator/ со своим .env, где ТЕ ЖЕ имена переменных значат
# другое (LLM_PROVIDER, LLM_TIMEOUT, имена моделей). Стоит кому-нибудь положить
# .env в корень экосистемы — и сервис молча заработает на чужих настройках.
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# Пути и режим запуска — из окружения (см. config.py). Раньше путь к базе был
# константой, и тестовый прогон работал с тем же файлом, что живой сервер.
UPLOAD_DIR = config.upload_dir()

# Адрес соседнего сервиса экосистемы. В шаблон его зашивать нельзя: в разработке
# это отдельный порт, а на сервере — путь за тем же nginx (см. deploy/).
GENERATOR_URL = os.environ.get("GENERATOR_URL", "http://localhost:8000")

# --- создание приложения ----------------------------------------------------
app = Flask(__name__)
app.config.update(
    SECRET_KEY=config.secret_key(),
    SQLALCHEMY_DATABASE_URI=config.database_uri(),
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,  # 16 МБ на файл
    UPLOAD_DIR=UPLOAD_DIR,
)

# Вход временно ОТКЛЮЧЁН для быстрого тестирования: сервис работает без логина.
# При этом хранилище ЭФЕМЕРНОЕ и ИЗОЛИРОВАННОЕ ПО СЕССИИ браузера:
#   • при перезапуске сервера БД и папка uploads очищаются (файлы не копятся);
#   • каждый посетитель видит только свои загрузки (документы привязаны к сессии).
# Чтобы вернуть штатный вход — снять флаг (сброс/изоляция отключатся сами).
AUTH_DISABLED = True

db.init_app(app)


def _reset_storage():
    """Полная очистка хранилища: пересоздать таблицы и удалить загруженные файлы.

    Схему после сброса поднимают МИГРАЦИИ, а не create_all: иначе свежая база
    осталась бы без отметки о ревизии, и следующая миграция попыталась бы
    накатиться на неё второй раз.
    """
    db.drop_all()
    db.session.commit()
    migrate.upgrade(db)
    for name in os.listdir(UPLOAD_DIR):
        try:
            os.remove(os.path.join(UPLOAD_DIR, name))
        except OSError:
            pass


with app.app_context():
    # Схему приводят в порядок миграции (см. migrate.py): create_all умел только
    # создавать недостающие таблицы и молча пропускал новые колонки в уже
    # существующих — с постоянным хранилищем это ломало бы сервис при каждом
    # добавлении поля.
    migrate.upgrade(db)
    # Очистка хранилища — ТОЛЬКО по явному разрешению и НИКОГДА как побочный
    # эффект импорта. Раньше она стояла прямо здесь, и любой запуск тестов
    # (а они импортируют app) стирал базу работающего рядом дев-сервера.
    if AUTH_DISABLED and config.reset_allowed():
        _reset_storage()

# Пул фоновых задач + подчистка задач, застигнутых прошлым перезапуском.
jobs.init_app(app)


# --- утилиты авторизации ----------------------------------------------------
def _session_user():
    """Гостевой пользователь, уникальный для сессии браузера (изоляция загрузок)."""
    sid = session.get("sid")
    if not sid:
        sid = uuid.uuid4().hex[:12]
        session["sid"] = sid
    tag = f"@guest-{sid}"
    user = User.query.filter_by(tg_tag=tag).first()
    if user is None:
        user = User(tg_tag=tag, role=User.ROLE_PARTICIPANT, display_name="Гость")
        user.set_password("guest")
        db.session.add(user)
        db.session.commit()
    return user


def current_user():
    """Текущий пользователь. При отключённом входе — гость этой сессии."""
    uid = session.get("user_id")
    if uid is not None:
        user = db.session.get(User, uid)
        if user is not None:
            return user
    if AUTH_DISABLED:
        return _session_user()
    return None


def login_required(view):
    """Декоратор входа. При AUTH_DISABLED пропускает всех."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not AUTH_DISABLED and current_user() is None:
            flash("Войдите в систему, чтобы продолжить.", "warning")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


@app.context_processor
def inject_user():
    """Делает current_user и активный LLM доступными во всех шаблонах.

    Имя провайдера в шапке — не украшение: сервис умеет работать с разными
    вендорами, и видеть, какой отвечает прямо сейчас, важно при разборе
    «почему агент ответил странно» или «почему ИИ недоступен».
    """
    name, _source = llm_provider.resolve_provider_name()
    return {
        "current_user": current_user(),
        "auth_disabled": AUTH_DISABLED,
        "llm_active_title": llm_provider.provider_class(name).TITLE,
        "generator_url": GENERATOR_URL,
    }


def _ensure_blank_line_around_tables(text: str) -> str:
    """Вставляет пустую строку до/после markdown-таблиц.

    Модели обычно не оставляют пустую строку между заголовком и таблицей
    (просто перенос строки) — а без неё расширение tables в Python-Markdown
    не признаёт таблицу отдельным блоком, особенно вместе с nl2br (который
    иначе превращает всю таблицу в цепочку <br> вместо <table>).
    """
    import re

    table_line = re.compile(r"^\s*\|.*\|\s*$")
    lines = text.split("\n")
    out = []
    prev_is_table = False
    for line in lines:
        is_table = bool(table_line.match(line))
        if is_table and not prev_is_table and out and out[-1].strip() != "":
            out.append("")
        if not is_table and prev_is_table and line.strip() != "":
            out.append("")
        out.append(line)
        prev_is_table = is_table
    return "\n".join(out)


@app.context_processor
def inject_job_helpers():
    """Подсказки о шагах — чтобы плашка прогресса не хардкодила их в разметке."""
    return {"job_hint": jobs.step_hint, "job_title": jobs.step_title,
            "job_retry": jobs.retry_endpoint}


@app.template_filter("markdown")
def render_markdown(text):
    """Рендерит markdown из ответа ИИ-агента в HTML (таблицы, списки, **жирный**).

    Текст — внешний ввод (ответ LLM, а через него косвенно и содержимое
    документа автора), поэтому сперва экранируем HTML-спецсимволы и только
    потом прогоняем через markdown — так `<script>`, случайно оказавшийся в
    тексте, останется безопасным текстом, а не выполнится как разметка;
    сама markdown-разметка (**, |, #, -) экранирования не боится.
    """
    import html as _html
    import markdown as _markdown

    if not text:
        return ""
    escaped = _html.escape(text)
    escaped = _ensure_blank_line_around_tables(escaped)
    return _markdown.markdown(escaped, extensions=["extra", "nl2br", "sane_lists"])


# --- фоновые задачи: статус, отмена -----------------------------------------
# Эти три маршрута — заодно и основа будущей интеграции по API: любой шаг
# конвейера ставится в очередь и опрашивается одинаково, независимо от того,
# смотрит на него человек в браузере или другой сервис.
@app.route("/jobs/<int:job_id>.json")
@login_required
def job_status(job_id):
    """Состояние одной задачи. Экран опрашивает его, пока шаг идёт."""
    job = db.session.get(Job, job_id)
    if job is None:
        abort(404)
    _owned_document(job.document_id)
    return jsonify(job.as_dict())


@app.route("/documents/<int:doc_id>/jobs/<int:game_index>.json")
@login_required
def jobs_of_game(doc_id, game_index):
    """Все задачи игры — карта состояния конвейера одним запросом."""
    _owned_document(doc_id)
    rows = (Job.query.filter_by(document_id=doc_id, game_index=game_index)
            .order_by(Job.id.asc()).all())
    return jsonify({"document_id": doc_id, "game_index": game_index,
                    "jobs": [j.as_dict() for j in rows]})


@app.route("/jobs/<int:job_id>/cancel", methods=["POST"])
@login_required
def job_cancel(job_id):
    """Просьба остановить шаг.

    Останавливает МЕЖДУ обращениями к модели, а не внутри: запрос уже ушёл, и
    ответ будет оплачен независимо от нашего желания. Об этом честно сказано на
    экране, чтобы отмена не выглядела мгновенной.
    """
    job = db.session.get(Job, job_id)
    if job is None:
        abort(404)
    document = _owned_document(job.document_id)
    if jobs.request_cancel(job_id):
        flash("Остановим после текущего обращения к модели — прервать сам запрос "
              "нельзя, он уже отправлен.", "warning")
    else:
        flash("Задача уже завершилась — останавливать нечего.", "warning")
    return redirect(request.referrer
                    or url_for("games", doc_id=document.id))


# --- API для соседних сервисов экосистемы -----------------------------------
# «Генератор игр» собирает игру по частям и после аудита каждого модуля просит
# оценить его по линзам Шелла. Владельцем линз остаётся ФинИгроСкоп: агент,
# промпт, валидатор и веса категорий живут в одном месте, иначе через месяц у
# двух сервисов будут две разные шкалы Шелла с одинаковым названием.
#
# Ответ ВСЕГДА асинхронный. Линзы читают много и отвечают до пяти минут
# (lens_evaluator.LLM_TIMEOUT = 300), а ФинИгроСкоп обязан работать одним
# воркером — синхронный ответ занял бы его целиком и положил сервис для всех.

LENS_API_TOKEN = os.environ.get("LENS_API_TOKEN", "").strip()


def _lens_api_denied():
    """Причина отказа или None. Закрыто по умолчанию — вызов стоит денег.

    Эндпоинт тратит деньги на модель, поэтому без общего секрета он работает
    ТОЛЬКО на сервере разработки. На боевом (gunicorn, debug выключен)
    незаданный LENS_API_TOKEN — это отказ, а не «пока так поработает»: открытый
    платный эндпоинт находят быстрее, чем про него вспоминают.
    """
    if LENS_API_TOKEN:
        # Сравниваем БАЙТЫ, а не строки: compare_digest на строке с любым
        # символом вне ASCII бросает TypeError, и токен с кириллицей ронял бы
        # эндпоинт в 500 вместо честного отказа. Отказ должен быть отказом при
        # любом значении, которое кто-то впишет в .env.
        supplied = request.headers.get("X-Lens-Token", "")
        if not hmac.compare_digest(supplied.encode("utf-8", "replace"),
                                   LENS_API_TOKEN.encode("utf-8", "replace")):
            return "Неверный или отсутствующий X-Lens-Token."
        return None
    if app.debug:
        return None
    return ("LENS_API_TOKEN не задан. Оценка по линзам тратит деньги на модель, "
            "поэтому без общего секрета доступна только в разработке. "
            "Задайте LENS_API_TOKEN в .env обоих сервисов.")


@app.route("/api/lenses/module", methods=["POST"])
def lens_module_submit():
    """Ставит оценку модуля в очередь. Отвечает сразу, не дожидаясь модели.

    Вход: {phase, module, params, audit}. `audit` — ответ агента-аудитора
    генератора: по нему КОД решает, звать ли линзы вообще.
    """
    denied = _lens_api_denied()
    if denied:
        return jsonify({"error": denied}), 403

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Ожидается объект JSON."}), 400

    phase = payload.get("phase")
    module = payload.get("module")
    if not isinstance(phase, str) or not phase:
        return jsonify({"error": "Нужно поле phase с именем модуля."}), 400
    if not isinstance(module, dict) or not module:
        return jsonify({"error": "Нужно поле module с содержимым модуля."}), 400

    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    audit = payload.get("audit") if isinstance(payload.get("audit"), dict) else {}

    # Условие вызова проверяем ЗДЕСЬ, а не в задаче: ответ мгновенный, и незачем
    # заводить задачу, чтобы через миг сообщить, что звать линзы было нельзя.
    gate = lens_module.should_run(audit)
    if not gate["ready"]:
        return jsonify({"ready": False, "reason": gate["reason"],
                        "blocking": gate.get("blocking") or {}}), 200

    provider = payload.get("provider") if isinstance(payload.get("provider"), str) else None
    job_id = lens_queue.submit(
        lambda progress: lens_module.evaluate(phase, module, params, audit,
                                              provider_name=provider))
    # Бюджет отдаём вместе с номером задачи. Он нужен вызывающему, чтобы
    # соразмерить своё ожидание: генератор, сдавшийся раньше, чем оценщик
    # закончил, показывает ошибку по несуществующему поводу, а работа при этом
    # продолжает идти. Заодно по этому полю видно, свежий ли код в памяти
    # сервера, — а это уже дважды обходилось дорого.
    return jsonify({"ready": True, "job_id": job_id,
                    "budget_seconds": lens_agent.TOTAL_BUDGET,
                    "status_url": url_for("lens_module_status", job_id=job_id)}), 202


@app.route("/api/lenses/module/<job_id>.json")
def lens_module_status(job_id):
    """Состояние оценки. Генератор опрашивает, пока идёт."""
    denied = _lens_api_denied()
    if denied:
        return jsonify({"error": denied}), 403

    state = lens_queue.status(job_id)
    if state is None:
        return jsonify({"error": "Задача не найдена — возможно, сервис "
                                 "перезапускался. Запросите оценку заново."}), 404
    return jsonify(state)


# --- симуляционный этап по готовому game_spec, без экранов -------------------
# Через эти два маршрута сгенерированная игра проходит весь разбор целиком:
# симуляционист → прогон → баланс → диагност → линзы → синтез → авто-редизайн.
# Экранов нет: для «Генератора игр» это внутренний шаг, а не работа автора.

# Прогон скелета — единственное место сервиса, где выполняется код, написанный
# моделью. В интерфейсе он закрыт явным нажатием человека; здесь нажимать
# некому, поэтому включается отдельной переменной и по умолчанию ВЫКЛЮЧЕН.
# Автоматическое выполнение чужого кода по сетевому запросу — это удалённое
# выполнение кода с правами сервиса, и включать его молча нельзя.
SIM_API_ALLOW_RUN = os.environ.get("SIM_API_ALLOW_RUN", "").strip() in (
    "1", "true", "yes", "on")


@app.route("/api/simulate/spec", methods=["POST"])
def simulate_spec_submit():
    """Ставит разбор игры в очередь. Отвечает сразу, не дожидаясь агентов.

    Вход: {game_spec: {...}, diagnostic_meta: {...}} — то, что собирает упаковка
    генератора. Ждать внутри запроса нельзя: цепочка идёт минутами и делает
    до полутора десятков обращений к моделям.
    """
    denied = _lens_api_denied()
    if denied:
        return jsonify({"error": denied}), 403

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Ожидается объект JSON."}), 400

    spec_root = payload.get("spec") if isinstance(payload.get("spec"), dict) else payload
    if not headless.core_of(spec_root):
        return jsonify({"error": "В запросе нет game_spec.core — разбирать нечего. "
                                 "Ожидается результат упаковки генератора."}), 400

    allow_run = SIM_API_ALLOW_RUN and bool(payload.get("run_code", True))
    provider = payload.get("provider") if isinstance(payload.get("provider"), str) else None

    # Длинная полоса. Разбор идёт десятки минут, и пускать его в общую очередь
    # с оценками модулей нельзя: те встают за ним намертво, а генератор
    # сообщает «оценка не уложилась» при полностью исправном сервисе.
    job_id = lens_queue.submit(
        lambda progress: headless.run(spec_root, progress=progress,
                                      allow_code_run=allow_run,
                                      provider_name=provider),
        lane=lens_queue.LONG)
    return jsonify({
        "job_id": job_id,
        "code_execution": allow_run,
        # Говорим прямо, а не молчим: без прогона цепочка остановится на
        # статистике, и лучше узнать об этом сейчас, чем через минуту.
        "note": None if allow_run else
                "Прогон скелета выключен (SIM_API_ALLOW_RUN). Разбор дойдёт до "
                "шага статистики и остановится.",
        "status_url": url_for("simulate_spec_status", job_id=job_id),
    }), 202


@app.route("/api/simulate/<job_id>.json")
def simulate_spec_status(job_id):
    """Состояние разбора. Генератор опрашивает, пока идёт."""
    denied = _lens_api_denied()
    if denied:
        return jsonify({"error": denied}), 403

    state = lens_queue.status(job_id)
    if state is None:
        return jsonify({"error": "Задача не найдена — возможно, сервис "
                                 "перезапускался. Запустите разбор заново."}), 404
    return jsonify(state)


# --- публичные маршруты -----------------------------------------------------
@app.route("/")
def index():
    """Приветственная страница. Доступна всем."""
    return render_template("index.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    """Вход без регистрации: telegram-ник + персональный пароль."""
    if current_user() is not None:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        tag = User.normalize_tag(request.form.get("tg_tag", ""))
        password = request.form.get("password", "")

        user = User.query.filter_by(tg_tag=tag).first()
        if user is None or not user.check_password(password):
            flash("Неверный ник или пароль.", "error")
            return render_template("login.html", tg_tag=tag)

        session["user_id"] = user.id
        flash(f"Добро пожаловать, {user.tg_tag}!", "success")
        next_url = request.args.get("next") or url_for("dashboard")
        return redirect(next_url)

    return render_template("login.html", tg_tag="")


@app.route("/logout")
def logout():
    session.pop("user_id", None)
    flash("Вы вышли из системы.", "success")
    return redirect(url_for("index"))


# --- защищённые маршруты ----------------------------------------------------
@app.route("/dashboard")
@login_required
def dashboard():
    """Личный кабинет: загрузка документа и список ранее загруженных."""
    user = current_user()
    docs = (
        Document.query.filter_by(user_id=user.id)
        .order_by(Document.uploaded_at.desc())
        .all()
    )
    return render_template("dashboard.html", documents=docs, doc_types=DOC_TYPES)


@app.route("/upload", methods=["POST"])
@login_required
def upload():
    """Приём .docx: сохраняем документ и ведём к выбору игры (БЕЗ анализа).

    Полный документный анализ здесь НЕ запускается — сначала файл сегментируется
    на отдельные игры, а анализ конкретной игры пользователь запускает сам.
    """
    user = current_user()
    doc_type = request.form.get("doc_type", "")
    if doc_type not in DOC_TYPES:
        flash("Выберите тип документа (эссе или карточка идей).", "error")
        return redirect(url_for("dashboard"))

    file = request.files.get("file")
    if not file or not file.filename:
        flash("Файл не выбран.", "error")
        return redirect(url_for("dashboard"))
    if not file.filename.lower().endswith(".docx"):
        flash("Поддерживаются только файлы .docx.", "error")
        return redirect(url_for("dashboard"))

    raw = file.read()
    file_hash = hashlib.sha256(raw).hexdigest()
    safe_name = secure_filename(file.filename) or "document.docx"
    stored_path = os.path.join(UPLOAD_DIR, f"{file_hash[:16]}_{safe_name}")
    with open(stored_path, "wb") as fh:
        fh.write(raw)

    version = (
        Document.query.filter_by(user_id=user.id, filename=file.filename).count() + 1
    )
    document = Document(
        user_id=user.id,
        filename=file.filename,
        stored_path=stored_path,
        file_hash=file_hash,
        doc_type=doc_type,
        version=version,
    )
    db.session.add(document)
    db.session.commit()

    return redirect(url_for("games", doc_id=document.id))


@app.route("/documents/<int:doc_id>/games")
@login_required
def games(doc_id):
    """Сегментация документа на игры и выбор, какую анализировать."""
    document = _owned_document(doc_id)
    try:
        seg = stage1.segment(document.stored_path, document.doc_type)
    except Exception as exc:
        flash(f"Не удалось разобрать документ: {exc}", "error")
        return redirect(url_for("dashboard"))
    return render_template("select_game.html", document=document, seg=seg,
                           doc_types=DOC_TYPES)


@app.route("/documents/<int:doc_id>/report/<int:game_index>")
@login_required
def report(doc_id, game_index):
    """Документный анализ ВЫБРАННОЙ игры (запускается по требованию)."""
    document = _owned_document(doc_id)

    result = stage1.analyze(document.stored_path, document.doc_type, use_semantics=False)
    game = next((g for g in result.get("games", []) if g["index"] == game_index), None)
    if game is None:
        flash("Игра не найдена в документе.", "error")
        return redirect(url_for("games", doc_id=doc_id))

    # кэшируем отчёт этапа 1 по игре
    db.session.add(Report(
        document_id=doc_id, stage="1", status="done",
        result_json=json.dumps({"game_index": game_index, "game": game}, ensure_ascii=False),
    ))
    db.session.commit()

    # отдаём report.html с единственной выбранной игрой
    single = {
        "doc_type": result["doc_type"], "doc_type_title": result["doc_type_title"],
        "games_count": result["games_count"], "paragraphs_total": result["paragraphs_total"],
        "strict": result.get("strict"), "formatting": result.get("formatting"),
        "games": [game],
    }
    return render_template("report.html", document=document, report=single)


@app.route("/documents/<int:doc_id>/mirror/<int:game_index>")
@login_required
def mirror(doc_id, game_index):
    """Экран диалога с агентом «Зеркало понимания» — шаг между этапами 1 и 2.

    Доступен только если у выбранной игры нет критичных пробелов (can_simulate).
    На первый визит сессия создаётся и сразу запускается проход 1; повторные
    визиты просто показывают текущее состояние диалога.
    """
    document = _owned_document(doc_id)

    result = stage1.analyze(document.stored_path, document.doc_type, use_semantics=False)
    game = next((g for g in result.get("games", []) if g["index"] == game_index), None)
    if game is None:
        flash("Игра не найдена в документе.", "error")
        return redirect(url_for("games", doc_id=doc_id))
    if not game.get("can_simulate"):
        flash("Сначала заполните ключевые разделы — без них диалог с агентом недоступен.", "warning")
        return redirect(url_for("report", doc_id=doc_id, game_index=game_index))

    ms = MirrorSession.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if ms is None:
        ms = MirrorSession(document_id=doc_id, game_index=game_index)
        db.session.add(ms)
        db.session.commit()

    if (ms.phase == MirrorSession.PHASE_PENDING
            and jobs.may_autostart(doc_id, game_index, "mirror", ms.created_at)):
        jobs.submit(doc_id, game_index, "mirror",
                    _mirror_pass1_job(document, game_index))

    return render_template("mirror.html", document=document, game=game,
                           game_index=game_index, ms=ms,
                           job=jobs.latest(doc_id, game_index, "mirror"))


def _mirror_pass1_job(document, game_index):
    """Тело фоновой задачи прохода 1. Внутри — тот же код, что был синхронным."""
    doc_id = document.id
    stored_path, doc_type = document.stored_path, document.doc_type

    def run(job_id=None):
        ms = MirrorSession.query.filter_by(document_id=doc_id, game_index=game_index).first()
        game_text = stage1.game_text_for_agent(stored_path, doc_type, game_index)
        outcome = mirror_agent.run_pass(game_text, round_no=ms.round,
                                        max_rounds=MirrorSession.MAX_ROUNDS)
        _apply_mirror_outcome(ms, outcome, phase_on_success=MirrorSession.PHASE_MIRROR)

    return run


@app.route("/documents/<int:doc_id>/mirror/<int:game_index>/reply", methods=["POST"])
@login_required
def mirror_reply(doc_id, game_index):
    """Ответ автора → очередной раунд сверки (не более MAX_ROUNDS).

    Агент может переспросить, если ответы закрыли не всё, но на последнем
    раунде обязан завершить — а мы дополнительно страхуем это на своей стороне,
    принудительно переводя сессию в confirmed. Так цикл конечен даже если
    модель проигнорирует инструкцию.
    """
    document = _owned_document(doc_id)

    ms = MirrorSession.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if ms is None or ms.phase != MirrorSession.PHASE_MIRROR:
        flash("Сначала дождитесь первого ответа агента.", "error")
        return redirect(url_for("mirror", doc_id=doc_id, game_index=game_index))

    answer = (request.form.get("answer") or "").strip()
    if not answer:
        flash("Введите ответ (или напишите «всё ясно, продолжаем»).", "error")
        return redirect(url_for("mirror", doc_id=doc_id, game_index=game_index))

    ms.author_answer = answer
    db.session.commit()

    stored_path, doc_type = document.stored_path, document.doc_type

    def run(job_id=None):
        session = MirrorSession.query.filter_by(
            document_id=doc_id, game_index=game_index).first()
        was_final = session.is_final_round
        game_text = stage1.game_text_for_agent(stored_path, doc_type, game_index)
        outcome = mirror_agent.run_pass(
            game_text, prior_json=session.last_json_dict(), author_answer=answer,
            round_no=session.round, max_rounds=MirrorSession.MAX_ROUNDS)

        if not outcome.get("available"):
            session.error = outcome.get("error") or "ИИ-агент недоступен."
            return

        data = outcome.get("json") or {}
        wants_more = (not data.get("ready_to_proceed")) and bool(data.get("questions"))
        # Ещё раунд — только если агент реально просит и лимит не исчерпан.
        if wants_more and not was_final:
            session.round += 1
            _apply_mirror_outcome(session, outcome,
                                  phase_on_success=MirrorSession.PHASE_MIRROR)
        else:
            _apply_mirror_outcome(session, outcome,
                                  phase_on_success=MirrorSession.PHASE_CONFIRMED)
            if was_final and wants_more:
                # Модель проигнорировала запрет на новые вопросы — закрываем сами.
                session.ready_to_proceed = True

    jobs.submit(doc_id, game_index, "mirror", run)
    return redirect(url_for("mirror", doc_id=doc_id, game_index=game_index))


@app.route("/documents/<int:doc_id>/mirror/<int:game_index>/retry", methods=["POST"])
@login_required
def mirror_retry(doc_id, game_index):
    """Прогнать последний проход заново — тот же раунд, бюджет не тратится.

    Единственный экран конвейера, где сбой раньше не отыгрывался. Ответ агента
    без машинного блока — не ошибка провайдера: вызов формально удачен, поэтому
    `ms.error` пуст, вопросов в ответе нет, и оркестратор честно закрывает
    сверку. Автор при этом остаётся без карты понимания и без заданных вопросов,
    а вернуться ему потом неоткуда: форма ответа исчезла вместе с фазой.

    Ответ автора сохраняется и подаётся снова — переспрашивать его повторно
    было бы наказанием за сбой на нашей стороне.

    Работает и на самом первом проходе (фаза `pending`). Раньше он здесь
    отказывал: перезапуском служило простое открытие экрана. Теперь экран
    ничего не запускает сам (см. jobs.may_autostart), и без этой ветки сбой
    первого прохода стал бы тупиком без единого выхода.
    """
    document = _owned_document(doc_id)
    ms = MirrorSession.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if ms is None:
        flash("Сверка ещё не начиналась.", "error")
        return redirect(url_for("mirror", doc_id=doc_id, game_index=game_index))

    stored_path, doc_type = document.stored_path, document.doc_type

    def run(job_id=None):
        session = MirrorSession.query.filter_by(
            document_id=doc_id, game_index=game_index).first()
        game_text = stage1.game_text_for_agent(stored_path, doc_type, game_index)
        outcome = mirror_agent.run_pass(
            game_text, prior_json=session.last_json_dict(),
            author_answer=session.author_answer,
            round_no=session.round, max_rounds=MirrorSession.MAX_ROUNDS)

        data = outcome.get("json") or {}
        # Фазу выбираем по тому же правилу, что и в обычном ходе: есть вопросы и
        # агент не закончил — ждём автора, иначе сверка закрыта.
        wants_more = (not data.get("ready_to_proceed")) and bool(data.get("questions"))
        _apply_mirror_outcome(
            session, outcome,
            phase_on_success=MirrorSession.PHASE_MIRROR if wants_more
            else MirrorSession.PHASE_CONFIRMED)

    jobs.submit(doc_id, game_index, "mirror", run)
    return redirect(url_for("mirror", doc_id=doc_id, game_index=game_index))


@app.route("/documents/<int:doc_id>/spec/<int:game_index>")
@login_required
def spec(doc_id, game_index):
    """Извлечение структуры игры (game_spec.json) — шаг после гейта понимания."""
    document = _owned_document(doc_id)

    ms = MirrorSession.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if ms is None or ms.phase != MirrorSession.PHASE_CONFIRMED:
        flash("Сначала завершите стадию понимания игры.", "warning")
        return redirect(url_for("mirror", doc_id=doc_id, game_index=game_index))

    gs = GameSpec.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if gs is None:
        gs = GameSpec(document_id=doc_id, game_index=game_index)
        db.session.add(gs)
        db.session.commit()

    if (gs.spec_json is None
            and jobs.may_autostart(doc_id, game_index, "extraction", gs.created_at)):
        jobs.submit(doc_id, game_index, "extraction",
                    _extraction_job(document, game_index))

    # Критичные пробелы — вход второго гейта: их поднимает не автор, а машина,
    # и закрыть их может только агент понимания (единственный, кто спрашивает).
    gate2_gaps = extractor_agent.critical_gaps(gs.spec_dict())
    return render_template("spec.html", document=document, game_index=game_index,
                           gs=gs, ms=ms, describe=spec_describe,
                           gate2_gaps=gate2_gaps,
                           job=jobs.latest(doc_id, game_index, "extraction"))


@app.route("/documents/<int:doc_id>/spec/<int:game_index>/retry", methods=["POST"])
@login_required
def spec_retry(doc_id, game_index):
    """Прогнать извлечение заново — после сбоя или смены модели.

    Кнопка обязательна именно потому, что экран больше не запускает шаг сам
    (см. jobs.may_autostart): без неё сбой извлечения стал бы тупиком, из
    которого автор не выбрался бы вообще.
    """
    document = _owned_document(doc_id)
    gs = GameSpec.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if gs is None:
        gs = GameSpec(document_id=doc_id, game_index=game_index)
        db.session.add(gs)
    gs.spec_json = gs.issues_json = None
    gs.error = None
    db.session.commit()
    jobs.submit(doc_id, game_index, "extraction", _extraction_job(document, game_index))
    return redirect(url_for("spec", doc_id=doc_id, game_index=game_index))


def _extraction_job(document, game_index):
    """Тело фоновой задачи извлечения."""
    doc_id = document.id

    def run(job_id=None):
        ms = MirrorSession.query.filter_by(document_id=doc_id, game_index=game_index).first()
        gs = GameSpec.query.filter_by(document_id=doc_id, game_index=game_index).first()
        _run_extraction(document, ms, gs, game_index)

    return run


def _run_extraction(document, ms, gs, game_index):
    """Прогон извлеченца по подтверждённому материалу гейта понимания.

    Карта узлов подаётся отдельно (`ms.map_list()`): в подтверждённом JSON
    прохода 2 её уже нет, а без неё извлеченец не отличит absent («механики
    нет») от missing («не описано»).

    Канонический текст тоже подаётся отдельно — по нему код восстанавливает
    `text.full_rules`, если модель его сократила. Копию делает код, а не модель.
    """
    game_text = stage1.game_text_for_agent(document.stored_path, document.doc_type, game_index)
    confirmed = extractor_agent.build_confirmed_input(
        ms.last_json_dict(), game_text, ms.author_answer, node_map=ms.map_list())
    outcome = extractor_agent.run(confirmed, doc_type=document.doc_type,
                                  canonical_text=game_text)
    if outcome.get("available"):
        gs.error = None
        gs.spec_json = json.dumps(outcome["spec"], ensure_ascii=False)
        gs.issues_json = json.dumps(outcome.get("issues") or [], ensure_ascii=False)
    else:
        gs.error = outcome.get("error") or "Извлеченец недоступен."


@app.route("/documents/<int:doc_id>/gate2/<int:game_index>")
@login_required
def gate2(doc_id, game_index):
    """Второй гейт (проход 3 понимателя): точечные вопросы по критичным пробелам.

    Извлеченец при структурировании видит пробелы, незаметные на уровне прозы
    (например, что порог победы так и не назван). Агент понимания — единственный
    шаг конвейера, который может спросить автора, поэтому критичные пробелы
    возвращаются именно ему, а не домысливаются дальше.

    Гейт однократный и открывается только при наличии критичных пробелов.
    """
    document = _owned_document(doc_id)
    ms, gs = _gate2_context(doc_id, game_index)

    gaps = extractor_agent.critical_gaps(gs.spec_dict())
    if not gaps:
        flash("Критичных пробелов нет — второй гейт не нужен.", "success")
        return redirect(url_for("spec", doc_id=doc_id, game_index=game_index))
    if not ms.can_gate2 and ms.gate2_status != MirrorSession.GATE2_ASKED:
        flash("Второй гейт уже пройден — он открывается один раз.", "warning")
        return redirect(url_for("spec", doc_id=doc_id, game_index=game_index))

    # Обращение А: агент формулирует точечные вопросы (или честно говорит, что
    # спрашивать нечего — всё уже закрыто на проходах 1-2).
    if (ms.gate2_status == MirrorSession.GATE2_NONE
            and jobs.may_autostart(doc_id, game_index, "gate2", ms.created_at)):
        jobs.submit(doc_id, game_index, "gate2", _gate2_ask_job(document, game_index))
        ms = MirrorSession.query.filter_by(
            document_id=doc_id, game_index=game_index).first()

    return render_template("gate2.html", document=document, game_index=game_index,
                           ms=ms, gs=gs, gaps=gaps,
                           job=jobs.latest(doc_id, game_index, "gate2"))


def _gate2_ask_job(document, game_index):
    """Обращение А второго гейта: агент формулирует точечные вопросы."""
    doc_id = document.id

    def run(job_id=None):
        ms = MirrorSession.query.filter_by(
            document_id=doc_id, game_index=game_index).first()
        gs = GameSpec.query.filter_by(
            document_id=doc_id, game_index=game_index).first()
        gaps = extractor_agent.critical_gaps(gs.spec_dict())
        game_text = stage1.game_text_for_agent(
            document.stored_path, document.doc_type, game_index)
        outcome = mirror_agent.run_gate2(
            game_text, ms.last_json_dict(), gaps,
            ambiguities=extractor_agent.ambiguities(gs.spec_dict()))
        if not outcome.get("available"):
            ms.gate2_error = outcome.get("error") or "ИИ-агент недоступен."
        else:
            data = outcome.get("json") or {}
            ms.gate2_error = None
            ms.gate2_text = outcome.get("text")
            ms.gate2_json = json.dumps(data, ensure_ascii=False)
            ms.gate2_runs += 1
            # Вопросов нет — гейт закрываем сразу, автора не тревожим.
            ms.gate2_status = (MirrorSession.GATE2_ASKED if data.get("questions")
                               else MirrorSession.GATE2_DONE)

    return run


@app.route("/documents/<int:doc_id>/gate2/<int:game_index>/retry", methods=["POST"])
@login_required
def gate2_retry(doc_id, game_index):
    """Прогнать обращение А гейта заново после сбоя.

    Бюджет гейта при этом НЕ тратится: `gate2_runs` растёт только когда агент
    действительно ответил. Сбой вызова — не использованная попытка, и списывать
    её значило бы наказывать автора за недоступность модели.
    """
    document = _owned_document(doc_id)
    ms = MirrorSession.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if ms is None or ms.gate2_status != MirrorSession.GATE2_NONE:
        flash("Гейт уже открыт — прогонять заново нечего.", "warning")
        return redirect(url_for("gate2", doc_id=doc_id, game_index=game_index))
    ms.gate2_error = None
    db.session.commit()
    jobs.submit(doc_id, game_index, "gate2", _gate2_ask_job(document, game_index))
    return redirect(url_for("gate2", doc_id=doc_id, game_index=game_index))


@app.route("/documents/<int:doc_id>/gate2/<int:game_index>/reply", methods=["POST"])
@login_required
def gate2_reply(doc_id, game_index):
    """Ответ автора на точечные вопросы гейта → пересборка структуры.

    Ответы гейта вливаются в ПОДТВЕРЖДЁННЫЙ ТЕКСТ и извлечение прогоняется
    заново: иначе извлеченец увидел бы тот же неполный материал и воспроизвёл
    тот же пробел.
    """
    document = _owned_document(doc_id)
    ms, gs = _gate2_context(doc_id, game_index)

    if ms.gate2_status != MirrorSession.GATE2_ASKED:
        flash("Сначала дождитесь вопросов второго гейта.", "error")
        return redirect(url_for("gate2", doc_id=doc_id, game_index=game_index))

    answer = (request.form.get("answer") or "").strip()
    if not answer:
        flash("Введите ответ (или напишите, что уточнить не получится).", "error")
        return redirect(url_for("gate2", doc_id=doc_id, game_index=game_index))

    def run(job_id=None):
        ms_ = MirrorSession.query.filter_by(
            document_id=doc_id, game_index=game_index).first()
        gs_ = GameSpec.query.filter_by(
            document_id=doc_id, game_index=game_index).first()
        gaps = extractor_agent.critical_gaps(gs_.spec_dict())
        asked = (ms_.gate2_json_dict() or {}).get("questions") or []
        game_text = stage1.game_text_for_agent(
            document.stored_path, document.doc_type, game_index)
        outcome = mirror_agent.run_gate2(
            game_text, ms_.last_json_dict(), gaps,
            ambiguities=extractor_agent.ambiguities(gs_.spec_dict()),
            author_answer=answer, asked_questions=asked)

        if not outcome.get("available"):
            ms_.gate2_error = outcome.get("error") or "ИИ-агент недоступен."
            return

        data = outcome.get("json") or {}
        ms_.gate2_error = None
        ms_.gate2_answer = answer
        ms_.gate2_text = outcome.get("text")
        ms_.gate2_json = json.dumps(data, ensure_ascii=False)
        ms_.gate2_status = MirrorSession.GATE2_DONE
        if data.get("simulation_blocking") is not None:
            ms_.simulation_blocking = bool(data.get("simulation_blocking"))

        # Ответы гейта — полноправные уточнения автора: дописываем их в
        # подтверждённый материал прохода 2 и пересобираем структуру.
        confirmed = ms_.last_json_dict() or {}
        ms_.last_json = json.dumps(
            extractor_agent.merge_gate2_answers(confirmed, data, answer),
            ensure_ascii=False)
        db.session.commit()

        # Между двумя обращениями к модели — естественная точка отмены.
        if job_id is not None:
            jobs.check_cancelled(job_id)

        gs_.spec_json = None
        _run_extraction(document, ms_, gs_, game_index)

    jobs.submit(doc_id, game_index, "gate2", run)
    flash("Ответы приняты — пересобираем структуру.", "success")
    return redirect(url_for("spec", doc_id=doc_id, game_index=game_index))


def _gate2_context(doc_id, game_index):
    """Сессия понимания и структура игры — с проверками, что гейт вообще применим."""
    ms = MirrorSession.query.filter_by(document_id=doc_id, game_index=game_index).first()
    gs = GameSpec.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if ms is None or ms.phase != MirrorSession.PHASE_CONFIRMED:
        flash("Сначала завершите стадию понимания игры.", "warning")
        abort(redirect(url_for("mirror", doc_id=doc_id, game_index=game_index)))
    if gs is None or not gs.spec_json:
        flash("Сначала нужна извлечённая структура игры.", "warning")
        abort(redirect(url_for("spec", doc_id=doc_id, game_index=game_index)))
    if gs.status == GameSpec.STATUS_ACCEPTED:
        # Структура уже принята и ушла дальше — доуточнять пробелы поздно.
        flash("Структура уже принята — второй гейт больше не открывается.", "warning")
        abort(redirect(url_for("spec", doc_id=doc_id, game_index=game_index)))
    return ms, gs


@app.route("/documents/<int:doc_id>/spec/<int:game_index>/accept", methods=["POST"])
@login_required
def spec_accept(doc_id, game_index):
    """Автор принимает структуру — игра идёт дальше по конвейеру."""
    _owned_document(doc_id)
    gs = GameSpec.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if gs is None or not gs.spec_json:
        abort(404)
    gs.status = GameSpec.STATUS_ACCEPTED
    db.session.commit()
    flash("Структура игры принята.", "success")
    return redirect(url_for("spec", doc_id=doc_id, game_index=game_index))


@app.route("/documents/<int:doc_id>/spec/<int:game_index>/revise", methods=["POST"])
@login_required
def spec_revise(doc_id, game_index):
    """Единственная правка: замечание автора → этап понимания → повторное извлечение.

    Замечание отправляется агенту понимания (а не сразу извлеченцу) намеренно:
    так поправка входит в ПОДТВЕРЖДЁННЫЙ ТЕКСТ — источник истины для всех
    последующих агентов, а не остаётся локальным патчем одной структуры.
    Агент при этом новых вопросов не задаёт (см. промпт, режим правки) —
    молча учитывает и пересобирает, чтобы цикл остался конечным.
    """
    document = _owned_document(doc_id)

    gs = GameSpec.query.filter_by(document_id=doc_id, game_index=game_index).first()
    ms = MirrorSession.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if gs is None or ms is None or not gs.spec_json:
        abort(404)
    if not gs.can_revise:
        flash("Правка уже использована — она доступна один раз.", "warning")
        return redirect(url_for("spec", doc_id=doc_id, game_index=game_index))

    note = (request.form.get("note") or "").strip()
    if not note:
        flash("Опишите, что именно не так в структуре.", "error")
        return redirect(url_for("spec", doc_id=doc_id, game_index=game_index))

    def run(job_id=None):
        ms_ = MirrorSession.query.filter_by(
            document_id=doc_id, game_index=game_index).first()
        gs_ = GameSpec.query.filter_by(
            document_id=doc_id, game_index=game_index).first()
        game_text = stage1.game_text_for_agent(
            document.stored_path, document.doc_type, game_index)
        outcome = mirror_agent.run_pass(
            game_text, prior_json=ms_.last_json_dict(), author_answer=note,
            round_no=MirrorSession.MAX_ROUNDS, max_rounds=MirrorSession.MAX_ROUNDS,
            revision_note=note)

        if not outcome.get("available"):
            # Правка засчитывается только при удачном прогоне — иначе автор
            # потерял бы единственную попытку из-за сбоя сети, а не из-за
            # своего решения. Поэтому счётчик двигаем ниже, а не здесь.
            ms_.error = outcome.get("error") or "Агент недоступен, попробуйте позже."
            return

        gs_.revisions += 1
        gs_.status = GameSpec.STATUS_REVISED
        gs_.revision_note = note

        ms_.author_answer = note
        _apply_mirror_outcome(ms_, outcome, phase_on_success=MirrorSession.PHASE_CONFIRMED)
        ms_.ready_to_proceed = True  # режим правки всегда завершает сверку
        db.session.commit()

        if job_id is not None:
            jobs.check_cancelled(job_id)

        gs_.spec_json = None  # заставляем пересобрать структуру заново
        _run_extraction(document, ms_, gs_, game_index)

    jobs.submit(doc_id, game_index, "extraction", run)
    flash("Замечание принято — пересобираем структуру.", "success")
    return redirect(url_for("spec", doc_id=doc_id, game_index=game_index))


@app.route("/documents/<int:doc_id>/spec/<int:game_index>.json")
@login_required
def spec_json(doc_id, game_index):
    """Отдаёт game_spec.json файлом — вход для следующих шагов конвейера."""
    _owned_document(doc_id)
    gs = GameSpec.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if gs is None or not gs.spec_json:
        abort(404)
    return app.response_class(
        gs.spec_json,
        mimetype="application/json",
        headers={"Content-Disposition":
                 f'attachment; filename="game_spec_doc{doc_id}_game{game_index}.json"'},
    )


# --- выбор LLM-провайдера ---------------------------------------------------
@app.route("/settings/llm")
@login_required
def llm_settings_page():
    """Страница выбора LLM: какой провайдер и модель используют ИИ-агенты.

    Список вендоров не захардкожен в шаблоне — он приходит из реестра
    провайдеров (llm_provider.provider_catalog), поэтому новый провайдер
    появляется на странице сам, без правки HTML.
    """
    return render_template(
        "settings_llm.html",
        status=llm_settings.status(),
        catalog=llm_provider.provider_catalog(),
    )


@app.route("/settings/llm", methods=["POST"])
@login_required
def llm_settings_save():
    """Сохраняет выбор провайдера/модели (или сбрасывает его обратно к .env)."""
    if request.form.get("action") == "reset":
        llm_settings.clear()
        flash("Выбор сброшен — сервис снова берёт провайдера из .env.", "success")
        return redirect(url_for("llm_settings_page"))

    provider = (request.form.get("provider") or "").lower().strip()
    if provider not in llm_provider.available_providers():
        flash("Неизвестный провайдер.", "error")
        return redirect(url_for("llm_settings_page"))

    model = (request.form.get("model") or "").strip()
    llm_settings.save(provider, model)

    cls = llm_provider.provider_class(provider)
    if not cls.is_configured():
        # Не блокируем сохранение: ключ можно вписать в .env и следующим шагом.
        flash(f"Провайдер «{cls.TITLE}» выбран, но ещё не настроен — "
              f"впишите {cls.KEY_ENV or 'параметры доступа'} в .env и перезапустите сервер.",
              "warning")
    else:
        flash(f"ИИ-агенты переключены на «{cls.TITLE}»"
              + (f", модель {model}." if model else " (модель по умолчанию)."), "success")
    return redirect(url_for("llm_settings_page"))


@app.route("/settings/llm/test", methods=["POST"])
@login_required
def llm_settings_test():
    """Живая проверка связи: настоящий короткий запрос к выбранной модели.

    Кэш намеренно отключён — иначе «проверка» могла бы бодро отвечать из
    прошлого удачного вызова, когда ключ уже отозван или квота кончилась.
    """
    provider_name = (request.form.get("provider") or "").lower().strip() or None
    provider = llm_provider.get_provider(provider_name)
    resp = provider.complete(
        "Ты отвечаешь ровно одним словом.",
        "Ответь одним словом: работает",
        use_cache=False,
    )
    where = f"{provider.name}" + (f" · {resp.model or provider.model}" if (resp.model or provider.model) else "")
    if resp.available:
        flash(f"✅ Связь есть ({where}). Ответ модели: «{resp.text.strip()[:80]}»", "success")
    else:
        flash(f"🛑 Не отвечает ({where}): {resp.error}", "error")
    return redirect(url_for("llm_settings_page"))


@app.route("/settings/llm/models")
@login_required
def llm_settings_models():
    """Живой список моделей выбранного провайдера (для подсказки в поле «модель»).

    Именно так решается вечная проблема устаревших имён моделей: список берётся
    у самого вендора, а не из константы в коде.
    """
    name = (request.args.get("provider") or "").lower().strip() or None
    provider = llm_provider.get_provider(name)
    lister = getattr(provider, "list_models", None)
    if lister is None:
        return jsonify({"ok": False, "error": "Этот провайдер не умеет отдавать список моделей."})
    try:
        models = lister()
    except Exception as exc:  # сеть/ключ/квота — сообщаем, но не падаем
        return jsonify({"ok": False, "error": str(exc)[:300]})
    return jsonify({"ok": True, "provider": provider.name, "models": models[:200],
                    "count": len(models)})


@app.route("/documents/<int:doc_id>/skeleton/<int:game_index>")
@login_required
def skeleton(doc_id, game_index):
    """Скелет-симулятор игры (game_skeleton.py) — шаг после приёмки структуры.

    Показывает ТОЛЬКО сгенерированный агентом «Симуляционист» код скелета, без
    статистик и прочих результатов (требование автора). Сам код здесь НЕ
    исполняется — прогон в песочнице и сбор stats будут отдельным шагом.
    """
    document = _owned_document(doc_id)

    gs = GameSpec.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if gs is None or not gs.spec_json or gs.status != GameSpec.STATUS_ACCEPTED:
        flash("Сначала примите структуру игры — скелет строится по принятому game_spec.", "warning")
        return redirect(url_for("spec", doc_id=doc_id, game_index=game_index))

    sk = GameSkeleton.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if sk is None:
        sk = GameSkeleton(document_id=doc_id, game_index=game_index)
        db.session.add(sk)
        db.session.commit()

    # Прогоняем агента только если ещё не было результата (или прошлый — сбой).
    if (not sk.is_ready
            and jobs.may_autostart(doc_id, game_index, "skeleton", sk.created_at)):
        jobs.submit(doc_id, game_index, "skeleton",
                    _skeleton_job(document, game_index))

    return render_template("skeleton.html", document=document, game_index=game_index,
                           sk=sk, job=jobs.latest(doc_id, game_index, "skeleton"))


def _skeleton_job(document, game_index):
    """Тело фоновой задачи симуляциониста — самый долгий шаг: агент пишет файл кода."""
    doc_id = document.id

    def run(job_id=None):
        gs = GameSpec.query.filter_by(document_id=doc_id, game_index=game_index).first()
        sk = GameSkeleton.query.filter_by(document_id=doc_id, game_index=game_index).first()
        _run_simulation(gs, sk, document, game_index)

    return run


def _run_simulation(gs, sk, document=None, game_index=1):
    """Прогон симуляциониста по принятой структуре.

    С v6 агенту подаётся не только `core`, но и `diagnostic_meta` с каноническим
    текстом: по сайдкару он решает, что класть в snapshot_metric, а что в
    snapshot_resources, выразим ли сговор и нужен ли тай-брейк. Без сайдкара всё
    это приходилось угадывать, и ошибка не проявлялась до самого отчёта.
    """
    root = gs.spec_dict() or {}
    spec = root.get("game_spec") or {}
    core = spec.get("core") or {}
    canonical = None
    if document is not None:
        canonical = stage1.game_text_for_agent(
            document.stored_path, document.doc_type, game_index)

    outcome = simulationist_agent.run(core, diagnostic_meta=root.get("diagnostic_meta"),
                                      canonical_text=canonical)
    if not outcome.get("available"):
        sk.error = outcome.get("error") or "Симуляционист недоступен."
        return
    meta = outcome.get("meta") or {}
    sk.error = None
    sk.simulatable = bool(outcome.get("simulatable"))
    sk.meta_json = json.dumps(meta, ensure_ascii=False)
    sk.issues_json = json.dumps(outcome.get("issues") or [], ensure_ascii=False)
    if sk.simulatable:
        sk.code = outcome.get("code")
        sk.player_counts_json = json.dumps(meta.get("player_counts") or [], ensure_ascii=False)
        sk.assumptions_json = json.dumps(meta.get("assumptions") or [], ensure_ascii=False)
        sk.reason = None
        sk.missing_json = None
    else:
        sk.code = None
        sk.reason = outcome.get("reason")
        sk.missing_json = json.dumps(outcome.get("missing") or [], ensure_ascii=False)


@app.route("/documents/<int:doc_id>/skeleton/<int:game_index>/retry", methods=["POST"])
@login_required
def skeleton_retry(doc_id, game_index):
    """Повторный прогон симуляциониста (после сбоя провайдера или по желанию автора)."""
    document = _owned_document(doc_id)
    gs = GameSpec.query.filter_by(document_id=doc_id, game_index=game_index).first()
    sk = GameSkeleton.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if gs is None or gs.status != GameSpec.STATUS_ACCEPTED:
        abort(404)
    if sk is None:
        sk = GameSkeleton(document_id=doc_id, game_index=game_index)
        db.session.add(sk)
        db.session.commit()
    jobs.submit(doc_id, game_index, "skeleton", _skeleton_job(document, game_index))
    return redirect(url_for("skeleton", doc_id=doc_id, game_index=game_index))


@app.route("/documents/<int:doc_id>/balance/<int:game_index>")
@login_required
def balance(doc_id, game_index):
    """Оценка технического баланса по статистике симуляции (шаг 4).

    Экран совмещает две вещи: приём STATS_JSON и вердикт агента. Пока
    статистики нет — показывается, как её получить; как только она есть,
    запускается «Оценщик статистик».
    """
    document = _owned_document(doc_id)
    sk, br = _balance_context(doc_id, game_index)

    if (br.has_stats and not br.report_json and not br.error
            and jobs.may_autostart(doc_id, game_index, "balance", br.created_at)):
        jobs.submit(doc_id, game_index, "balance",
                    lambda job_id=None: _run_stats_evaluation(
                        doc_id, game_index, _balance_report(doc_id, game_index)))

    gs = GameSpec.query.filter_by(document_id=doc_id, game_index=game_index).first()
    root = gs.spec_dict() if gs else {}
    return render_template(
        "balance.html", document=document, game_index=game_index, sk=sk, br=br,
        breakdown=stats_agent.reachability_breakdown(br.stats() or []),
        # Именно ИМЕНА действий: `assumptions` симуляциониста — это свободные
        # фразы («исход питча смоделирован случайно»), и в списке действий им
        # не место, иначе автор видит предложение вместо идентификатора.
        soft=stats_agent.soft_actions((root or {}).get("diagnostic_meta"),
                                      _subjective_actions(root, sk)),
        job=jobs.latest(doc_id, game_index, "balance"),
    )


def _balance_report(doc_id, game_index):
    """Запись отчёта о балансе — берётся заново внутри задачи, своей сессией."""
    return BalanceReport.query.filter_by(
        document_id=doc_id, game_index=game_index).first()


@app.route("/documents/<int:doc_id>/balance/<int:game_index>/stats", methods=["POST"])
@login_required
def balance_stats(doc_id, game_index):
    """Приём STATS_JSON: вставкой из буфера либо локальным прогоном скелета."""
    document = _owned_document(doc_id)
    sk, br = _balance_context(doc_id, game_index)

    if request.form.get("action") == "run_local":
        # Единственное место, где сервис выполняет код, написанный моделью, и
        # только по явному нажатию автора — см. предупреждения в simulation/runner.
        outcome = sim_runner.run_skeleton(sk.code)
        if not outcome.get("ok"):
            br.error = outcome.get("error") or "Не удалось прогнать скелет."
            db.session.commit()
            flash("Прогон скелета не удался — подробности на странице.", "error")
            return redirect(url_for("balance", doc_id=doc_id, game_index=game_index))
        stats, source = outcome["stats"], BalanceReport.SOURCE_LOCAL
        diag = outcome.get("diag")
    else:
        # Принимаем ВЕСЬ вывод консоли, а не вырезанный кусок: у скелета два
        # блока, и просьба скопировать «нужный» стабильно теряла DIAG_JSON.
        parsed = sim_runner.parse_pasted_output(request.form.get("stats"))
        if parsed["error"]:
            flash(parsed["error"], "error")
            return redirect(url_for("balance", doc_id=doc_id, game_index=game_index))
        stats, diag = parsed["stats"], parsed["diag"]
        source = BalanceReport.SOURCE_PASTED
        if diag is None:
            flash("Статистика принята, но блока DIAG_JSON в тексте не было. Числовая "
                  "оценка пройдёт полностью, а вот диагносту не хватит данных для части "
                  "тестов — они получат честное «не выполнен». Если DIAG_JSON есть в "
                  "выводе, вставьте текст целиком.", "warning")

    br.stats_json = json.dumps(stats, ensure_ascii=False)
    # DIAG_JSON сохраняем отдельно: его читает агент-диагност, «Оценщику
    # статистик» он не подаётся. None (вставка вручную) — это не пустота, а
    # отсутствие данных, и дальше по конвейеру оно даёт честный n/a.
    br.diag_json = json.dumps(diag, ensure_ascii=False) if diag else None
    br.stats_source = source
    br.error = None
    # Новая статистика — прежний вердикт по ней недействителен.
    br.report_json = None
    br.issues_json = None
    db.session.commit()

    # Оценку заказываем ЗДЕСЬ, а не на экране. Экран запускает шаг сам только
    # один раз (jobs.may_autostart) — иначе сбой крутился бы по кругу, тратя
    # обращения к модели. А новая статистика — явное действие автора, ровно то,
    # по чему шаг и должен запускаться повторно.
    jobs.submit(doc_id, game_index, "balance",
                lambda job_id=None: _run_stats_evaluation(
                    doc_id, game_index, _balance_report(doc_id, game_index)))

    return redirect(url_for("balance", doc_id=doc_id, game_index=game_index))


@app.route("/documents/<int:doc_id>/balance/<int:game_index>/retry", methods=["POST"])
@login_required
def balance_retry(doc_id, game_index):
    """Переоценить ту же статистику заново (после сбоя или смены провайдера)."""
    _owned_document(doc_id)
    _sk, br = _balance_context(doc_id, game_index)
    if not br.has_stats:
        flash("Сначала нужна статистика прогона.", "warning")
        return redirect(url_for("balance", doc_id=doc_id, game_index=game_index))
    jobs.submit(doc_id, game_index, "balance",
                lambda job_id=None: _run_stats_evaluation(
                    doc_id, game_index, _balance_report(doc_id, game_index)))
    return redirect(url_for("balance", doc_id=doc_id, game_index=game_index))


@app.route("/documents/<int:doc_id>/balance/<int:game_index>.json")
@login_required
def balance_json(doc_id, game_index):
    """Отдаёт Finding_balance.json файлом — вход редизайнера, диагноста и синтеза."""
    _owned_document(doc_id)
    br = BalanceReport.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if br is None or not br.report_json:
        abort(404)
    return app.response_class(
        br.report_json, mimetype="application/json",
        headers={"Content-Disposition":
                 f'attachment; filename="finding_balance_doc{doc_id}_game{game_index}.json"'},
    )


@app.route("/documents/<int:doc_id>/diagnost/<int:game_index>")
@login_required
def diagnost(doc_id, game_index):
    """Агент-диагност: исполнение методички балансной верификации (шаг 5).

    Двухпроходный. На первый визит запускается только ТРИАЖ — вердиктов там нет
    и быть не должно: часть тестов невыполнима без дополнительных прогонов, а
    заказать их можно лишь разобравшись, какие тесты вообще применимы.
    """
    document = _owned_document(doc_id)
    br, sk, dr = _diagnost_context(doc_id, game_index)

    if (dr.phase == DiagnosisReport.PHASE_NONE and not dr.error
            and jobs.may_autostart(doc_id, game_index, "diagnost_triage", dr.created_at)):
        jobs.submit(doc_id, game_index, "diagnost_triage",
                    _triage_job(document, game_index))

    # На экране показываем ту задачу, которая сейчас имеет значение: пока идёт
    # триаж — его, после него — проход вердиктов.
    job = (jobs.active(doc_id, game_index, "diagnost_triage")
           or jobs.active(doc_id, game_index, "diagnost_findings")
           or jobs.latest(doc_id, game_index, "diagnost_findings")
           or jobs.latest(doc_id, game_index, "diagnost_triage"))
    return render_template(
        "diagnost.html", document=document, game_index=game_index, dr=dr, br=br, sk=sk,
        registry_size=len(diagnost_agent.registry_ids()),
        route=diagnost_agent.route(dr.findings()) if dr.findings() else None,
        job=job,
    )


def _triage_job(document, game_index):
    doc_id = document.id

    def run(job_id=None):
        br, sk, dr = _diagnost_records(doc_id, game_index)
        _run_triage(document, game_index, br, sk, dr)

    return run


def _diagnost_records(doc_id, game_index):
    """Три записи диагноста, взятые заново — внутри задачи своя сессия базы."""
    br = BalanceReport.query.filter_by(document_id=doc_id, game_index=game_index).first()
    sk = GameSkeleton.query.filter_by(document_id=doc_id, game_index=game_index).first()
    dr = DiagnosisReport.query.filter_by(document_id=doc_id, game_index=game_index).first()
    return br, sk, dr


@app.route("/documents/<int:doc_id>/diagnost/<int:game_index>/execute", methods=["POST"])
@login_required
def diagnost_execute(doc_id, game_index):
    """Исполняет заказанные прогоны и запускает проход 2.

    К симуляционисту повторно не обращаемся: код скелета уже есть, доп. прогон —
    это запись в RUN_PLAN и перезапуск файла.
    """
    document = _owned_document(doc_id)
    br, sk, dr = _diagnost_context(doc_id, game_index)

    if dr.phase == DiagnosisReport.PHASE_NONE:
        flash("Сначала нужен триаж.", "warning")
        return redirect(url_for("diagnost", doc_id=doc_id, game_index=game_index))

    def run(job_id=None):
        br_, sk_, dr_ = _diagnost_records(doc_id, game_index)
        extra = None
        dr_.runs_error = None
        requests = dr_.kept_requests()
        if requests:
            outcome = sim_runner.run_extra_runs(sk_.code, requests)
            if outcome.get("ok"):
                extra = outcome.get("runs") or {}
                if outcome.get("missing_runs"):
                    # id прогона обязан совпасть: заказ -> RUN_PLAN -> результат.
                    # Иначе агент на проходе 2 не сопоставит данные с тестом.
                    dr_.runs_error = ("Не вернулись результаты прогонов: "
                                      + ", ".join(outcome["missing_runs"]))
            else:
                dr_.runs_error = outcome.get("error") or "Доп. прогоны не выполнились."

        dr_.extra_runs_json = json.dumps(extra, ensure_ascii=False) if extra else None
        db.session.commit()

        # Естественная точка отмены: прогоны отработали, второй (самый долгий)
        # вызов модели ещё не ушёл. Внутри самого вызова прервать уже нельзя.
        if job_id is not None:
            jobs.check_cancelled(job_id)

        _run_findings(document, game_index, br_, sk_, dr_, extra)

    jobs.submit(doc_id, game_index, "diagnost_findings", run)
    return redirect(url_for("diagnost", doc_id=doc_id, game_index=game_index))


@app.route("/documents/<int:doc_id>/diagnost/<int:game_index>/retry", methods=["POST"])
@login_required
def diagnost_retry(doc_id, game_index):
    """Прогнать диагноста заново с чистого листа (после сбоя или смены модели)."""
    document = _owned_document(doc_id)
    br, sk, dr = _diagnost_context(doc_id, game_index)
    dr.phase = DiagnosisReport.PHASE_NONE
    dr.triage_json = dr.findings_json = dr.extra_runs_json = None
    dr.triage_issues_json = dr.issues_json = None
    dr.kept_requests_json = dr.dropped_requests_json = None
    dr.error = dr.runs_error = None
    db.session.commit()
    jobs.submit(doc_id, game_index, "diagnost_triage", _triage_job(document, game_index))
    return redirect(url_for("diagnost", doc_id=doc_id, game_index=game_index))


@app.route("/documents/<int:doc_id>/diagnost/<int:game_index>.json")
@login_required
def diagnost_json(doc_id, game_index):
    """Отдаёт Findings_diagnost.json файлом — вход редизайнера, линз и синтеза."""
    _owned_document(doc_id)
    dr = DiagnosisReport.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if dr is None or not dr.findings_json:
        abort(404)
    return app.response_class(
        dr.findings_json, mimetype="application/json",
        headers={"Content-Disposition":
                 f'attachment; filename="findings_diagnost_doc{doc_id}_game{game_index}.json"'},
    )


def _diagnost_context(doc_id, game_index):
    """Отчёт баланса, скелет и запись диагноста — с проверкой порядка вызова.

    Диагност идёт ПОСЛЕ «Оценщика статистик»: он не пересчитывает его шесть
    проверок, а ссылается на них через covered_tests. Без Finding_balance
    ссылаться не на что.
    """
    br = BalanceReport.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if br is None or not br.report_json:
        flash("Сначала нужна оценка баланса — диагност ссылается на её вердикты.", "warning")
        abort(redirect(url_for("balance", doc_id=doc_id, game_index=game_index)))

    sk = GameSkeleton.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if sk is None or not sk.code:
        flash("Сначала нужен собранный скелет-симулятор.", "warning")
        abort(redirect(url_for("skeleton", doc_id=doc_id, game_index=game_index)))

    dr = DiagnosisReport.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if dr is None:
        dr = DiagnosisReport(document_id=doc_id, game_index=game_index)
        db.session.add(dr)
        db.session.commit()
    return br, sk, dr


def _diagnost_input(document, game_index, br, sk, attempt_number=1):
    gs = GameSpec.query.filter_by(document_id=document.id, game_index=game_index).first()
    canonical = stage1.game_text_for_agent(
        document.stored_path, document.doc_type, game_index)
    return diagnost_agent.build_input(
        gs.spec_dict() if gs else {}, br.stats(), br.diag(), br.report(),
        sk.meta(), canonical_text=canonical, attempt_number=attempt_number)


def _run_triage(document, game_index, br, sk, dr):
    """Проход 1. Бюджет прогонов режет КОД, а не модель."""
    data = _diagnost_input(document, game_index, br, sk, dr.attempt_number)
    outcome = diagnost_agent.run_triage(data)
    if not outcome.get("available"):
        dr.error = outcome.get("error") or "Диагност недоступен."
        return
    dr.error = None
    dr.phase = DiagnosisReport.PHASE_TRIAGE
    dr.triage_json = json.dumps(outcome["triage"], ensure_ascii=False)
    dr.triage_issues_json = json.dumps(outcome.get("issues") or [], ensure_ascii=False)
    dr.kept_requests_json = json.dumps(outcome.get("kept_requests") or [], ensure_ascii=False)
    dr.dropped_requests_json = json.dumps(outcome.get("dropped_requests") or [],
                                          ensure_ascii=False)


def _run_findings(document, game_index, br, sk, dr, extra_runs):
    """Проход 2. Обрезанные бюджетом прогоны подаются явно — их тесты обязаны
    получить n/a: budget_exceeded, а не исчезнуть из отчёта."""
    data = _diagnost_input(document, game_index, br, sk, dr.attempt_number)
    outcome = diagnost_agent.run_findings(
        data, dr.triage(), extra_runs=extra_runs, dropped=dr.dropped_requests())
    if not outcome.get("available"):
        dr.error = outcome.get("error") or "Диагност недоступен."
        return
    dr.error = None
    dr.phase = DiagnosisReport.PHASE_DONE
    dr.findings_json = json.dumps(outcome["findings"], ensure_ascii=False)
    dr.issues_json = json.dumps(outcome.get("issues") or [], ensure_ascii=False)


@app.route("/documents/<int:doc_id>/lenses/<int:game_index>")
@login_required
def lenses(doc_id, game_index):
    """Оценщик по линзам Шелла — качественная оценка игры (шаг 6).

    Единственный шаг конвейера, который читает игру как игру, а не как набор
    чисел. Запускается сам при первом заходе, но только если диагност не оставил
    незакрытых критичных находок: пока игру чинят, оценивать её замысел рано.
    """
    document = _owned_document(doc_id)
    dr, lr = _lenses_context(doc_id, game_index)

    gate = lens_agent.should_run(dr.findings())
    if (gate["ready"] and lr.result_json is None and not lr.error
            and jobs.may_autostart(doc_id, game_index, "lenses", lr.created_at)):
        jobs.submit(doc_id, game_index, "lenses", _lenses_job(document, game_index))

    return render_template(
        "lenses.html", document=document, game_index=game_index,
        lr=lr, dr=dr, gate=gate, job=jobs.latest(doc_id, game_index, "lenses"),
        core=lens_agent.CORE, lens_names=lens_agent.lens_names(),
        core_size=len(lens_agent.CORE_LENSES),
        playtest=lens_agent.needs_playtest(_sim_meta(doc_id, game_index)),
    )


@app.route("/documents/<int:doc_id>/lenses/<int:game_index>/retry", methods=["POST"])
@login_required
def lenses_retry(doc_id, game_index):
    """Прогнать оценку по линзам заново (после сбоя или смены модели)."""
    document = _owned_document(doc_id)
    dr, lr = _lenses_context(doc_id, game_index)
    lr.result_json = lr.issues_json = None
    lr.error = None
    db.session.commit()
    jobs.submit(doc_id, game_index, "lenses", _lenses_job(document, game_index))
    return redirect(url_for("lenses", doc_id=doc_id, game_index=game_index))


def _lenses_job(document, game_index):
    doc_id = document.id

    def run(job_id=None):
        dr = DiagnosisReport.query.filter_by(
            document_id=doc_id, game_index=game_index).first()
        lr = LensReport.query.filter_by(
            document_id=doc_id, game_index=game_index).first()
        _run_lenses(document, game_index, dr, lr)

    return run


@app.route("/documents/<int:doc_id>/lenses/<int:game_index>.json")
@login_required
def lenses_json(doc_id, game_index):
    """Отдаёт Findings_lenses.json файлом — вход синтезатора и режима B."""
    _owned_document(doc_id)
    lr = LensReport.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if lr is None or not lr.result_json:
        abort(404)
    return app.response_class(
        lr.result_json, mimetype="application/json",
        headers={"Content-Disposition":
                 f'attachment; filename="findings_lenses_doc{doc_id}_game{game_index}.json"'},
    )


def _lenses_context(doc_id, game_index):
    """Вердикты диагноста и запись линз — с проверкой порядка вызова.

    Диагност обязателен: от него приходят и вопросы, требующие вердикта линз, и
    сводка непроверенного. Без них агент оценивал бы игру вслепую и молча
    пропустил бы гибридные тесты методички.
    """
    dr = DiagnosisReport.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if dr is None or not dr.findings_json:
        flash("Сначала нужны вердикты диагноста — от него приходят вопросы к линзам.",
              "warning")
        abort(redirect(url_for("diagnost", doc_id=doc_id, game_index=game_index)))

    lr = LensReport.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if lr is None:
        lr = LensReport(document_id=doc_id, game_index=game_index)
        db.session.add(lr)
        db.session.commit()
    return dr, lr


def _sim_meta(doc_id, game_index) -> dict:
    """Метаданные симуляциониста о границах модели (могут отсутствовать)."""
    sk = GameSkeleton.query.filter_by(document_id=doc_id, game_index=game_index).first()
    return (sk.meta() or {}) if sk is not None else {}


def _run_lenses(document, game_index, dr, lr):
    """Прогон агента по линзам.

    Заметки собираются из ДВУХ источников: свободные наблюдения «Оценщика
    статистик» и структурированные вопросы диагноста. Полные отчёты обоих
    агентов на вход НЕ подаются — качественная и числовая оценки сходятся
    впервые только в синтезаторе.
    """
    doc_id, game_index = document.id, game_index
    gs = GameSpec.query.filter_by(document_id=doc_id, game_index=game_index).first()
    br = BalanceReport.query.filter_by(document_id=doc_id, game_index=game_index).first()
    findings = dr.findings() or {}

    canonical = stage1.game_text_for_agent(
        document.stored_path, document.doc_type, game_index)

    data = lens_agent.build_input(
        gs.spec_dict() if gs else {},
        canonical_text=canonical,
        stats_notes=(br.report() or {}).get("notes_for_lenses") if br else None,
        diagnost_notes=findings.get("notes_for_lenses"),
        sim_meta=_sim_meta(doc_id, game_index),
        coverage_summary=findings.get("coverage_summary"),
    )

    outcome = lens_agent.run(data)
    if not outcome.get("available"):
        lr.error = outcome.get("error") or "Оценщик по линзам недоступен."
        return
    lr.error = None
    lr.result_json = json.dumps(outcome["report"], ensure_ascii=False)
    lr.issues_json = json.dumps(outcome.get("issues") or [], ensure_ascii=False)


@app.route("/documents/<int:doc_id>/synthesis/<int:game_index>")
@login_required
def synthesis(doc_id, game_index):
    """Финал: сведение оценок в общий балл и отчёт автору (шаг 6).

    Сам ничего не переоценивает — агрегирует то, что посчитали числовой оценщик,
    диагност и (когда появится) оценщик по линзам. Первый визит запускает синтез
    автоматически: все входы к этому моменту уже собраны.
    """
    document = _owned_document(doc_id)
    br, dr, sr = _synthesis_context(doc_id, game_index)

    if (sr.result_json is None and not sr.error
            and jobs.may_autostart(doc_id, game_index, "synthesis", sr.created_at)):
        jobs.submit(doc_id, game_index, "synthesis",
                    _synthesis_job(document, game_index, sr.attempt_number))

    return render_template(
        "synthesis.html", document=document, game_index=game_index,
        sr=sr, br=br, dr=dr, job=jobs.latest(doc_id, game_index, "synthesis"),
        history=_synthesis_history(doc_id, game_index),
        route=_synthesis_route(doc_id, game_index, sr),
        weights=synth_agent.CATEGORY_WEIGHTS,
        max_attempts=RedesignAttempt.MAX_ACCEPTED_ATTEMPTS,
    )


@app.route("/documents/<int:doc_id>/synthesis/<int:game_index>/retry", methods=["POST"])
@login_required
def synthesis_retry(doc_id, game_index):
    """Пересчитать текущую итерацию заново (после сбоя или смены модели).

    Именно ПЕРЕСЧИТАТЬ, а не начать новый круг: номер попытки не растёт, иначе
    неудачный вызов LLM молча съедал бы бюджет авто-редизайна.
    """
    document = _owned_document(doc_id)
    br, dr, sr = _synthesis_context(doc_id, game_index)
    sr.result_json = sr.reference_json = sr.issues_json = None
    sr.overall_score = sr.score_confidence = None
    sr.revision_required = False
    sr.error = None
    db.session.commit()
    jobs.submit(doc_id, game_index, "synthesis",
                _synthesis_job(document, game_index, sr.attempt_number))
    return redirect(url_for("synthesis", doc_id=doc_id, game_index=game_index))


def _synthesis_job(document, game_index, attempt_number):
    doc_id = document.id

    def run(job_id=None):
        br = BalanceReport.query.filter_by(
            document_id=doc_id, game_index=game_index).first()
        dr = DiagnosisReport.query.filter_by(
            document_id=doc_id, game_index=game_index).first()
        sr = (SynthesisReport.query
              .filter_by(document_id=doc_id, game_index=game_index,
                         attempt_number=attempt_number).first())
        _run_synthesis(document, game_index, br, dr, sr)

    return run


@app.route("/documents/<int:doc_id>/synthesis/<int:game_index>.json")
@login_required
def synthesis_json(doc_id, game_index):
    """Отдаёт финальный отчёт файлом — вместе с эталонным расчётом кода.

    Расчёт кладём рядом намеренно: балл обязан быть воспроизводим вручную, и
    без таблицы «балл × вес» проверить его нечем.
    """
    _owned_document(doc_id)
    sr = _latest_synthesis(doc_id, game_index)
    if sr is None or not sr.result_json:
        abort(404)
    payload = dict(sr.result() or {})
    payload["_reference"] = sr.reference()
    payload["_issues"] = sr.issues()
    return app.response_class(
        json.dumps(payload, ensure_ascii=False, indent=2), mimetype="application/json",
        headers={"Content-Disposition":
                 f'attachment; filename="synthesis_doc{doc_id}_game{game_index}.json"'},
    )


def _synthesis_context(doc_id, game_index):
    """Входы синтеза + запись текущей итерации.

    Диагност обязателен: без его coverage_summary нечем считать долю
    непроверенного, а без неё `score_confidence` — выдумка. Линзы опциональны
    (агент ещё не реализован) — их категории честно уйдут в N/A.
    """
    br = BalanceReport.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if br is None or not br.report_json:
        flash("Сначала нужна оценка баланса — синтез сводит уже готовые оценки.", "warning")
        abort(redirect(url_for("balance", doc_id=doc_id, game_index=game_index)))

    dr = DiagnosisReport.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if dr is None or not dr.findings_json:
        flash("Сначала нужны вердикты диагноста — по ним считается надёжность оценки.",
              "warning")
        abort(redirect(url_for("diagnost", doc_id=doc_id, game_index=game_index)))

    sr = _latest_synthesis(doc_id, game_index)
    # Новый круг открывается ровно тогда, когда авто-редизайн что-то применил:
    # балл меняется только вслед за изменением игры.
    expected = _synthesis_attempt_number(doc_id, game_index)
    if sr is None or sr.attempt_number != expected:
        sr = SynthesisReport(document_id=doc_id, game_index=game_index,
                             attempt_number=expected)
        db.session.add(sr)
        db.session.commit()
    return br, dr, sr


def _latest_synthesis(doc_id, game_index):
    return (SynthesisReport.query
            .filter_by(document_id=doc_id, game_index=game_index)
            .order_by(SynthesisReport.attempt_number.desc()).first())


def _synthesis_history(doc_id, game_index):
    return (SynthesisReport.query
            .filter_by(document_id=doc_id, game_index=game_index)
            .order_by(SynthesisReport.attempt_number.asc()).all())


def _synthesis_attempt_number(doc_id, game_index) -> int:
    """Номер итерации = принятых правок + 1.

    Считается по применённым правкам, а не по числу вызовов агента: неудачный
    ответ LLM не должен съедать бюджет ремонта.
    """
    applied = (RedesignAttempt.query
               .filter_by(document_id=doc_id, game_index=game_index,
                          status=RedesignAttempt.STATUS_ACCEPTED).count())
    return applied + 1


def _run_synthesis(document, game_index, br, dr, sr):
    """Один прогон синтезатора. Арифметику считает код, суждение — агент."""
    gs = GameSpec.query.filter_by(document_id=document.id, game_index=game_index).first()
    sk = GameSkeleton.query.filter_by(document_id=document.id,
                                      game_index=game_index).first()

    attempts_used = _synthesis_attempt_number(document.id, game_index) - 1
    attempts_left = max(0, RedesignAttempt.MAX_ACCEPTED_ATTEMPTS - attempts_used)

    # Из авто-редизайнера синтез забирает ровно две вещи: что тот не тронул и
    # что сознательно передал в рекомендации. Обе обязаны доехать до автора —
    # это последний шаг конвейера, потерянное здесь не увидит никто.
    not_touched, handed = [], []
    for att in _redesign_attempts(document.id, game_index):
        result = att.result() or {}
        not_touched += result.get("not_touched") or []
        handed += result.get("handed_to_recommendations") or []

    previous = None
    if sr.attempt_number > 1:
        prev = (SynthesisReport.query
                .filter_by(document_id=document.id, game_index=game_index,
                           attempt_number=sr.attempt_number - 1).first())
        previous = prev.overall_score if prev is not None else None

    sim_meta = dict(sk.meta() or {}) if sk is not None else {}
    sim_meta["subjective_actions"] = _subjective_actions(
        gs.spec_dict() if gs else {}, sk)

    data = synth_agent.build_input(
        lenses=_lenses_findings(document.id, game_index),
        finding_balance=br.report(), findings_diagnost=dr.findings(),
        stats=br.stats(), sim_meta=sim_meta,
        redesign_not_touched=not_touched, redesign_recommendations=handed,
        attempt_number=sr.attempt_number, previous_score=previous,
        attempts_left=attempts_left)

    outcome = synth_agent.run(data)
    sr.reference_json = json.dumps(outcome.get("reference"), ensure_ascii=False,
                                   default=str)
    if not outcome.get("available"):
        sr.error = outcome.get("error") or "Синтезатор недоступен."
        return

    report = outcome["report"]
    sr.error = None
    sr.result_json = json.dumps(report, ensure_ascii=False)
    sr.issues_json = json.dumps(outcome.get("issues") or [], ensure_ascii=False)
    sr.overall_score = report.get("overall_score")
    sr.score_confidence = report.get("score_confidence")
    # Решение о ревизии берём из ЭТАЛОНА, а не из ответа модели: по этому полю
    # оркестратор гонит игру на новый круг, и расхождение уже отмечено в issues.
    sr.revision_required = bool(outcome["reference"]["revision"]["required"])


def _lenses_findings(doc_id, game_index):
    """Findings_lenses.json для синтезатора — или None, если линз ещё нет.

    Возвращать None при отсутствии оценки ОБЯЗАТЕЛЬНО: синтезатор различает
    «линзы не запускались» (категории честно уходят в N/A, доверие к баллу
    падает) и «линзы отработали и ничего не нашли». Пустой объект вместо None
    превратил бы первое во второе — и балл выглядел бы полным, не будучи им.
    """
    lr = LensReport.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if lr is None or not lr.result_json:
        return None
    return lens_agent.to_synthesis(lr.result())


def _synthesis_route(doc_id, game_index, sr):
    """Что дальше по итогам синтеза — решает код под счётчиком попыток."""
    if sr is None or not sr.result_json:
        return None
    attempts_used = _synthesis_attempt_number(doc_id, game_index) - 1
    return synth_agent.route(sr.result(), sr.reference(), attempts_used,
                             RedesignAttempt.MAX_ACCEPTED_ATTEMPTS)


@app.route("/documents/<int:doc_id>/redesign/<int:game_index>")
@login_required
def redesign(doc_id, game_index):
    """Авто-редизайн: минимальная правка параметров по найденным недочётам.

    Агент вызывается УСЛОВНО — только когда в отчёте о балансе есть критичные
    флаги. Условие считает код (`redesigner.should_trigger`), а не модель:
    правка меняет структуру игры и делает недействительными скелет и
    статистику, поэтому решение «звать или нет» не отдаётся формулировке из
    ответа LLM.
    """
    document = _owned_document(doc_id)
    br, gs = _redesign_context(doc_id, game_index)

    attempts = _redesign_attempts(doc_id, game_index)
    sr = _latest_synthesis(doc_id, game_index)
    trigger = redesign_agent.should_trigger(br.report(), _synthesis_verdict(sr))
    current = next((a for a in attempts if a.status == RedesignAttempt.STATUS_PROPOSED), None)

    return render_template(
        "redesign.html", document=document, game_index=game_index,
        br=br, gs=gs, attempts=attempts, current=current, trigger=trigger, sr=sr,
        max_attempts=RedesignAttempt.MAX_ACCEPTED_ATTEMPTS,
        used=len([a for a in attempts if a.status == RedesignAttempt.STATUS_ACCEPTED]),
        # Второй бюджет — потолок расходов на подбор. Считаются ВСЕ попытки
        # любого статуса, включая отклонённые: модель звали, деньги потрачены.
        max_proposals=RedesignAttempt.MAX_PROPOSALS,
        proposals=len(attempts),
    )


def _synthesis_verdict(sr):
    """Вердикт синтезатора для авто-редизайнера: ревизия и её причины.

    Флаг берётся из колонки, которую пишет код по эталонному расчёту, а не из
    текста ответа модели — по нему открывается круг ремонта.
    """
    if sr is None or not sr.result_json:
        return None
    return {"revision_required": bool(sr.revision_required),
            "revision_reason": sr.revision_reason,
            "top_priorities": sr.top_priorities,
            "overall_score": sr.overall_score}


@app.route("/documents/<int:doc_id>/redesign/<int:game_index>/propose", methods=["POST"])
@login_required
def redesign_propose(doc_id, game_index):
    """Запрашивает у агента очередной шаг правки (предложение, не применение)."""
    document = _owned_document(doc_id)
    br, gs = _redesign_context(doc_id, game_index)

    finding = br.report()
    sr = _latest_synthesis(doc_id, game_index)
    verdict = _synthesis_verdict(sr)
    trigger = redesign_agent.should_trigger(finding, verdict)
    if not trigger["trigger"]:
        flash(f"Авто-редизайн не нужен: {trigger['reason']}.", "warning")
        return redirect(url_for("redesign", doc_id=doc_id, game_index=game_index))

    attempts = _redesign_attempts(doc_id, game_index)
    accepted = [a for a in attempts if a.status == RedesignAttempt.STATUS_ACCEPTED]

    # Два бюджета проверяются раздельно, и сообщения у них разные: «игра уже
    # менялась трижды» и «предложений сгенерировано достаточно» — это разные
    # причины остановки, и автор должен понимать, какая сработала.
    if len(accepted) >= RedesignAttempt.MAX_ACCEPTED_ATTEMPTS:
        flash(f"Исчерпан лимит правок структуры "
              f"({RedesignAttempt.MAX_ACCEPTED_ATTEMPTS}). Оставшиеся находки идут "
              "в рекомендации автору.", "warning")
        return redirect(url_for("redesign", doc_id=doc_id, game_index=game_index))
    if len(attempts) >= RedesignAttempt.MAX_PROPOSALS:
        flash(f"Исчерпан лимит предложений ({RedesignAttempt.MAX_PROPOSALS}): столько "
              "раз агент уже подбирал правку для этой игры. Ремонт не запрещён — "
              "закончился бюджет на подбор. Оставшиеся находки идут в рекомендации.",
              "warning")
        return redirect(url_for("redesign", doc_id=doc_id, game_index=game_index))

    # Незакрытое предложение заменяем новым, а не плодим параллельные.
    pending = [a for a in attempts if a.status == RedesignAttempt.STATUS_PROPOSED]
    for a in pending:
        db.session.delete(a)
    db.session.flush()

    # Номер — идентификатор записи, а не счётчик бюджета: отклонённая попытка
    # свой номер сохраняет, и переиспользовать его нельзя.
    attempt_number = RedesignAttempt.next_attempt_number(doc_id, game_index)
    spec_root = gs.spec_dict() or {}
    previous = redesign_agent.collect_previous_changes(
        [a.result() or {} for a in accepted])

    # Когда круг открыл синтезатор, режим — B: агент чинит не один сломанный
    # флаг, а то, что назвал приоритетами финальный отчёт. Вердикты диагноста
    # подаются всегда, если они есть, — иначе правка идёт по одной шестой
    # картины, которую видел числовой оценщик.
    dr = DiagnosisReport.query.filter_by(document_id=doc_id, game_index=game_index).first()
    diag_findings = None
    if dr is not None and dr.findings_json:
        diag_findings = [r for r in (dr.findings() or {}).get("results") or []
                         if r.get("verdict") in ("warning", "problem")]

    # Критику передаём только когда синтезатор ДЕЙСТВИТЕЛЬНО требует ревизии:
    # detect_mode переключается в B по самому факту её наличия, и сам по себе
    # готовый отчёт с проходным баллом не повод для полного разбора.
    critique = verdict if (verdict or {}).get("revision_required") else None

    # Запись попытки заводим СРАЗУ, до обращения к модели: номер уже занят, и
    # повторная постановка не должна выдать тот же самый. Ответ агента допишется
    # в неё, когда задача отработает.
    att = RedesignAttempt(document_id=doc_id, game_index=game_index,
                          attempt_number=attempt_number,
                          trigger_reason=trigger["reason"],
                          spec_before_json=json.dumps(spec_root, ensure_ascii=False))
    db.session.add(att)
    db.session.commit()
    att_id = att.id

    def run(job_id=None):
        outcome = redesign_agent.run(spec_root, finding, attempt_number=attempt_number,
                                     previous_changes=previous,
                                     findings_diagnost=diag_findings,
                                     synthesis_critique=critique)
        row = db.session.get(RedesignAttempt, att_id)
        if outcome.get("available"):
            row.mode = outcome.get("mode")
            row.result_json = json.dumps(outcome["result"], ensure_ascii=False)
            row.issues_json = json.dumps(outcome.get("issues") or [], ensure_ascii=False)
        else:
            row.error = outcome.get("error") or "Авто-редизайнер недоступен."

    jobs.submit(doc_id, game_index, "redesign", run)
    return redirect(url_for("redesign", doc_id=doc_id, game_index=game_index))


@app.route("/documents/<int:doc_id>/redesign/<int:game_index>/decide", methods=["POST"])
@login_required
def redesign_decide(doc_id, game_index):
    """Решение автора по предложенной правке: принять или отклонить.

    Принятие обнуляет скелет и статистику: структура изменилась, и прежние
    числа к ней больше не относятся (`needs_resimulation` в ответе агента).
    """
    document = _owned_document(doc_id)
    br, gs = _redesign_context(doc_id, game_index)

    att = (RedesignAttempt.query
           .filter_by(document_id=doc_id, game_index=game_index,
                      status=RedesignAttempt.STATUS_PROPOSED)
           .order_by(RedesignAttempt.attempt_number.desc()).first())
    if att is None or not att.result_json:
        flash("Нет предложенной правки.", "error")
        return redirect(url_for("redesign", doc_id=doc_id, game_index=game_index))

    if request.form.get("action") == "reject":
        att.status = RedesignAttempt.STATUS_REJECTED
        db.session.commit()
        flash("Правка отклонена — структура игры осталась прежней.", "success")
        return redirect(url_for("redesign", doc_id=doc_id, game_index=game_index))

    gs.spec_json = json.dumps(
        redesign_agent.apply_to_spec(gs.spec_dict() or {}, att.result()),
        ensure_ascii=False)
    att.status = RedesignAttempt.STATUS_ACCEPTED

    # Скелет собран по прежнему ядру, статистика посчитана по прежнему скелету —
    # после правки и то и другое недействительно.
    sk = GameSkeleton.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if sk is not None:
        sk.simulatable = None
        sk.code = None
        sk.player_counts_json = None
        sk.assumptions_json = None
        sk.reason = None
        sk.missing_json = None
        sk.error = None
    br.stats_json = None
    br.stats_source = None
    br.report_json = None
    br.issues_json = None
    br.error = None

    # Вердикты диагноста вынесены о ПРЕЖНЕЙ структуре и после правки не значат
    # ничего. Оставить их значило бы посчитать итоговый балл по перемешанным
    # данным: числа новые, вердикты старые — и разницы в отчёте не видно.
    dr = DiagnosisReport.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if dr is not None:
        dr.phase = DiagnosisReport.PHASE_NONE
        dr.triage_json = dr.findings_json = dr.extra_runs_json = None
        dr.triage_issues_json = dr.issues_json = None
        dr.kept_requests_json = dr.dropped_requests_json = None
        dr.error = dr.runs_error = None
        dr.attempt_number = (dr.attempt_number or 1) + 1

    # Оценка по линзам — о ЗАМЫСЛЕ прежней версии игры. Правка меняет правила,
    # а вместе с ними и то, что оценивалось: глубину решений, экономику как
    # замысел, ответы на вопросы диагноста (самих вопросов после пересчёта
    # может не остаться). Оставить её значило бы смешать в итоговом балле
    # качественную оценку старой игры с числовой оценкой новой.
    lr = LensReport.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if lr is not None:
        lr.result_json = lr.issues_json = None
        lr.error = None
    db.session.commit()

    # Пересборку скелета заказываем ЗДЕСЬ. Экран сам шаг больше не запускает
    # (jobs.may_autostart): иначе сбой крутился бы по кругу за деньги. А
    # применение правки — явное решение автора, ровно тот случай, когда
    # симуляциониста надо позвать повторно.
    jobs.submit(doc_id, game_index, "skeleton", _skeleton_job(document, game_index))

    flash("Правка применена. Скелет, статистика, вердикты диагноста и оценка по "
          "линзам сброшены — игру нужно пересимулировать и оценить заново.", "success")
    return redirect(url_for("skeleton", doc_id=doc_id, game_index=game_index))


def _redesign_context(doc_id, game_index):
    """Отчёт о балансе и структура. Без отчёта чинить нечего — правка идёт по находкам."""
    br = BalanceReport.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if br is None or not br.report_json:
        flash("Сначала нужна оценка баланса — правка делается только по найденным недочётам.",
              "warning")
        abort(redirect(url_for("balance", doc_id=doc_id, game_index=game_index)))
    gs = GameSpec.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if gs is None or not gs.spec_json:
        abort(redirect(url_for("spec", doc_id=doc_id, game_index=game_index)))
    return br, gs


def _redesign_attempts(doc_id, game_index):
    return (RedesignAttempt.query
            .filter_by(document_id=doc_id, game_index=game_index)
            .order_by(RedesignAttempt.attempt_number.asc()).all())


def _subjective_actions(spec_root, sk=None):
    """Действия с «мягкими» числами — объединение двух источников.

    Извлеченец классифицирует действие как subjective_judgment по тексту, а
    симуляционист (v6) заявляет, что реально смоделировал случайной величиной.
    Источники могут разойтись, и объединение — безопасное направление: лишняя
    осторожность даёт вердикт warn вместо problem, тогда как пропуск даёт
    ложный флаг доминирующей стратегии по артефакту допущения.
    """
    declared = (sk.meta() or {}).get("subjective_actions") if sk is not None else None
    return sorted(set(extractor_agent.subjective_actions(spec_root)) | set(declared or []))


def _balance_context(doc_id, game_index):
    """Скелет и запись отчёта. Баланс оценивать не по чему, пока игра несимулируема."""
    sk = GameSkeleton.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if sk is None or not sk.simulatable or not sk.code:
        flash("Сначала нужен собранный скелет-симулятор.", "warning")
        abort(redirect(url_for("skeleton", doc_id=doc_id, game_index=game_index)))

    br = BalanceReport.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if br is None:
        br = BalanceReport(document_id=doc_id, game_index=game_index)
        db.session.add(br)
        db.session.commit()
    return sk, br


def _run_stats_evaluation(doc_id, game_index, br):
    """Прогон оценщика по сохранённой статистике.

    Агенту подаётся ТОЛЬКО его зона: core, STATS_JSON и три поля сайдкара.
    Ничего диагностического — иначе на одну игру появятся два несогласованных
    вердикта, и авто-редизайнер получит противоречивые указания.
    """
    gs = GameSpec.query.filter_by(document_id=doc_id, game_index=game_index).first()
    sk = GameSkeleton.query.filter_by(document_id=doc_id, game_index=game_index).first()
    root = (gs.spec_dict() if gs else None) or {}
    core = ((root.get("game_spec") or {}).get("core")) or {}

    outcome = stats_agent.run(
        core, br.stats(),
        diagnostic_meta=root.get("diagnostic_meta"),
        assumptions=sk.assumptions() if sk else [],
        subjective_actions=_subjective_actions(root, sk),
    )
    if outcome.get("available"):
        br.error = None
        br.report_json = json.dumps(outcome["report"], ensure_ascii=False)
        br.issues_json = json.dumps(outcome.get("issues") or [], ensure_ascii=False)
    else:
        br.error = outcome.get("error") or "Оценщик статистик недоступен."


@app.route("/documents/<int:doc_id>/skeleton/<int:game_index>.py")
@login_required
def skeleton_code(doc_id, game_index):
    """Отдаёт заполненный game_skeleton.py файлом."""
    _owned_document(doc_id)
    sk = GameSkeleton.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if sk is None or not sk.code:
        abort(404)
    return app.response_class(
        sk.code,
        mimetype="text/x-python",
        headers={"Content-Disposition":
                 f'attachment; filename="game_skeleton_doc{doc_id}_game{game_index}.py"'},
    )


def _apply_mirror_outcome(ms, outcome, phase_on_success):
    """Записывает результат вызова агента в сессию: успех продвигает фазу, ошибка — нет.

    На ошибке фаза не меняется, поэтому повторный GET на /mirror сам повторит
    попытку (для pending) или пользователь может просто отправить ответ ещё раз
    (для mirror) — без отдельной кнопки «retry».
    """
    if not outcome.get("available"):
        ms.error = outcome.get("error") or "ИИ-агент недоступен."
        return
    ms.error = None
    ms.last_text = outcome.get("text")
    data = outcome.get("json") or {}
    ms.last_json = json.dumps(data, ensure_ascii=False) if outcome.get("json") else None
    # Карту узлов запоминаем отдельно и НЕ теряем при переходе к подтверждению:
    # она приходит на проходе 1, а last_json потом перезаписывается фазой
    # confirmed, где карты уже нет. Извлеченцу нужны именно статусы absent.
    if data.get("map"):
        ms.map_json = json.dumps(data["map"], ensure_ascii=False)
    ms.issues_json = json.dumps(outcome.get("issues") or [], ensure_ascii=False)
    ms.ready_to_proceed = bool(data.get("ready_to_proceed"))
    ms.phase = phase_on_success


def _owned_document(doc_id):
    """Возвращает документ, если он принадлежит текущему пользователю/админу.

    Если документ не найден — не голый 404, а понятное сообщение и редирект в
    кабинет: чаще всего это просто устаревшая ссылка (хранилище эфемерное и
    чистится при перезапуске сервера, см. AUTH_DISABLED), а не настоящая ошибка.
    """
    user = current_user()
    document = db.session.get(Document, doc_id)
    if document is None:
        flash("Документ не найден — возможно, ссылка устарела (сервер перезапускался). "
              "Загрузите файл заново.", "warning")
        abort(redirect(url_for("dashboard")))
    if document.user_id != user.id and not user.is_admin:
        abort(403)
    return document


if __name__ == "__main__":
    # Очистку хранилища при запуске включаем ЗДЕСЬ, а не при импорте: импорт
    # делают и тесты, и сид-скрипты, и раньше они молча стирали базу живого
    # сервера. Явное значение в окружении сильнее этого умолчания.
    os.environ.setdefault("FINIGROSKOP_RESET", "1")

    # Автоперезагрузчик вернули, но с двумя оговорками — обе выяснены на практике.
    #
    # 1) Werkzeug по умолчанию следит за файлами ВСЕХ импортированных модулей,
    #    включая site-packages: любой `pip install` чего-то постороннего
    #    перезапускал воркер. `exclude_patterns` убирает из-под наблюдения чужие
    #    библиотеки — сторожим только свой код.
    # 2) Раньше перезапуск был ещё и ОПАСЕН: вызов модели шёл синхронно внутри
    #    запроса, и рестарт посреди него молча обрывал соединение. Теперь вызовы
    #    ушли в фоновые задачи, а задачи, застигнутые перезапуском, помечаются
    #    `failed` с внятной причиной (jobs.recover_orphans) — вместо вечного
    #    «идёт…» автор видит «прервано, запустите заново».
    #
    # reloader_type="stat" выбран сознательно: watchdog умеет следить точнее, но
    # он внешняя зависимость, а сервис намеренно живёт на стандартной библиотеке.
    app.run(debug=True, reloader_type="stat",
            exclude_patterns=["*/site-packages/*", "*/lib/*", "*/Lib/*"])
