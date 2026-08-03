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
    """Диалог агента «Понимание игры» для одной игры документа.

    Цикличный, но КОНЕЧНЫЙ (см. review/prompts/mirror.md): агент может
    переспрашивать, пока не исчерпан MAX_ROUNDS, после чего обязан завершить
    сверку. Одна сессия на (document_id, game_index) — повторный визит на экран
    продолжает её, а не начинает заново.
    """

    __tablename__ = "mirror_sessions"

    PHASE_PENDING = "pending"      # ещё не запускали проход 1
    PHASE_MIRROR = "mirror"        # вопросы заданы, ждём ответа автора
    PHASE_CONFIRMED = "confirmed"  # сверка завершена

    # Жёсткий предел раундов уточнений: агент может переспросить, если ответы
    # автора закрыли не всё, но не бесконечно. На последнем раунде он обязан
    # завершить сверку, перенеся нераскрытое в still_open.
    MAX_ROUNDS = 3

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=False, index=True)
    game_index = db.Column(db.Integer, nullable=False, default=1)
    phase = db.Column(db.String(16), nullable=False, default=PHASE_PENDING)
    round = db.Column(db.Integer, nullable=False, default=1)  # текущий раунд уточнений
    last_text = db.Column(db.Text, nullable=True)       # человекочитаемый ответ агента
    last_json = db.Column(db.Text, nullable=True)       # его машинный JSON-блок
    author_answer = db.Column(db.Text, nullable=True)   # текст ответа автора
    # Карта узлов со статусами (v3) хранится ОТДЕЛЬНО: она приходит на проходе 1,
    # а last_json затем перезаписывается подтверждением прохода 2, где карты уже
    # нет. Без отдельного поля извлеченец не получил бы статусы absent и заново
    # угадывал бы «механики нет» против «не описано».
    map_json = db.Column(db.Text, nullable=True)
    # Замечания самопроверки к последнему проходу. Главное из них — незаданный
    # вопрос о компонентах: он ничем себя не выдаёт, а через четыре шага гасит
    # целую группу проверок баланса артефактов.
    issues_json = db.Column(db.Text, nullable=True)
    error = db.Column(db.Text, nullable=True)           # причина, если LLM недоступен
    ready_to_proceed = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    document = db.relationship("Document", backref="mirror_sessions")

    __table_args__ = (
        db.UniqueConstraint("document_id", "game_index", name="uq_mirror_doc_game"),
    )

    # --- второй гейт (проход 3 промпта понимателя) ---------------------------
    # Отдельные поля, а не переиспользование last_json: там лежит подтверждённый
    # материал прохода 2, который нужен извлеченцу, и перезаписать его гейтом —
    # значит потерять источник истины для всего остального конвейера.
    #
    # `phase` гейт тоже НЕ меняет: сверка понимания к этому моменту уже
    # завершена (confirmed), гейт — отдельная короткая ветка поверх неё.
    MAX_GATE2 = 1                    # гейт однократный, как и правка структуры

    GATE2_NONE = "none"              # ещё не запускали
    GATE2_ASKED = "asked"            # точечные вопросы заданы, ждём автора
    GATE2_DONE = "done"              # гейт закрыт (с ответом или без вопросов)

    gate2_status = db.Column(db.String(16), nullable=False, default=GATE2_NONE)
    gate2_runs = db.Column(db.Integer, nullable=False, default=0)
    gate2_text = db.Column(db.Text, nullable=True)      # человекочитаемая часть гейта
    gate2_json = db.Column(db.Text, nullable=True)      # machine-блок (questions | resolved)
    gate2_answer = db.Column(db.Text, nullable=True)    # ответ автора на точечные вопросы
    gate2_error = db.Column(db.Text, nullable=True)
    # Подсказка оркестратору из прохода 3: вести ли числовую ветку вообще.
    simulation_blocking = db.Column(db.Boolean, nullable=True)

    def last_json_dict(self):
        import json
        return json.loads(self.last_json) if self.last_json else None

    def map_list(self):
        """Карта узлов со статусами ok/unclear/missing/absent (может быть пустой)."""
        import json
        return json.loads(self.map_json) if self.map_json else []

    def issues(self):
        import json
        return json.loads(self.issues_json) if self.issues_json else []

    @property
    def blocking_issues(self):
        return [i for i in self.issues() if i.get("severity") == "error"]

    def questions(self) -> list:
        """Вопросы последнего прохода — их показывают и рядом с формой ответа."""
        return ((self.last_json_dict() or {}).get("questions")) or []

    def gate2_json_dict(self):
        import json
        return json.loads(self.gate2_json) if self.gate2_json else None

    @property
    def is_final_round(self) -> bool:
        """Достигнут ли предел — на этом раунде агент обязан завершить сверку."""
        return self.round >= self.MAX_ROUNDS

    @property
    def can_gate2(self) -> bool:
        """Можно ли ещё открыть второй гейт (он однократный)."""
        return self.gate2_runs < self.MAX_GATE2 and self.gate2_status != self.GATE2_DONE


