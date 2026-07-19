"""ФинИгроСкоп — Flask-приложение.

На текущем этапе реализован публичный «фасад» сервиса:
  • приветственная (лендинг) страница с описанием трёх этапов конвейера;
  • вход без регистрации (telegram-ник + персональный пароль);
  • заглушка личного кабинета, доступная только после входа.

Всё, что за пределами лендинга и страницы входа, требует авторизации.
"""

import hashlib
import json
import os
import uuid
from functools import wraps

from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

from analysis import stage1
from analysis.reference import DOC_TYPES
from models import Document, MirrorSession, Report, User, db
from review import mirror as mirror_agent

# Секреты (LLM_PROVIDER, GEMINI_API_KEY, ...) читаются из .env в корне проекта —
# см. .env.example. Файл .env в git не попадает (см. .gitignore).
load_dotenv()

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")

# --- создание приложения ----------------------------------------------------
app = Flask(__name__)
app.config.update(
    SECRET_KEY="finigroskop-dev-secret-change-me",  # TODO: вынести в конфиг/ENV
    SQLALCHEMY_DATABASE_URI="sqlite:///finigroskop.db",
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,  # 16 МБ на файл
    UPLOAD_DIR=UPLOAD_DIR,
)
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Вход временно ОТКЛЮЧЁН для быстрого тестирования: сервис работает без логина.
# При этом хранилище ЭФЕМЕРНОЕ и ИЗОЛИРОВАННОЕ ПО СЕССИИ браузера:
#   • при перезапуске сервера БД и папка uploads очищаются (файлы не копятся);
#   • каждый посетитель видит только свои загрузки (документы привязаны к сессии).
# Чтобы вернуть штатный вход — снять флаг (сброс/изоляция отключатся сами).
AUTH_DISABLED = True

db.init_app(app)


def _reset_storage():
    """Полная очистка хранилища: пересоздать таблицы и удалить загруженные файлы."""
    db.drop_all()
    db.create_all()
    for name in os.listdir(UPLOAD_DIR):
        try:
            os.remove(os.path.join(UPLOAD_DIR, name))
        except OSError:
            pass


with app.app_context():
    if AUTH_DISABLED:
        # Только на ДЕЙСТВИТЕЛЬНО новый запуск процесса `python app.py`, а не на
        # каждую авто-перезагрузку Flask (debug=True перезапускает воркер при
        # любом сохранении .py-файла — без этой проверки правки кода на лету
        # стирали бы всю сессию пользователя, и старые открытые вкладки/ссылки
        # начинали бы отдавать 404 посреди работы).
        if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
            _reset_storage()
    else:
        db.create_all()


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
    """Делает current_user доступным во всех шаблонах."""
    return {"current_user": current_user(), "auth_disabled": AUTH_DISABLED}


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

    if ms.phase == MirrorSession.PHASE_PENDING:
        game_text = stage1.game_text_for_agent(document.stored_path, document.doc_type, game_index)
        outcome = mirror_agent.run_pass(game_text)
        _apply_mirror_outcome(ms, outcome, phase_on_success=MirrorSession.PHASE_MIRROR)
        db.session.commit()

    return render_template("mirror.html", document=document, game=game,
                           game_index=game_index, ms=ms)


@app.route("/documents/<int:doc_id>/mirror/<int:game_index>/reply", methods=["POST"])
@login_required
def mirror_reply(doc_id, game_index):
    """Отправка ответа автора агенту → проход 2 («Сверка и финал»)."""
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
    game_text = stage1.game_text_for_agent(document.stored_path, document.doc_type, game_index)
    outcome = mirror_agent.run_pass(game_text, prior_json=ms.last_json_dict(), author_answer=answer)
    _apply_mirror_outcome(ms, outcome, phase_on_success=MirrorSession.PHASE_CONFIRMED)
    db.session.commit()

    return redirect(url_for("mirror", doc_id=doc_id, game_index=game_index))


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
    ms.last_json = json.dumps(outcome.get("json"), ensure_ascii=False) if outcome.get("json") else None
    ms.ready_to_proceed = bool((outcome.get("json") or {}).get("ready_to_proceed"))
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
    # use_reloader=False — намеренно. Автоперезагрузчик Werkzeug следит не
    # только за файлами проекта, а за файлами ВСЕХ импортированных модулей,
    # включая сторонние библиотеки в site-packages: любое изменение там (даже
    # `pip install` чего-то не связанного) перезапускает воркер. Для обычных
    # маршрутов это неприятно (см. AUTH_DISABLED — сбрасывало сессию), а для
    # /mirror — ОПАСНО: запрос к ИИ-агенту синхронный и может идти до минуты,
    # и перезапуск посреди него молча обрывает соединение (наблюдалось на
    # практике). После правки .py-файлов сервер нужно перезапускать вручную.
    app.run(debug=True, use_reloader=False)
