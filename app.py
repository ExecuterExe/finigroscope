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
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

from analysis import stage1
from analysis.reference import DOC_TYPES
from models import (
    BalanceReport,
    Document,
    GameSkeleton,
    GameSpec,
    MirrorSession,
    RedesignAttempt,
    Report,
    User,
    db,
)
from review import extractor as extractor_agent
from review import llm_provider, llm_settings
from review import mirror as mirror_agent
from review import redesigner as redesign_agent
from review import simulationist as simulationist_agent
from review import spec_describe
from review import stats_evaluator as stats_agent
from simulation import runner as sim_runner

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
        outcome = mirror_agent.run_pass(game_text, round_no=ms.round,
                                        max_rounds=MirrorSession.MAX_ROUNDS)
        _apply_mirror_outcome(ms, outcome, phase_on_success=MirrorSession.PHASE_MIRROR)
        db.session.commit()

    return render_template("mirror.html", document=document, game=game,
                           game_index=game_index, ms=ms)


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

    was_final = ms.is_final_round
    ms.author_answer = answer
    game_text = stage1.game_text_for_agent(document.stored_path, document.doc_type, game_index)
    outcome = mirror_agent.run_pass(game_text, prior_json=ms.last_json_dict(), author_answer=answer,
                                    round_no=ms.round, max_rounds=MirrorSession.MAX_ROUNDS)

    if not outcome.get("available"):
        ms.error = outcome.get("error") or "ИИ-агент недоступен."
        db.session.commit()
        return redirect(url_for("mirror", doc_id=doc_id, game_index=game_index))

    data = outcome.get("json") or {}
    wants_more = (not data.get("ready_to_proceed")) and bool(data.get("questions"))
    # Ещё раунд — только если агент реально просит и лимит не исчерпан.
    if wants_more and not was_final:
        ms.round += 1
        _apply_mirror_outcome(ms, outcome, phase_on_success=MirrorSession.PHASE_MIRROR)
    else:
        _apply_mirror_outcome(ms, outcome, phase_on_success=MirrorSession.PHASE_CONFIRMED)
        if was_final and wants_more:
            # Модель проигнорировала запрет на новые вопросы — закрываем сами.
            ms.ready_to_proceed = True
    db.session.commit()

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

    if gs.spec_json is None:
        _run_extraction(document, ms, gs, game_index)
        db.session.commit()

    # Критичные пробелы — вход второго гейта: их поднимает не автор, а машина,
    # и закрыть их может только агент понимания (единственный, кто спрашивает).
    gate2_gaps = extractor_agent.critical_gaps(gs.spec_dict())
    return render_template("spec.html", document=document, game_index=game_index,
                           gs=gs, ms=ms, describe=spec_describe,
                           gate2_gaps=gate2_gaps)


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
    if ms.gate2_status == MirrorSession.GATE2_NONE:
        game_text = stage1.game_text_for_agent(document.stored_path, document.doc_type, game_index)
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
        db.session.commit()

    return render_template("gate2.html", document=document, game_index=game_index,
                           ms=ms, gs=gs, gaps=gaps)


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

    gaps = extractor_agent.critical_gaps(gs.spec_dict())
    asked = (ms.gate2_json_dict() or {}).get("questions") or []
    game_text = stage1.game_text_for_agent(document.stored_path, document.doc_type, game_index)
    outcome = mirror_agent.run_gate2(
        game_text, ms.last_json_dict(), gaps,
        ambiguities=extractor_agent.ambiguities(gs.spec_dict()),
        author_answer=answer, asked_questions=asked)

    if not outcome.get("available"):
        ms.gate2_error = outcome.get("error") or "ИИ-агент недоступен."
        db.session.commit()
        return redirect(url_for("gate2", doc_id=doc_id, game_index=game_index))

    data = outcome.get("json") or {}
    ms.gate2_error = None
    ms.gate2_answer = answer
    ms.gate2_text = outcome.get("text")
    ms.gate2_json = json.dumps(data, ensure_ascii=False)
    ms.gate2_status = MirrorSession.GATE2_DONE
    if data.get("simulation_blocking") is not None:
        ms.simulation_blocking = bool(data.get("simulation_blocking"))

    # Ответы гейта — полноправные уточнения автора: дописываем их в подтверждённый
    # материал прохода 2 и пересобираем структуру по обновлённому тексту.
    confirmed = ms.last_json_dict() or {}
    ms.last_json = json.dumps(
        extractor_agent.merge_gate2_answers(confirmed, data, answer), ensure_ascii=False)

    gs.spec_json = None
    _run_extraction(document, ms, gs, game_index)
    db.session.commit()

    flash("Ответы второго гейта учтены, структура пересобрана.", "success")
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

    game_text = stage1.game_text_for_agent(document.stored_path, document.doc_type, game_index)
    outcome = mirror_agent.run_pass(
        game_text, prior_json=ms.last_json_dict(), author_answer=note,
        round_no=MirrorSession.MAX_ROUNDS, max_rounds=MirrorSession.MAX_ROUNDS,
        revision_note=note)

    if not outcome.get("available"):
        flash(outcome.get("error") or "Агент недоступен, попробуйте позже.", "error")
        return redirect(url_for("spec", doc_id=doc_id, game_index=game_index))

    # Правка засчитывается только при удачном прогоне — иначе автор потерял бы
    # единственную попытку из-за сбоя сети, а не из-за своего решения.
    gs.revisions += 1
    gs.status = GameSpec.STATUS_REVISED
    gs.revision_note = note

    ms.author_answer = note
    _apply_mirror_outcome(ms, outcome, phase_on_success=MirrorSession.PHASE_CONFIRMED)
    ms.ready_to_proceed = True  # режим правки всегда завершает сверку

    gs.spec_json = None  # заставляем пересобрать структуру заново
    _run_extraction(document, ms, gs, game_index)
    db.session.commit()

    flash("Замечание учтено, структура пересобрана.", "success")
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
    if not sk.is_ready:
        _run_simulation(gs, sk)
        db.session.commit()

    return render_template("skeleton.html", document=document, game_index=game_index, sk=sk)