class GameSpec(db.Model):
    """Структура игры (game_spec.json) от агента-извлеченца.

    Второй шаг ветки анализа: принимает подтверждённый текст от «Понимания
    игры» и раскладывает его в core/text + gaps + ambiguities. Одна запись на
    (document_id, game_index), как и у MirrorSession.
    """

    __tablename__ = "game_specs"

    # Приёмка структуры автором. Правка — ОТДЕЛЬНЫЙ бюджет от раундов понимания:
    # раунды закрывают «агент не понял», а правка — «агент понял, но собрал не
    # так». Смешивать нельзя, иначе автор, честно израсходовавший раунды на
    # хорошие вопросы, остался бы без права поправить кривую структуру.
    MAX_REVISIONS = 1

    STATUS_PENDING = "pending"    # структура показана, автор ещё не решил
    STATUS_ACCEPTED = "accepted"  # автор принял
    STATUS_REVISED = "revised"    # автор отправлял правку (бюджет исчерпан)

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=False, index=True)
    game_index = db.Column(db.Integer, nullable=False, default=1)
    spec_json = db.Column(db.Text, nullable=True)   # весь ответ извлеченца целиком
    error = db.Column(db.Text, nullable=True)       # причина, если извлечение не удалось
    # Нарушения контракта, найденные самопроверкой (review/extractor.validate).
    # Хранятся, потому что промпт предупреждает: эти поломки БЕСШУМНЫ — без
    # явного списка о них никто бы не узнал.
    issues_json = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(16), nullable=False, default=STATUS_PENDING)
    revisions = db.Column(db.Integer, nullable=False, default=0)  # сколько правок потрачено
    revision_note = db.Column(db.Text, nullable=True)             # что автору не понравилось
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    document = db.relationship("Document", backref="game_specs")

    __table_args__ = (
        db.UniqueConstraint("document_id", "game_index", name="uq_spec_doc_game"),
    )

    def spec_dict(self):
        import json
        return json.loads(self.spec_json) if self.spec_json else None

    def issues(self):
        import json
        return json.loads(self.issues_json) if self.issues_json else []

    @property
    def blocking_issues(self):
        """Нарушения уровня error — из-за них дальше по конвейеру поедет неверное."""
        return [i for i in self.issues() if i.get("severity") == "error"]

    @property
    def can_revise(self) -> bool:
        """Осталась ли у автора попытка поправить структуру."""
        return self.revisions < self.MAX_REVISIONS


class GameSkeleton(db.Model):
    """Код скелета-симулятора от агента «Симуляционист».

    Третий шаг ветки анализа, после приёмки структуры автором: агент берёт
    game_spec.core и заполняет шаблон game_skeleton.py под конкретную игру, либо
    честно объявляет игру несимулируемой (`simulatable=False`) с причиной.

    ВАЖНО: сервис этот код НЕ исполняет — на отдельном экране показывается только
    сам сгенерированный скелет, без статистик и прочих результатов (требование
    автора). Прогон в песочнице и сбор stats — более поздний шаг конвейера.

    Одна запись на (document_id, game_index), как у MirrorSession и GameSpec.
    """

    __tablename__ = "game_skeletons"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=False, index=True)
    game_index = db.Column(db.Integer, nullable=False, default=1)
    simulatable = db.Column(db.Boolean, nullable=True)     # None = ещё не прогоняли
    code = db.Column(db.Text, nullable=True)               # заполненный game_skeleton.py
    player_counts_json = db.Column(db.Text, nullable=True)  # [2,3,4] от агента
    assumptions_json = db.Column(db.Text, nullable=True)    # список ключевых допущений
    reason = db.Column(db.Text, nullable=True)             # почему несимулируема
    missing_json = db.Column(db.Text, nullable=True)       # чего не хватило в core
    error = db.Column(db.Text, nullable=True)              # причина, если агент недоступен
    # Метаданные v6 целиком: границы модели, которые читают следующие агенты
    # (metric_responds_immediately, coalition_expressible, subjective_actions,
    # fixed_length, content_scale, ignored_components, hooks_filled, pattern).
    # Умолчание здесь = дезинформация следующего шага, поэтому храним как есть.
    meta_json = db.Column(db.Text, nullable=True)
    # Нарушения контракта, найденные самопроверкой (review/simulationist.validate).
    # Все они БЕСШУМНЫ: код отрабатывает и выдаёт правдоподобные числа.
    issues_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    document = db.relationship("Document", backref="game_skeletons")

    __table_args__ = (
        db.UniqueConstraint("document_id", "game_index", name="uq_skeleton_doc_game"),
    )

    def _load(self, raw):
        import json
        return json.loads(raw) if raw else []

    def player_counts(self):
        return self._load(self.player_counts_json)

    def assumptions(self):
        return self._load(self.assumptions_json)

    def missing(self):
        return self._load(self.missing_json)

    def meta(self):
        import json
        return json.loads(self.meta_json) if self.meta_json else {}

    def issues(self):
        return self._load(self.issues_json)

    @property
    def blocking_issues(self):
        return [i for i in self.issues() if i.get("severity") == "error"]

    @property
    def is_ready(self) -> bool:
        """Прогон уже был (успешный или с честным отказом), не ошибка провайдера."""
        return self.simulatable is not None and not self.error


