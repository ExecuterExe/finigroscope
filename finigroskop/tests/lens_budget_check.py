# -*- coding: utf-8 -*-
"""Бюджет времени на вызов модели: каскад не имеет права идти сколько угодно.

Сетевых вызовов нет: подменяется только _call_model.

Что здесь ловится. Провайдер перебирает бесплатные модели каскадом, а таймаут
применялся к КАЖДОЙ из них по отдельности. Вызов оценщика по линзам с 300 с на
пяти кандидатах шёл до 25 минут — и это не теория: на живом прогоне страница
показала ошибку по своему сроку (330 с), а перебор в ФинИгроСкопе продолжался
дальше. Худший из возможных исходов: пользователь видит сбой, работа идёт,
время тратится, и связать одно с другим нельзя.

Правило, которое проверяется: срок ОБЩИЙ на весь каскад, а не на модель.
"""
import io
import os
import sys
import time

sys.path.insert(0, ".")
os.environ["LLM_PROVIDER_FORCE"] = "mock"

from review import lens_evaluator as L                    # noqa: E402
from review import llm_provider as P                      # noqa: E402

checks = {}


class Slow(P.OpenAICompatProvider):
    """Провайдер, у которого каждая модель «висит» до своего таймаута."""

    name = "slow"
    TITLE = "Медленный"
    KEY_ENV = None
    MODEL_ENV = None
    API_BASE = "https://example.invalid/v1"
    DEFAULT_MODEL = "m1"
    FALLBACK_MODELS = ("m1", "m2", "m3", "m4", "m5")

    def __init__(self, hang=None, **options):
        super().__init__(**options)
        self.api_key = "x"
        self.model_explicit = False
        self.seen = []            # (модель, отведённый таймаут)
        self.hang = hang or {}    # модель -> сколько «висеть» на самом деле

    def _call_model(self, model, system, user, **opts):
        allowed = self._timeout(opts)
        self.seen.append((model, allowed))
        # «Зависание» имитируем сдвигом часов, а не сном: проверка обязана быть
        # быстрой, иначе её перестанут запускать.
        time.monotonic = _clock.advance(min(self.hang.get(model, 0), allowed))
        raise P._RetryableModelError("%s: сеть/таймаут" % model)


class _Clock:
    """Управляемые часы: время идёт только когда мы этого хотим."""

    def __init__(self):
        self.now = 1000.0
        self.real = time.monotonic

    def advance(self, seconds):
        self.now += seconds
        return self.read

    def read(self):
        return self.now


_clock = _Clock()
time.monotonic = _clock.read
P.time.monotonic = _clock.read


# ============================================================================
# ЧАСТЬ 1. Без бюджета поведение прежнее — каскад перебирает всё
# ============================================================================
p = Slow(hang={m: 120 for m in Slow.FALLBACK_MODELS})
try:
    p._complete("s", "u", timeout=120)
except RuntimeError as exc:
    checks["без срока перебраны все пять"] = len(p.seen) == 5
    checks["без срока в ошибке сказано, сколько пробовали"] = "5 из 5" in str(exc)

# ============================================================================
# ЧАСТЬ 2. С бюджетом перебор прекращается, когда время вышло
# ============================================================================
_clock.now = 1000.0
p = Slow(hang={m: 120 for m in Slow.FALLBACK_MODELS})
start = _clock.now
try:
    p._complete("s", "u", timeout=120, deadline=start + 300)
except RuntimeError as exc:
    text = str(exc)

checks["с бюджетом перебраны НЕ все"] = len(p.seen) < 5
checks["уложились в бюджет"] = _clock.now - start <= 300
checks["в ошибке названо число попыток"] = "из 5" in text
checks["в ошибке сказано про нехватку времени"] = "времени не осталось" in text

# Ключевое число: 300 с бюджета при 120 с на модель — это две полные попытки
# плюс третья на остаток (60 с), а не пять по 120 (=600) и не одна на 300.
# Остаток не выбрасывается: шанс на укороченную попытку лучше, чем гарантия
# отказа, — но и растянуть бюджет она не может.
checks["две полные попытки плюс третья на остаток"] = len(p.seen) == 3
checks["третьей досталось меньше полного таймаута"] = p.seen[2][1] < 120