def _run_simulation(gs, sk):
    """Прогон симуляциониста по ядру принятой структуры (game_spec.core)."""
    spec = (gs.spec_dict() or {}).get("game_spec") or {}
    core = spec.get("core") or {}
    outcome = simulationist_agent.run(core)
    if not outcome.get("available"):
        sk.error = outcome.get("error") or "Симуляционист недоступен."
        return
    sk.error = None
    sk.simulatable = bool(outcome.get("simulatable"))
    if sk.simulatable:
        sk.code = outcome.get("code")
        sk.player_counts_json = json.dumps(outcome.get("player_counts") or [], ensure_ascii=False)
        sk.assumptions_json = json.dumps(outcome.get("assumptions") or [], ensure_ascii=False)
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
    _owned_document(doc_id)
    gs = GameSpec.query.filter_by(document_id=doc_id, game_index=game_index).first()
    sk = GameSkeleton.query.filter_by(document_id=doc_id, game_index=game_index).first()
    if gs is None or gs.status != GameSpec.STATUS_ACCEPTED:
        abort(404)
    if sk is None:
        sk = GameSkeleton(document_id=doc_id, game_index=game_index)
        db.session.add(sk)
    _run_simulation(gs, sk)
    db.session.commit()
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

    if br.has_stats and not br.report_json and not br.error:
        _run_stats_evaluation(doc_id, game_index, br)
        db.session.commit()

    gs = GameSpec.query.filter_by(document_id=doc_id, game_index=game_index).first()
    root = gs.spec_dict() if gs else {}
    return render_template(
        "balance.html", document=document, game_index=game_index, sk=sk, br=br,
        breakdown=stats_agent.reachability_breakdown(br.stats() or []),
        # Именно ИМЕНА действий: `assumptions` симуляциониста — это свободные
        # фразы («исход питча смоделирован случайно»), и в списке действий им
        # не место, иначе автор видит предложение вместо идентификатора.
        soft=stats_agent.soft_actions((root or {}).get("diagnostic_meta"),
                                      extractor_agent.subjective_actions(root)),
    )


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
    else:
        raw = (request.form.get("stats") or "").strip()
        if not raw:
            flash("Вставьте JSON со статистикой прогона.", "error")
            return redirect(url_for("balance", doc_id=doc_id, game_index=game_index))
        try:
            stats = json.loads(raw)
        except json.JSONDecodeError as exc:
            flash(f"Это не похоже на JSON: {exc}", "error")
            return redirect(url_for("balance", doc_id=doc_id, game_index=game_index))
        if not sim_runner.looks_like_stats(stats):
            flash("JSON разобран, но это не STATS_JSON базового прогона — нужен список "
                  "конфигураций с полями num_players и win_rate_by_seat.", "error")
            return redirect(url_for("balance", doc_id=doc_id, game_index=game_index))
        source = BalanceReport.SOURCE_PASTED

    br.stats_json = json.dumps(stats, ensure_ascii=False)
    br.stats_source = source
    br.error = None
    # Новая статистика — прежний вердикт по ней недействителен.
    br.report_json = None
    br.issues_json = None
    db.session.commit()

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
    _run_stats_evaluation(doc_id, game_index, br)
    db.session.commit()
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
    trigger = redesign_agent.should_trigger(br.report())
    current = next((a for a in attempts if a.status == RedesignAttempt.STATUS_PROPOSED), None)

    return render_template(
        "redesign.html", document=document, game_index=game_index,
        br=br, gs=gs, attempts=attempts, current=current, trigger=trigger,
        max_attempts=RedesignAttempt.MAX_ATTEMPTS,
        used=len([a for a in attempts if a.status == RedesignAttempt.STATUS_ACCEPTED]),
    )