class BalanceReport(db.Model):
    """Статистика прогона скелета и вердикт агента «Оценщик статистик».

    Четвёртый шаг ветки анализа. Хранит две вещи: сам STATS_JSON (числа) и
    Finding_balance.json (их интерпретация) — они разделены намеренно, потому
    что статистика получается один раз, а переоценить её агентом можно заново
    (например, сменив провайдера) без повторного прогона симуляции.
    """

    __tablename__ = "balance_reports"

    # Откуда взялась статистика. Основной путь — автор сам прогнал скелет
    # (хоть на online-python.com, как написано в шапке шаблона) и вставил JSON:
    # так сервис вообще не исполняет сгенерированный моделью код.
    SOURCE_PASTED = "pasted"
    SOURCE_LOCAL = "local"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=False, index=True)
    game_index = db.Column(db.Integer, nullable=False, default=1)
    stats_json = db.Column(db.Text, nullable=True)     # STATS_JSON базового прогона
    stats_source = db.Column(db.String(16), nullable=True)
    # DIAG_JSON — диагностический блок скелета v4 (runs[] + exploit_search).
    # Его читает агент-диагност; «Оценщику статистик» он не подаётся вовсе,
    # иначе на одну игру появятся два несогласованных вердикта.
    # None у скелетов v3 — это не ошибка, а отсутствие данных.
    diag_json = db.Column(db.Text, nullable=True)
    report_json = db.Column(db.Text, nullable=True)    # Finding_balance.json
    issues_json = db.Column(db.Text, nullable=True)    # нарушения самопроверки
    error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    document = db.relationship("Document", backref="balance_reports")

    __table_args__ = (
        db.UniqueConstraint("document_id", "game_index", name="uq_balance_doc_game"),
    )

    def stats(self):
        import json
        return json.loads(self.stats_json) if self.stats_json else None

    def diag(self):
        import json
        return json.loads(self.diag_json) if self.diag_json else None

    def report(self):
        import json
        return json.loads(self.report_json) if self.report_json else None

    def issues(self):
        import json
        return json.loads(self.issues_json) if self.issues_json else []

    @property
    def blocking_issues(self):
        return [i for i in self.issues() if i.get("severity") == "error"]

    @property
    def has_stats(self) -> bool:
        return bool(self.stats_json)


