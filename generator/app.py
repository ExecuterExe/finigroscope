# -*- coding: utf-8 -*-
"""Локальный сервер: отдаёт страницу опросника и проксирует запросы к модели.

Почему запросы к модели идут через сервер, а не из браузера напрямую:
ключ OpenRouter не должен попадать в код страницы — иначе его увидит любой,
кто откроет исходник вкладки. Страница обращается к своему же /api/llm/*,
ключ остаётся на стороне Python.
"""

import json
import sys
import traceback
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import reloader
from config import config
from agents import components as components_agent
from agents import lens_review
from agents import mechanics
from agents import module_auditor
from agents import pipeline
from agents import story
import jobs
import llm
import params as params_module

BASE_DIR = Path(__file__).resolve().parent

# Отдаём только то, что действительно нужно странице. Белый список, а не
# чёрный: иначе очередной новый файл рядом (.env, ключи, выгрузки) уехал бы
# в браузер по прямому запросу.
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
}

MAX_BODY = 256 * 1024


class Handler(BaseHTTPRequestHandler):
    server_version = "GeneratorIgr"

    # ---------- ответы ----------

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_page(self, status, text):
        body = ("<h1>%d — %s</h1>" % (status, text)).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------- GET ----------

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/api/llm/status":
            self.send_json(config.status())
            return

        if path == "/api/links":
            self.send_json(config.links())
            return

        self.serve_static(path)

    def serve_static(self, path):
        if path in ("/", ""):
            file_path = BASE_DIR / "index.html"
        else:
            relative = path.lstrip("/")
            # скрытые файлы (.env и прочие) наружу не отдаём никогда
            if any(part.startswith(".") for part in relative.split("/")):
                self.send_error_page(404, "страница не найдена")
                return
            file_path = BASE_DIR / relative

        resolved = file_path.resolve()
        allowed = (
            resolved.is_file()
            and BASE_DIR in resolved.parents
            and resolved.suffix in CONTENT_TYPES
        )
        if not allowed:
            self.send_error_page(404, "страница не найдена")
            return

        data = resolved.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES[resolved.suffix])
        self.send_header("Content-Length", str(len(data)))
        # Страницу и её скрипты браузер держать в кеше НЕ должен. Иначе после
        # правки app.js обычное обновление отдаёт старый файл, и получается
        # пара «старая страница + новый сервер» — с ошибками, которые в коде уже
        # исправлены. Ритуал «жми Ctrl+F5» — не решение, а способ забыть.
        # Это сервер разработки, экономить на его трафике нечего.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(data)

    # ---------- POST ----------

    def do_POST(self):
        path = self.path.split("?")[0]
        routes = {
            "/api/llm/complete": self.route_complete,
            "/api/generate/mechanics": self.route_mechanics,
            "/api/audit/mechanics": self.route_audit_mechanics,
            "/api/lenses/module": self.route_lenses,
            "/api/lenses/status": self.route_lens_status,
            "/api/pipeline/mechanics": self.route_pipeline,
            "/api/pipeline/story": self.route_pipeline_story,
            "/api/pipeline/features": self.route_pipeline_features,
            "/api/pipeline/rules": self.route_pipeline_rules,
            "/api/pipeline/package": self.route_pipeline_package,
            "/api/pipeline/status": self.route_pipeline_status,
            "/api/pipeline/cancel": self.route_pipeline_cancel,
            "/api/components": self.route_components,
        }
        handler = routes.get(path)
        if not handler:
            # Сообщение не просто «404»: чаще всего этот ответ означает не
            # опечатку в адресе, а что страница новее работающего сервера.
            # Прошлый текст «неизвестный адрес» об этом молчал, и на разгадку
            # уходили минуты.
            self.send_json({
                "error": "Сервер не знает адрес %s. Известны: %s. "
                         "Чаще всего это значит, что страница новее кода в "
                         "запущенном сервере — перезапустите generator/app.py."
                         % (path, ", ".join(sorted(routes))),
                "stage": "маршрут",
                "unknown_route": path,
            }, 404)
            return

        payload = self.read_json()
        if payload is None:
            return

        try:
            handler(payload)
        except Exception as failure:                      # noqa: BLE001
            # Любой неучтённый сбой обязан стать ОТВЕТОМ. Без этого исключение
            # долетало до ThreadingHTTPServer, тот молча закрывал соединение, и
            # страница получала «сервер не ответил» — неотличимо от зависшей
            # сети. Полная трассировка идёт в консоль, наружу — тип ошибки.
            traceback.print_exc()
            self.send_json({"error": "Внутренняя ошибка сервера: %s. "
                                     "Подробности в окне сервера."
                                     % type(failure).__name__,
                            "stage": "сервер"}, 500)

    def read_json(self):
        """Тело запроса как словарь. При ошибке сама отвечает и возвращает None."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.send_json({"error": "некорректный Content-Length"}, 400)
            return None
        if length <= 0 or length > MAX_BODY:
            self.send_json({"error": "пустое или слишком большое тело запроса"}, 400)
            return None

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json({"error": "тело запроса не является JSON"}, 400)
            return None
        if not isinstance(payload, dict):
            self.send_json({"error": "ожидается объект JSON"}, 400)
            return None
        return payload

    # Первый шаг конвейера: ответы опросника -> варианты игрового цикла.
    def route_mechanics(self, payload):
        try:
            params = params_module.build(payload.get("answers"))
        except params_module.ParamsError as error:
            self.send_json({"error": str(error), "stage": "параметры"}, 400)
            return

        try:
            result = mechanics.generate(params)
        except mechanics.NotEnoughMechanics as error:
            # 422: параметры корректны, но библиотека их не покрывает.
            # Модель не зовём — платить за заведомо провальный запрос незачем.
            self.send_json({"error": str(error), "stage": "библиотека",
                            "dropped": error.dropped}, 422)
            return
        except llm.LLMError as error:
            self.send_json({"error": str(error), "stage": "модель"}, 502)
            return

        result["params"] = params
        self.send_json(result, 200 if result.get("ok") else 502)

    # Второй шаг конвейера: выбранный пользователем вариант цикла уходит
    # аудитору. Проход mechanics — предыдущих модулей ещё нет.
    def route_audit_mechanics(self, payload):
        try:
            params = params_module.build(payload.get("answers"))
        except params_module.ParamsError as error:
            self.send_json({"error": str(error), "stage": "параметры"}, 400)
            return

        module = payload.get("module")
        if not isinstance(module, dict) or not module:
            self.send_json({"error": "нужно поле module с выбранным вариантом",
                            "stage": "модуль"}, 400)
            return

        try:
            result = module_auditor.audit_module("mechanics", module, params)
        except (module_auditor.AuditError, llm.LLMError) as error:
            self.send_json({"error": str(error), "stage": "аудитор"}, 502)
            return

        answer = result.to_dict()
        # подписи пунктов: в map ходят идентификаторы, показывать нужно текст
        answer["labels"] = module_auditor.checklist_labels("mechanics")
        self.send_json(answer)

    # Третий шаг конвейера: модуль, прошедший аудит без критичных замечаний,
    # уходит на качественную оценку по линзам Шелла. Агент живёт в ФинИгроСкопе
    # (см. agents/lens_review.py), здесь только стыковка с опросником.
    def route_lenses(self, payload):
        try:
            params = params_module.build(payload.get("answers"))
        except params_module.ParamsError as error:
            self.send_json({"error": str(error), "stage": "параметры"}, 400)
            return

        module = payload.get("module")
        if not isinstance(module, dict) or not module:
            self.send_json({"error": "нужно поле module с оцениваемым модулем",
                            "stage": "модуль"}, 400)
            return

        audit = payload.get("audit")
        if not isinstance(audit, dict) or not audit:
            self.send_json({"error": "нужно поле audit с ответом аудитора: "
                                     "линзы идут ПОСЛЕ него",
                            "stage": "аудит"}, 400)
            return

        phase = payload.get("phase") or "mechanics"
        try:
            # Только ставим в очередь. Ждать здесь нельзя: оценка идёт минутами,
            # и обработчик, зависший на всё это время, лишает страницу
            # возможности показать, что работа идёт. Состояние спрашивают
            # отдельно — /api/lenses/status.
            started = lens_review.submit(phase, module, params, audit)
        except lens_review.LensError as error:
            self.send_json({"error": str(error), "stage": "линзы"}, 502)
            return

        self.send_json(started)

    def route_lens_status(self, payload):
        """Состояние оценки. Страница спрашивает раз в пару секунд."""
        try:
            state = lens_review.poll(payload.get("job_id"))
        except lens_review.LensError as error:
            self.send_json({"error": str(error), "stage": "линзы"}, 502)
            return

        self.send_json(state)

    # Полный проход по ТЗ: до трёх попыток «генерация → аудит → линзы», и если
    # порог не взят ни разу — берётся попытка с наибольшим баллом.
    def route_pipeline(self, payload):
        try:
            params = params_module.build(payload.get("answers"))
        except params_module.ParamsError as error:
            self.send_json({"error": str(error), "stage": "параметры"}, 400)
            return

        attempts = payload.get("attempts")
        if not isinstance(attempts, int) or not 1 <= attempts <= pipeline.MAX_ATTEMPTS:
            attempts = pipeline.MAX_ATTEMPTS

        # Ставим в очередь и сразу отвечаем: проход делает до девяти обращений
        # к моделям и идёт минутами.
        job_id = jobs.submit(lambda progress: pipeline.run(params, progress, attempts))
        self.send_json({"job_id": job_id, "attempts_allowed": attempts,
                        "threshold": pipeline.PASSING_SCORE}, 202)

    # Этап 3 ТЗ: сюжет и артефакты поверх ПРИНЯТЫХ механик.
    def route_pipeline_story(self, payload):
        try:
            params = params_module.build(payload.get("answers"))
        except params_module.ParamsError as error:
            self.send_json({"error": str(error), "stage": "параметры"}, 400)
            return

        # Ворота проверяет СЕРВЕР по результату прохода механик, а не страница.
        # Разница принципиальная: «механики приняты» — это вывод из двух платных
        # проверок, и верить в него на слово браузеру нельзя. Поэтому на вход
        # идёт номер завершённой задачи, а модуль сервер достаёт из неё сам.
        chain, error = self.accepted_chain(payload, [("mechanics_job_id", "mechanics")])
        if error:
            self.send_json(error[0], error[1])
            return

        attempts = self._attempts(payload)
        module = chain[0]["module"]
        built_on = [self.chain_note(c) for c in chain]

        job_id = jobs.submit(
            lambda progress: pipeline.run_story(params, progress, module, attempts,
                                                built_on=built_on))
        self.send_json({"job_id": job_id, "attempts_allowed": attempts,
                        "threshold": pipeline.PASSING_SCORE,
                        "depth": story.depth_of(params),
                        "built_on": built_on}, 202)

    # Этап 4 ТЗ: концепция, особенности и помощь отстающим поверх ОБОИХ
    # принятых модулей.
    def route_pipeline_features(self, payload):
        try:
            params = params_module.build(payload.get("answers"))
        except params_module.ParamsError as error:
            self.send_json({"error": str(error), "stage": "параметры"}, 400)
            return

        chain, error = self.accepted_chain(payload, [
            ("mechanics_job_id", "mechanics"),
            ("story_job_id", "story"),
        ])
        if error:
            self.send_json(error[0], error[1])
            return

        attempts = self._attempts(payload)
        built_on = [self.chain_note(c) for c in chain]
        mechanics_module, story_module = chain[0]["module"], chain[1]["module"]

        job_id = jobs.submit(
            lambda progress: pipeline.run_features(
                params, progress, mechanics_module, story_module, attempts,
                built_on=built_on))
        self.send_json({"job_id": job_id, "attempts_allowed": attempts,
                        "threshold": pipeline.PASSING_SCORE,
                        "built_on": built_on}, 202)

    # Этап 6 ТЗ: краткие правила и советы. Последний проход конвейера — стоит
    # на трёх принятых модулях и на расчёте компонентов этапа 5.
    def route_pipeline_rules(self, payload):
        try:
            params = params_module.build(payload.get("answers"))
        except params_module.ParamsError as error:
            self.send_json({"error": str(error), "stage": "параметры"}, 400)
            return

        chain, error = self.accepted_chain(payload, [
            ("mechanics_job_id", "mechanics"),
            ("story_job_id", "story"),
            ("features_job_id", "features"),
        ])
        if error:
            self.send_json(error[0], error[1])
            return

        # Компоненты считает КОД по таблицам, а не агент, поэтому они не звено
        # цепочки и номера задачи у них нет: считаем прямо здесь, из тех же
        # ответов опросника. Второй проход («final») — потому что правилам нужны
        # и количества, и материалы, а материал появляется только в нём.
        try:
            computed = components_agent.final(params)
        except components_agent.ComponentsError as error:
            self.send_json({
                "error": "Правила пишутся по рассчитанным компонентам, а расчёт "
                         "не выполнен: %s" % error,
                "stage": "компоненты"}, 422)
            return

        attempts = self._attempts(payload)
        built_on = [self.chain_note(c) for c in chain]
        modules = {link["phase"]: link["module"] for link in chain}
        rows = computed["components"]

        job_id = jobs.submit(
            lambda progress: pipeline.run_rules(params, progress, modules, rows,
                                                attempts, built_on=built_on))
        self.send_json({"job_id": job_id, "attempts_allowed": attempts,
                        "threshold": pipeline.PASSING_SCORE,
                        "built_on": built_on,
                        "components": computed}, 202)

    # Упаковка: полное описание и game_spec.json. Через него сгенерированная
    # игра попадает на симуляционный этап ФинИгроСкопа.
    def route_pipeline_package(self, payload):
        try:
            params = params_module.build(payload.get("answers"))
        except params_module.ParamsError as error:
            self.send_json({"error": str(error), "stage": "параметры"}, 400)
            return

        chain, error = self.accepted_chain(payload, [
            ("mechanics_job_id", "mechanics"),
            ("story_job_id", "story"),
            ("features_job_id", "features"),
            ("rules_job_id", "rules"),
        ])
        if error:
            self.send_json(error[0], error[1])
            return

        try:
            computed = components_agent.final(params)
        except components_agent.ComponentsError as error:
            self.send_json({
                "error": "Упаковка описывает рассчитанные компоненты, а расчёт "
                         "не выполнен: %s" % error,
                "stage": "компоненты"}, 422)
            return

        modules = {link["phase"]: link["module"] for link in chain
                   if link["phase"] != "rules"}
        # Правила приходят из своей задачи тем же путём, что и модули, но в
        # описание идут не как модуль, а как готовый текст разделов.
        rules_variant = next(link["module"] for link in chain
                             if link["phase"] == "rules")
        built_on = [self.chain_note(c) for c in chain]
        rows = computed["components"]

        job_id = jobs.submit(
            lambda progress: pipeline.run_packaging(
                params, progress, modules, rows, rules_variant, built_on=built_on))
        self.send_json({"job_id": job_id, "built_on": built_on,
                        "components": computed}, 202)

    @staticmethod
    def _attempts(payload):
        attempts = payload.get("attempts")
        if not isinstance(attempts, int) or not 1 <= attempts <= pipeline.MAX_ATTEMPTS:
            return pipeline.MAX_ATTEMPTS
        return attempts

    @classmethod
    def accepted_chain(cls, payload, wanted):
        """Все основания этапа разом. Возвращает (список звеньев, ошибка).

        `wanted` — пары «поле запроса → фаза» В ПОРЯДКЕ ЭТАПОВ. Порядок
        сохраняется в ответе: аудитор читает список принятых модулей как
        последовательность, и переставить их местами значит соврать ему о том,
        что на чём построено.

        Согласие автора (`accept_anyway`) распространяется на ВСЕ звенья сразу.
        Разделять его по этапам можно, но пока незачем: автор соглашается идти
        дальше с тем, что есть, а не выборочно прощает один модуль из двух.
        """
        force = bool(payload.get("accept_anyway"))
        chain = []
        for field, phase in wanted:
            link, error = cls.accepted_module(payload.get(field), phase, force=force)
            if error:
                return None, error
            chain.append(link)
        return chain, None

    @staticmethod
    def chain_note(link):
        """Звено цепочки без самого модуля — то, что уходит в отчёт и на экран.

        Модуль отсюда убран намеренно: он и так лежит в результате прохода, а в
        отчёте занял бы место дважды и уехал бы в браузер третьим экземпляром.
        """
        return {k: v for k, v in link.items() if k != "module"}

    @staticmethod
    def accepted_module(job_id, phase, force=False):
        """Модуль предыдущего этапа для следующего. Возвращает (цепочка, ошибка).

        Ошибка — готовая пара (тело ответа, код). Отдельным методом, потому что
        следующий этап («особенности») будет доставать так же модули механик и
        сюжета, и правило приёмки должно быть записано один раз.

        В цепочку идёт cleaned_module аудита, а не сырой вывод генератора: это
        контракт ModuleChain, и нарушать его здесь значило бы отдать сюжету
        вариант, который аудитор мог поправить.

        `force` — решение АВТОРА продолжить на непринятом модуле. По ТЗ (этапы
        2–6, пункт 3) такое право у него есть: «если рекомендации выступают
        просто в роли совета, программа может пропустить к следующему этапу».
        Само по себе оно ничего не прощает: непринятость едет дальше в поле
        `accepted` и обязана быть видна в итоге. Молчаливое «ну ладно» — как раз
        то, чего здесь быть не должно.

        Что `force` НЕ разрешает: строить этап на пропавшей задаче, на чужой
        фазе, на незавершённом проходе и на результате без проверенного модуля.
        Это не решения автора, а отсутствие входных данных.
        """
        if not job_id:
            return None, ({"error": "Не указан номер прохода предыдущего этапа.",
                           "stage": "цепочка"}, 400)

        state = jobs.status(job_id)
        if state is None:
            return None, ({
                "error": "Проход этапа «%s» не найден: сервер перезапускался "
                         "или результат устарел. Запустите этап заново — иначе "
                         "неизвестно, приняты его модули или нет." % phase,
                "stage": "цепочка"}, 404)

        if state["status"] != jobs.DONE:
            return None, ({"error": "Этап «%s» ещё не завершён (%s)."
                                    % (phase, state["status"]),
                           "stage": "цепочка"}, 409)

        result = state.get("result") or {}
        if result.get("phase", "mechanics") != phase:
            return None, ({"error": "Указан проход этапа «%s», а нужен «%s»."
                                    % (result.get("phase"), phase),
                           "stage": "цепочка"}, 400)

        accepted = bool(result.get("passed"))
        if not accepted and not force:
            return None, ({
                "error": "Модуль этапа «%s» не принят: %s. Следующий этап "
                         "строится на нём, поэтому запускать его рано."
                         % (phase, result.get("verdict") or "порог не взят"),
                "stage": "цепочка",
                # Признак для страницы: отказ снимается решением автора, а не
                # починкой. Без него кнопка «продолжить» была бы догадкой.
                "can_accept_anyway": True,
                "score": (result.get("best") or {}).get("score"),
                "threshold": result.get("threshold")}, 409)

        module = ((result.get("best") or {}).get("audit") or {}).get("cleaned_module")
        if not isinstance(module, dict) or not module:
            return None, ({
                "error": "В результате этапа «%s» нет проверенного модуля "
                         "(cleaned_module). Запустите этап заново." % phase,
                "stage": "цепочка"}, 409)

        return {
            "phase": phase,
            "job_id": job_id,
            "module": module,
            "accepted": accepted,
            "override": not accepted,
            "score": (result.get("best") or {}).get("score"),
            "threshold": result.get("threshold"),
            "verdict": result.get("verdict"),
        }, None

    def route_pipeline_status(self, payload):
        state = jobs.status(payload.get("job_id"))
        if state is None:
            self.send_json({"error": "Проход не найден — возможно, сервер "
                                     "перезапускался. Запустите заново.",
                            "stage": "проход"}, 404)
            return
        self.send_json(state)

    def route_pipeline_cancel(self, payload):
        stopped = jobs.request_cancel(payload.get("job_id"))
        self.send_json({
            "stopped": stopped,
            "note": ("Остановим после текущего обращения к модели — прервать "
                     "сам запрос нельзя, он уже отправлен."
                     if stopped else "Проход уже завершился."),
        })

    # Этап 5 ТЗ: количество компонентов и материал. Отвечает СИНХРОННО и
    # мгновенно — расчёт по таблицам, модель не вызывается. Заводить под него
    # задачу в очереди значило бы городить ожидание там, где его нет.
    def route_components(self, payload):
        try:
            params = params_module.build(payload.get("answers"))
        except params_module.ParamsError as error:
            self.send_json({"error": str(error), "stage": "параметры"}, 400)
            return

        which = payload.get("pass") or "base"
        if which not in ("base", "final"):
            self.send_json({"error": "поле pass должно быть base или final",
                            "stage": "компоненты"}, 400)
            return

        try:
            if which == "base":
                result = components_agent.base(params)
            else:
                result = components_agent.final(params)
        except components_agent.ComponentsError as error:
            self.send_json({"error": str(error), "stage": "компоненты"}, 422)
            return

        self.send_json(result)

    def route_complete(self, payload):
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            self.send_json({"error": "нужно поле messages со списком сообщений"}, 400)
            return

        try:
            result = llm.complete(
                messages,
                tier=payload.get("tier", "pro"),
                temperature=payload.get("temperature"),
                max_tokens=payload.get("max_tokens"),
            )
        except llm.LLMError as error:
            # 502: ошибка не у нас, а на стороне провайдера или в настройках
            self.send_json({"error": str(error)}, 502)
            return

        self.send_json(result)

    def log_message(self, format, *args):
        print("%s - %s" % (self.address_string(), format % args))


if __name__ == "__main__":
    # Автоперезагрузка: родительский процесс следит за кодом и перезапускает
    # этот же файл. Без неё правка маршрута молча не доезжала до работающего
    # сервера, а страница (её браузер перечитывает сам) оказывалась новее кода —
    # и отвечала «неизвестный адрес» на маршрут, который в исходниках есть.
    if config.autoreload and not reloader.is_child():
        sys.exit(reloader.supervise())

    # Построчный вывод. Под автоперезагрузкой сервер — дочерний процесс, его
    # stdout уходит в канал родителю, а Python в канал пишет БЛОКАМИ по 8 КБ.
    # Из-за этого журнал запросов, предупреждение «модель недоступна» и строки
    # [линзы]/[проход] копились в буфере и не показывались вовсе, пока процесс
    # жив, — то есть ровно тогда, когда они и нужны.
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except AttributeError:                       # очень старый Python
        pass

    host, port = config.bind()

    print("Сервер запущен: http://%s:%d" % (host, port))
    problem = config.problem()
    if problem:
        print("Модель пока недоступна: %s" % problem)
    else:
        print("OpenRouter готов: pro=%s, flash=%s"
              % (config.model_pro, config.model_flash))
    print("Для остановки закройте терминал или завершите процесс.")

    # ThreadingHTTPServer, а не HTTPServer: генерация занимает до минуты, и на
    # однопоточном сервере на это время переставала открываться сама страница.
    ThreadingHTTPServer((host, port), Handler).serve_forever()