@app.route("/documents/<int:doc_id>/redesign/<int:game_index>/propose", methods=["POST"])
@login_required
def redesign_propose(doc_id, game_index):
    """Запрашивает у агента очередной шаг правки (предложение, не применение)."""
    document = _owned_document(doc_id)
    br, gs = _redesign_context(doc_id, game_index)

    finding = br.report()
    trigger = redesign_agent.should_trigger(finding)
    if not trigger["trigger"]:
        flash(f"Авто-редизайн не нужен: {trigger['reason']}.", "warning")
        return redirect(url_for("redesign", doc_id=doc_id, game_index=game_index))

    attempts = _redesign_attempts(doc_id, game_index)
    accepted = [a for a in attempts if a.status == RedesignAttempt.STATUS_ACCEPTED]
    if len(accepted) >= RedesignAttempt.MAX_ATTEMPTS:
        flash(f"Исчерпан лимит попыток авто-редизайна ({RedesignAttempt.MAX_ATTEMPTS}). "
              "Оставшиеся находки идут в рекомендации автору.", "warning")
        return redirect(url_for("redesign", doc_id=doc_id, game_index=game_index))

    # Незакрытое предложение заменяем новым, а не плодим параллельные.
    pending = [a for a in attempts if a.status == RedesignAttempt.STATUS_PROPOSED]
    for a in pending:
        db.session.delete(a)
    db.session.flush()

    attempt_number = len(accepted) + 1
    spec_root = gs.spec_dict() or {}
    previous = redesign_agent.collect_previous_changes(
        [a.result() or {} for a in accepted])

    outcome = redesign_agent.run(spec_root, finding, attempt_number=attempt_number,
                                 previous_changes=previous)

    att = RedesignAttempt(document_id=doc_id, game_index=game_index,
                          attempt_number=attempt_number,
                          trigger_reason=trigger["reason"],
                          spec_before_json=json.dumps(spec_root, ensure_ascii=False))
    if outcome.get("available"):
        att.mode = outcome.get("mode")
        att.result_json = json.dumps(outcome["result"], ensure_ascii=False)
        att.issues_json = json.dumps(outcome.get("issues") or [], ensure_ascii=False)
    else:
        att.error = outcome.get("error") or "Авто-редизайнер недоступен."
    db.session.add(att)
    db.session.commit()

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
    db.session.commit()

    flash("Правка применена. Скелет и статистика сброшены — игру нужно пересимулировать.",
          "success")
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
        subjective_actions=extractor_agent.subjective_actions(root),
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
    # use_reloader=False — намеренно. Автоперезагрузчик Werkzeug следит не
    # только за файлами проекта, а за файлами ВСЕХ импортированных модулей,
    # включая сторонние библиотеки в site-packages: любое изменение там (даже
    # `pip install` чего-то не связанного) перезапускает воркер. Для обычных
    # маршрутов это неприятно (см. AUTH_DISABLED — сбрасывало сессию), а для
    # /mirror — ОПАСНО: запрос к ИИ-агенту синхронный и может идти до минуты,
    # и перезапуск посреди него молча обрывает соединение (наблюдалось на
    # практике). После правки .py-файлов сервер нужно перезапускать вручную.
    app.run(debug=True, use_reloader=False)
