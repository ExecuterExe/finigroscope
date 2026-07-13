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
from simulation import constructor as sim_constructor
from simulation import runner as sim_runner
from simulation.schema import BLOCKS
from models import Document, Report, SimConfig, User, db

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
        _reset_storage()   # чистый старт при каждом запуске сервера
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


def _owned_document(doc_id):
    """Возвращает документ, если он принадлежит текущему пользователю/админу, иначе abort."""
    user = current_user()
    document = db.session.get(Document, doc_id)
    if document is None:
        abort(404)
    if document.user_id != user.id and not user.is_admin:
        abort(403)
    return document


@app.route("/documents/<int:doc_id>/constructor/<int:game_index>", methods=["GET"])
@login_required
def constructor(doc_id, game_index):
    """Форма конструктора блоков для конкретной игры из документа."""
    document = _owned_document(doc_id)
    presets = sim_constructor.preset_list()
    game_title = f"Игра {game_index}"

    # пресет-шаблон по запросу — приоритетнее сохранённого/предзаполнения
    preset_key = request.args.get("preset")
    if preset_key:
        preset_cfg = sim_constructor.get_preset(preset_key)
        if preset_cfg:
            form_state = sim_constructor.build_form_state(saved=preset_cfg)
            return render_template("constructor.html", document=document, game_index=game_index,
                                   game_title=game_title, state=form_state, blocks_meta=BLOCKS,
                                   presets=presets, saved=False, preset_key=preset_key)

    saved = SimConfig.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if saved:
        form_state = sim_constructor.build_form_state(saved=saved.config())
        return render_template("constructor.html", document=document, game_index=game_index,
                               game_title=game_title, state=form_state,
                               blocks_meta=BLOCKS, presets=presets, saved=True, preset_key=None)

    # предзаполнение из этапа 1
    stage1_report = (
        Report.query.filter_by(document_id=doc_id, stage="1")
        .order_by(Report.created_at.desc()).first()
    )
    prefill = None
    if stage1_report:
        games = stage1_report.result().get("games", [])
        game = next((g for g in games if g["index"] == game_index), None)
        if game:
            prefill = sim_constructor.build_prefill(game)
            game_title = game.get("title", game_title)
    form_state = sim_constructor.build_form_state(prefill=prefill)
    return render_template("constructor.html", document=document, game_index=game_index,
                           game_title=game_title, state=form_state, blocks_meta=BLOCKS,
                           presets=presets, saved=False, preset_key=None)


@app.route("/documents/<int:doc_id>/constructor/<int:game_index>", methods=["POST"])
@login_required
def save_constructor(doc_id, game_index):
    """Сохранение конфигурации конструктора (JSON из формы)."""
    _owned_document(doc_id)

    payload = request.get_json(silent=True) or {}
    cfg = sim_constructor.validate(payload)

    record = SimConfig.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if record is None:
        record = SimConfig(document_id=doc_id, game_index=game_index)
        db.session.add(record)
    record.config_json = json.dumps(cfg, ensure_ascii=False)
    db.session.commit()

    return {"ok": True, "saved": cfg}


@app.route("/documents/<int:doc_id>/simulate/<int:game_index>", methods=["POST"])
@login_required
def simulate(doc_id, game_index):
    """Прогон симуляции по конфигу из формы. Возвращает метрики и находки."""
    _owned_document(doc_id)

    payload = request.get_json(silent=True) or {}
    cfg = sim_constructor.validate(payload.get("config") or payload)
    n_games = int(payload.get("n_games", sim_runner.DEFAULT_GAMES) or sim_runner.DEFAULT_GAMES)

    result = sim_runner.run(cfg, n_games=n_games)

    # сохраняем последний прогон как отчёт этапа 2
    if result.get("runnable"):
        report = Report(
            document_id=doc_id, stage="2", status="done",
            result_json=json.dumps({"game_index": game_index, "config": cfg, "result": result},
                                   ensure_ascii=False),
        )
        db.session.add(report)
        db.session.commit()

    return {"ok": True, "result": result}


if __name__ == "__main__":
    app.run(debug=True)