class DiagnosisReport(db.Model):
    """Отчёт агента-диагноста: исполнение методички балансной верификации.

    Агент двухпроходный, и оба прохода хранятся раздельно: между ними
    оркестратор исполняет заказанные прогоны, и триаж нужен на входе второго
    прохода. Перезапускать первый проход при повторном заходе не обязательно —
    решение принимает оркестратор, а не модель.
    """

    __tablename__ = "diagnosis_reports"

    PHASE_NONE = "none"          # ещё не запускали
    PHASE_TRIAGE = "triage"      # триаж сделан, прогоны не исполнены
    PHASE_DONE = "done"          # вердикты получены

    MAX_ATTEMPTS = 3             # цикл ремонта ведёт оркестратор

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=False, index=True)
    game_index = db.Column(db.Integer, nullable=False, default=1)
    phase = db.Column(db.String(16), nullable=False, default=PHASE_NONE)
    attempt_number = db.Column(db.Integer, nullable=False, default=1)

    triage_json = db.Column(db.Text, nullable=True)          # выход прохода 1
    triage_issues_json = db.Column(db.Text, nullable=True)
    # Что реально заказано и что обрезано бюджетом. Обрезанное хранится, потому
    # что его тесты обязаны получить честный n/a: budget_exceeded, а не исчезнуть.
    kept_requests_json = db.Column(db.Text, nullable=True)
    dropped_requests_json = db.Column(db.Text, nullable=True)
    extra_runs_json = db.Column(db.Text, nullable=True)       # результаты доп. прогонов
    runs_error = db.Column(db.Text, nullable=True)

    findings_json = db.Column(db.Text, nullable=True)         # выход прохода 2
    issues_json = db.Column(db.Text, nullable=True)
    error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    document = db.relationship("Document", backref="diagnosis_reports")

    __table_args__ = (
        db.UniqueConstraint("document_id", "game_index", name="uq_diagnosis_doc_game"),
    )

    def _load(self, raw, default=None):
        import json
        return json.loads(raw) if raw else default

    def triage(self):
        return self._load(self.triage_json)

    def triage_issues(self):
        return self._load(self.triage_issues_json, [])

    def kept_requests(self):
        return self._load(self.kept_requests_json, [])

    @property
    def budget_used(self) -> int:
        """Фактически исполненные прогоны, а не заявленное агентом число."""
        return len(self.kept_requests())

    @property
    def budget_limit(self) -> int:
        from review.diagnost import BUDGET_LIMIT
        return BUDGET_LIMIT

    def dropped_requests(self):
        return self._load(self.dropped_requests_json, [])

    def extra_runs(self):
        return self._load(self.extra_runs_json)

    def findings(self):
        return self._load(self.findings_json)

    def issues(self):
        return self._load(self.issues_json, [])

    @property
    def blocking_issues(self):
        return [i for i in self.issues() if i.get("severity") == "error"]

    @property
    def critical_flags(self):
        return (self.findings() or {}).get("critical_flags") or []

    @property
    def coverage(self):
        return (self.findings() or {}).get("coverage_summary") or {}

    @property
    def unmeasured_count(self) -> int:
        """Сколько тестов НЕ выполнено по причинам, которые не значат «всё хорошо».

        `mechanic_absent` сюда не входит: механики нет — проверять нечего.
        Остальные четыре причины означают «не измерено», и это обязано доехать
        до финального отчёта, а не раствориться в общем «претензий нет».
        """
        br = (self.coverage.get("n_a_breakdown") or {})
        return sum(int(br.get(k) or 0) for k in
                   ("no_data", "method_blind", "search_incomplete", "budget_exceeded"))


class SynthesisReport(db.Model):
    """Финальный отчёт: общий балл, надёжность оценки и решение о ревизии.

    Записей на игру несколько — по одной на итерацию. Хранить только последнюю
    нельзя: смысл цикла в том, поднялся ли балл после правки, а без предыдущего
    значения этого не видно ни автору, ни коду (агент получает `previous_score`
    на вход и обязан сказать, что изменилось).

    Оркестратор читает отсюда `revision_required`, а НЕ сам балл: игра может
    набрать проходные 6.4 и при этом иметь незакрытую эксплойт-петлю.
    """

    __tablename__ = "synthesis_reports"

    # Бюджет кругов «синтез → авто-редизайн → пересчёт» — тот же счётчик, что у
    # авто-редизайнера (RedesignAttempt.MAX_ACCEPTED_ATTEMPTS): круг делает он.
    # Держать здесь второе число значило бы завести второй бюджет на один цикл.
    MAX_REVISIONS = 3

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=False, index=True)
    game_index = db.Column(db.Integer, nullable=False, default=1)
    attempt_number = db.Column(db.Integer, nullable=False, default=1)

    result_json = db.Column(db.Text, nullable=True)      # весь ответ агента
    reference_json = db.Column(db.Text, nullable=True)   # эталонный расчёт кода
    issues_json = db.Column(db.Text, nullable=True)      # расхождения с эталоном
    # Дублируем в колонки то, по чему ходит оркестратор и что показывается в
    # списках: иначе каждый переход по конвейеру разбирает JSON целиком.
    overall_score = db.Column(db.Float, nullable=True)
    score_confidence = db.Column(db.String(16), nullable=True)
    revision_required = db.Column(db.Boolean, nullable=False, default=False)
    error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    document = db.relationship("Document", backref="synthesis_reports")

    __table_args__ = (
        db.UniqueConstraint("document_id", "game_index", "attempt_number",
                            name="uq_synthesis_doc_game_attempt"),
    )

    def _load(self, raw, default=None):
        import json
        return json.loads(raw) if raw else default

    def result(self):
        return self._load(self.result_json)

    def reference(self):
        return self._load(self.reference_json)

    def issues(self):
        return self._load(self.issues_json, [])

    @property
    def blocking_issues(self):
        return [i for i in self.issues() if i.get("severity") == "error"]

    @property
    def report_md(self) -> str:
        return (self.result() or {}).get("report_md") or ""

    @property
    def categories(self) -> list:
        return (self.result() or {}).get("categories") or []

    @property
    def top_priorities(self) -> list:
        return (self.result() or {}).get("top_priorities") or []

    @property
    def revision_reason(self) -> list:
        return (self.result() or {}).get("revision_reason") or []


