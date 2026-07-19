"""ORM-модели ФинИгроСкопа.

Пока заведена только таблица пользователей — она нужна для входа в сервис.
Остальные таблицы из проектного документа (documents, reports, sim_configs,
jobs) добавятся по мере реализации соответствующих этапов конвейера.

База данных: SQLite на старте (см. раздел 9.1 проектного документа), модели
описаны через SQLAlchemy, чтобы позже без боли переехать на PostgreSQL.
"""

from datetime import datetime

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


class User(db.Model):
    """Участник конкурса или администратор ИФТП.

    Регистрации на сайте нет: список участников формируется заранее (люди уже
    зарегистрировались на конкурсе). Логин — telegram-ник с «@», пароль —
    персональная строка символов, выданная участнику. Храним только хэш.
    """

    __tablename__ = "users"

    ROLE_PARTICIPANT = "participant"
    ROLE_ADMIN = "admin"

    id = db.Column(db.Integer, primary_key=True)
    tg_tag = db.Column(db.String(64), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(16), nullable=False, default=ROLE_PARTICIPANT)
    display_name = db.Column(db.String(128), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # --- работа с паролем ---------------------------------------------------
    def set_password(self, raw_password: str) -> None:
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password: str) -> bool:
        return check_password_hash(self.password_hash, raw_password)

    # --- удобные свойства ---------------------------------------------------
    @property
    def is_admin(self) -> bool:
        return self.role == self.ROLE_ADMIN

    @staticmethod
    def normalize_tag(tag: str) -> str:
        """Приводит ник к единому виду: обрезает пробелы, гарантирует «@»."""
        tag = (tag or "").strip()
        if tag and not tag.startswith("@"):
            tag = "@" + tag
        return tag

    def __repr__(self) -> str:  # pragma: no cover - отладочное
        return f"<User {self.tg_tag} ({self.role})>"


class Document(db.Model):
    """Загруженный участником документ .docx (эссе или карточка идей).

    Повторная загрузка тем же участником файла с тем же именем создаёт новую
    версию (см. раздел 8.2 — история версий).
    """

    __tablename__ = "documents"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    filename = db.Column(db.String(255), nullable=False)
    stored_path = db.Column(db.String(512), nullable=False)
    file_hash = db.Column(db.String(64), nullable=False, index=True)
    doc_type = db.Column(db.String(16), nullable=False)  # essay | card
    version = db.Column(db.Integer, nullable=False, default=1)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User", backref="documents")
    reports = db.relationship("Report", backref="document", cascade="all, delete-orphan")


class Report(db.Model):
    """Результат одного этапа анализа по документу (JSON)."""

    __tablename__ = "reports"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=False, index=True)
    stage = db.Column(db.String(16), nullable=False)  # 1 | 2 | 3 | redesign
    status = db.Column(db.String(16), nullable=False, default="done")
    result_json = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def result(self) -> dict:
        import json
        return json.loads(self.result_json)


class MirrorSession(db.Model):
    """Диалог агента «Зеркало понимания» для одной игры документа.

    Протокол ровно двухпроходный (см. review/prompts/mirror.md): проход 1 без
    ответа автора, проход 2 — с ответом. Одна сессия на (document_id,
    game_index) — повторный визит на экран продолжает её, а не начинает заново.
    """

    __tablename__ = "mirror_sessions"

    PHASE_PENDING = "pending"      # ещё не запускали проход 1
    PHASE_MIRROR = "mirror"        # проход 1 сделан, ждём ответа автора
    PHASE_CONFIRMED = "confirmed"  # проход 2 сделан

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=False, index=True)
    game_index = db.Column(db.Integer, nullable=False, default=1)
    phase = db.Column(db.String(16), nullable=False, default=PHASE_PENDING)
    last_text = db.Column(db.Text, nullable=True)       # человекочитаемый ответ агента
    last_json = db.Column(db.Text, nullable=True)       # его машинный JSON-блок
    author_answer = db.Column(db.Text, nullable=True)   # текст ответа автора
    error = db.Column(db.Text, nullable=True)           # причина, если LLM недоступен
    ready_to_proceed = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    document = db.relationship("Document", backref="mirror_sessions")

    __table_args__ = (
        db.UniqueConstraint("document_id", "game_index", name="uq_mirror_doc_game"),
    )

    def last_json_dict(self):
        import json
        return json.loads(self.last_json) if self.last_json else None
