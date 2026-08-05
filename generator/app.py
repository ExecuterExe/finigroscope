# -*- coding: utf-8 -*-
"""Локальный сервер: отдаёт страницу опросника и проксирует запросы к модели.

Почему запросы к модели идут через сервер, а не из браузера напрямую:
ключ OpenRouter не должен попадать в код страницы — иначе его увидит любой,
кто откроет исходник вкладки. Страница обращается к своему же /api/llm/*,
ключ остаётся на стороне Python.
"""

import json
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

from config import config
from agents import mechanics
from agents import module_auditor
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
        self.end_headers()
        self.wfile.write(data)

    # ---------- POST ----------

    def do_POST(self):
        path = self.path.split("?")[0]
        routes = {
            "/api/llm/complete": self.route_complete,
            "/api/generate/mechanics": self.route_mechanics,
            "/api/audit/mechanics": self.route_audit_mechanics,
        }
        handler = routes.get(path)
        if not handler:
            self.send_json({"error": "неизвестный адрес"}, 404)
            return

        payload = self.read_json()
        if payload is not None:
            handler(payload)

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
    host = "127.0.0.1"
    port = 8000

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
