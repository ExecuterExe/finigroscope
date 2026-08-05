# Развёртывание экосистемы на VPS

Два самостоятельных процесса за одним nginx. Общий у них только веб-сервер:
свои каталоги, свои `.env`, своё окружение Python не пересекаются.

| | ФинИгроСкоп | Генератор игр |
|---|---|---|
| каталог | `/srv/ecosystem/finigroskop` | `/srv/ecosystem/generator` |
| порт | 127.0.0.1:5000 | 127.0.0.1:8000 |
| запуск | gunicorn (WSGI) | `python app.py` (свой http.server) |
| адрес снаружи | `/` | `/generator/` |
| хранилище | SQLite + `uploads/` | нет, состояние в браузере |

## Порядок установки

```bash
git clone <репозиторий> /srv/ecosystem
cd /srv/ecosystem
python3 -m venv .venv
.venv/bin/pip install -r finigroskop/requirements.txt gunicorn
# генератору зависимости не нужны: он живёт на стандартной библиотеке
```

Настройки — **по одному `.env` на сервис**, из шаблонов. Общего `.env` в корне
быть не должно: имена переменных у сервисов совпадают, а смысл разный
(`LLM_PROVIDER`, `LLM_TIMEOUT`, имена моделей), и один файл на двоих молча выдал
бы каждому чужие значения.

```bash
cp finigroskop/.env.example finigroskop/.env
cp generator/.env.example   generator/.env
```

В `finigroskop/.env` на сервере обязательно:

- `FINIGROSKOP_SECRET=<длинная случайная строка>` — иначе подпись сессий идёт
  общеизвестным значением по умолчанию;
- `GENERATOR_URL=/generator/` — адрес кнопки перехода в шапке;
- **`FINIGROSKOP_RESET` не задавать никогда.** Эта переменная разрешает стереть
  хранилище при старте: каждый перезапуск сервиса уничтожал бы все документы.

В `generator/.env`:

- `OPENROUTER_API_KEY=<ключ>`;
- `FINIGROSKOP_URL=/` — обратная кнопка в шапке страницы генератора;
- `LENS_API_URL=http://127.0.0.1:5000` — **полный** адрес ФинИгроСкопа для
  вызовов сервер-сервер. Это НЕ то же самое, что `FINIGROSKOP_URL`: та строка
  предназначена браузеру и на сервере равна `/`, по которому Python постучаться
  не сможет;
- `LENS_API_TOKEN=<та же строка, что в finigroskop/.env>`.

Секрет одинаковый в обоих файлах:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Оценка по линзам тратит деньги на модель, поэтому без токена эндпоинт
`/api/lenses/module` отвечает 403 везде, кроме сервера разработки. Наружу его
выставлять не надо: генератор ходит к ФинИгроСкопу напрямую на 127.0.0.1, минуя
nginx.

Юниты и конфиг веб-сервера:

```bash
cp deploy/systemd/finigroskop.service deploy/systemd/generator.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now finigroskop generator

cp deploy/nginx/ecosystem.conf /etc/nginx/sites-available/ecosystem
ln -s /etc/nginx/sites-available/ecosystem /etc/nginx/sites-enabled/
# подставить свой домен вместо ВАШ-ДОМЕН
nginx -t && systemctl reload nginx
```

## На что смотреть, если что-то не работает

**У ФинИгроСкопа ровно один воркер gunicorn (`-w 1`).** Очередь фоновых задач
живёт внутри процесса (`finigroskop/jobs.py`). При двух воркерах задача считается
в одном процессе, а страница статуса приходит в другой — пользователь видит
вечное «идёт…». Масштабировать можно только вместе с заменой очереди на внешнюю.

**`EnvironmentFile` у systemd — не то же самое, что `.env` для python-dotenv.**
Комментарии и `KEY=value` он понимает, но значение с пробелами нужно взять в
кавычки, а подстановок вида `$OTHER` там нет. Если сервис не стартует —
`journalctl -u finigroskop -n 50` покажет строку, на которой он споткнулся.

**Генератор подключён по пути, а не поддоменом.** Это сознательный выбор: при
поддомене куку единого входа пришлось бы выдавать на `.домен`, а по пути она
работает как обычная. Цена выбора — страница генератора обязана ходить в свой
API относительными адресами (это уже сделано) и открываться строго по адресу со
слэшем на конце; редирект `/generator` → `/generator/` в конфиге для этого и
стоит.

**Логи генератора (`generator/logs/`) в репозиторий не попадают** — там журнал
вызовов модели с кусками пользовательских ответов. На сервере каталог создаётся
сам, за его размером надо следить.

## Следующий шаг: единый вход

Пока вход в ФинИгроСкопе отключён (`AUTH_DISABLED = True` в `finigroskop/app.py`),
и генератор открыт всем. Когда дойдут руки, владельцем пользователей остаётся
ФинИгроСкоп, а генератор закрывается на стороне nginx, без единой строки про
авторизацию в его собственном коде:

```nginx
    location /generator/ {
        auth_request /_auth;
        auth_request_set $user $upstream_http_x_user;
        proxy_set_header X-User $user;
        proxy_pass http://127.0.0.1:8000/;
        # …остальные заголовки как в ecosystem.conf…
    }

    location = /_auth {
        internal;
        proxy_pass              http://127.0.0.1:5000/api/whoami;
        proxy_pass_request_body off;
        proxy_set_header        Content-Length "";
        proxy_set_header        X-Original-URI $request_uri;
    }
```

Порядок работ: снять `AUTH_DISABLED`, прогнать весь набор проверок ФинИгроСкопа
(часть из них ходит по страницам без входа), задать `FINIGROSKOP_SECRET`,
добавить в ФинИгроСкоп эндпоинт `GET /api/whoami` (200 для вошедшего, 401 для
остальных) и только потом включать `auth_request`.