# ============================================================================
# ЧАСТЬ 3. Последней попытке достаётся только остаток, а не полный таймаут
# ============================================================================
_clock.now = 1000.0
p = Slow(hang={"m1": 100, "m2": 100, "m3": 100})
start = _clock.now
try:
    p._complete("s", "u", timeout=120, deadline=start + 250)
except RuntimeError:
    pass

allowed = [t for _, t in p.seen]
checks["первой дан полный таймаут"] = allowed[0] == 120
checks["последней дан только остаток"] = allowed[-1] < 120
checks["остаток не отрицательный"] = all(t > 0 for t in allowed)

# ============================================================================
# ЧАСТЬ 4. Слишком малый остаток попытку не начинает
# ============================================================================
_clock.now = 1000.0
p = Slow(hang={"m1": 100})
try:
    p._complete("s", "u", timeout=120, deadline=_clock.now + 105)
except RuntimeError:
    pass
checks["огрызок времени не тратится на новую попытку"] = len(p.seen) == 1

# ============================================================================
# ЧАСТЬ 5. Закреплённая модель каскада не разворачивает
# ============================================================================
_clock.now = 1000.0
p = Slow(hang={"m1": 50})
p.model = "m1"
p.model_explicit = True          # автор выбрал модель осознанно
try:
    p._complete("s", "u", timeout=120, deadline=_clock.now + 300)
except RuntimeError:
    pass
checks["явную модель не подменяем"] = [m for m, _ in p.seen] == ["m1"]

# ============================================================================
# ЧАСТЬ 6. Согласованность сроков между сервисами
# ============================================================================
checks["предел одной попытки меньше общего бюджета"] = L.LLM_TIMEOUT < L.TOTAL_BUDGET
# Иначе первая же зависшая модель съест весь бюджет, и каскад, ради которого
# он и заведён, не сработает ни разу.
checks["бюджета хватает минимум на две попытки"] = (
    L.TOTAL_BUDGET >= L.LLM_TIMEOUT * 2)
checks["на повтор оставлен осмысленный запас"] = (
    0 < L.MIN_RETRY_SECONDS < L.TOTAL_BUDGET)

# Живые прогоны показали, что оценка иногда не укладывается и в 360 с, которые
# ждал генератор. Запас взят сознательно большой: это ПРЕДЕЛ, а не план —
# уложившаяся за минуту оценка вернётся за минуту. А вот сдаться раньше, чем
# оценщик закончил, значит выбросить оплаченную работу.
checks["общий бюджет не меньше 1000 с"] = L.TOTAL_BUDGET >= 1000
# Каскад перебирает несколько моделей; на три полные попытки бюджета хватать
# обязано, иначе последние кандидаты не получат шанса никогда.
checks["бюджета хватает на три попытки"] = L.TOTAL_BUDGET >= L.LLM_TIMEOUT * 3

# Генератор обязан ждать ДОЛЬШЕ, чем работает оценщик: иначе он сдастся раньше
# срока, покажет ошибку, а работа продолжится — ровно то, что и случилось.
GENERATOR_CONFIG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath("."))), "generator", "config.py")
raw = ""
for candidate in (os.path.join("..", "generator", "config.py"), GENERATOR_CONFIG):
    if os.path.isfile(candidate):
        raw = io.open(candidate, encoding="utf-8").read()
        break
if raw:
    import re

    found = re.search(r'_number\("LENS_TIMEOUT",\s*(\d+)', raw)
    checks["ожидание генератора найдено в его config.py"] = bool(found)
    if found:
        checks["генератор ждёт дольше бюджета оценщика"] = (
            int(found.group(1)) > L.TOTAL_BUDGET)
else:
    checks["config.py генератора доступен для сверки сроков"] = False


for label, ok in checks.items():
    print(("OK  " if ok else "FAIL") + " | " + label)
assert all(checks.values()), "часть проверок провалилась"
print(f"\nВСЁ ОК ({len(checks)} проверок)")