class RedesignAttempt(db.Model):
    """Попытка авто-редизайна: одна правка структуры по найденным недочётам.

    Записей на игру несколько — по одной на попытку. Это не прихоть: агент
    получает на вход историю прошлых правок (`previous_changes`), иначе цикл
    «поднял порог → перелетел в too_hard → опустил порог → снова too_easy»
    крутится до исчерпания счётчика, каждый раз выглядя как осмысленная работа.

    Правка автору ПРЕДЛАГАЕТСЯ, а не применяется молча: агент меняет структуру
    его игры, и последнее слово за ним (так же прямо сказано и в промпте —
    «в ветке с живым автором изменения подаются как предложение»).
    """

    __tablename__ = "redesign_attempts"

    # Два НЕЗАВИСИМЫХ бюджета, и смешивать их нельзя.
    #
    # MAX_ACCEPTED_ATTEMPTS — сколько раз игре позволено измениться. Это бюджет
    # правок структуры: цикл ведёт оркестратор, агент круг не решает.
    #
    # MAX_PROPOSALS — сколько предложений вообще разрешено сгенерировать, любого
    # статуса. Это потолок расходов на модель. Считать отказы в первый бюджет
    # было бы наказанием за разборчивость: автор, отклонивший три неудачных
    # предложения, остался бы без права на ремонт вовсе.
    MAX_ACCEPTED_ATTEMPTS = 3
    MAX_PROPOSALS = 6

    STATUS_PROPOSED = "proposed"   # предложено, автор ещё не решил
    STATUS_ACCEPTED = "accepted"   # применено к структуре
    STATUS_REJECTED = "rejected"   # автор отказался

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=False, index=True)
    game_index = db.Column(db.Integer, nullable=False, default=1)
    attempt_number = db.Column(db.Integer, nullable=False, default=1)
    mode = db.Column(db.String(24), nullable=True)         # A_technical | D_diagnostic | B_full_critique
    trigger_reason = db.Column(db.Text, nullable=True)     # почему агента вообще позвали
    result_json = db.Column(db.Text, nullable=True)        # весь ответ агента
    issues_json = db.Column(db.Text, nullable=True)        # нарушения самопроверки
    spec_before_json = db.Column(db.Text, nullable=True)   # структура до правки (для отката)
    status = db.Column(db.String(16), nullable=False, default=STATUS_PROPOSED)
    error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    document = db.relationship("Document", backref="redesign_attempts")

    __table_args__ = (
        db.UniqueConstraint("document_id", "game_index", "attempt_number",
                            name="uq_redesign_doc_game_attempt"),
    )

    @classmethod
    def next_attempt_number(cls, document_id, game_index) -> int:
        """Следующий номер попытки: максимальный существующий + 1.

        Номер — это идентификатор записи, а не счётчик бюджета. Пока их считали
        одним числом («принятых + 1»), отклонённая попытка занимала номер, и
        следующее предложение падало на уникальном индексе: отказ автора от
        правки ломал сервис.
        """
        from sqlalchemy import func
        current = db.session.query(func.max(cls.attempt_number)).filter_by(
            document_id=document_id, game_index=game_index).scalar()
        return (current or 0) + 1

    def result(self):
        import json
        return json.loads(self.result_json) if self.result_json else None

    def issues(self):
        import json
        return json.loads(self.issues_json) if self.issues_json else []

    def spec_before(self):
        import json
        return json.loads(self.spec_before_json) if self.spec_before_json else None

    @property
    def blocking_issues(self):
        return [i for i in self.issues() if i.get("severity") == "error"]

    @property
    def changes(self):
        return (self.result() or {}).get("changes") or []
