"""
FloodWait перестал наказывать врача и перестал прятаться внутри await.

Три правки, вытекающие из инвентаризации 151 вызова Telegram API.

1. Оба цикла приглашений считали FloodWait виной ВРАЧА. `except Exception` ловил
   в одну кучу UserIsBlockedError (врач заблокировал бота — его решение) и
   FloodWaitError (мы шлём слишком часто — наша скорость), и счётчик
   ping_failures рос на любой из них. Трёх хватало: MAX_PING_FAILURES = 3
   проверяется на входе, обнуляется только успешной отправкой, а отправки больше
   не будет — живой врач выпадал из приглашений НАВСЕГДА.

2. Причина флуда, а не симптом: рассылка брала 20% активных кандидатов без
   потолка и без паузы. При 749 врачах это до 150 личных сообщений подряд,
   уходящих настолько быстро, насколько успевает сеть.

3. telethon по умолчанию держит flood_sleep_threshold = 60, и он НЕ задавался.
   При request_retries = 10 это до 600 секунд сна внутри одного
   `await send_message` — молча, потому что строка про сон идёт уровнем INFO у
   логгера telethon.client.users, а runtime_guard приглушает telethon до ERROR.
   Замер по журналам: в bot.log.1 (109 798 строк) flood-строк три, и все три —
   транспортный HTTP 429; RPC-уровень по этим журналам не измерить в принципе.
   Так что порог ставится по механике библиотеки, а не по замеру, и здесь это
   сказано прямо.

Запуск: python test_flood_discipline.py
"""
import io
import os
import shutil
import sys
import tempfile

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_flood_")
os.environ["STOMCHAT_LOG_PATH"] = os.path.join(_TMPDIR, "t.log")

import assistant as A  # noqa: E402
import tg_safety  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


class FakeFloodWait(Exception):
    """Подделка FloodWaitError: у настоящей есть .seconds и такое же имя класса."""
    def __init__(self, seconds):
        super().__init__(f"A wait of {seconds} seconds is required")
        self.seconds = seconds


FakeFloodWait.__name__ = "FloodWaitError"


class FakeBlocked(Exception):
    pass


FakeBlocked.__name__ = "UserIsBlockedError"


print("\n[1] FloodWait отличается от вины врача")
# Если классификатор их не различает, ВСЯ правка ниже бессмысленна: обе ветки
# пойдут одним путём независимо от того, что написано в обработчике.
check("FloodWait опознан как флуд",
      tg_safety.classify(FakeFloodWait(30)) == tg_safety.KIND_FLOOD,
      f"got {tg_safety.classify(FakeFloodWait(30))!r}")
check("блокировка ботом флудом НЕ считается",
      tg_safety.classify(FakeBlocked()) != tg_safety.KIND_FLOOD,
      f"got {tg_safety.classify(FakeBlocked())!r}")
check("секунды ожидания достаются из исключения",
      tg_safety.flood_wait_seconds(FakeFloodWait(45)) == 45,
      f"got {tg_safety.flood_wait_seconds(FakeFloodWait(45))!r}")
check("блокировка — вина врача, флуд — нет",
      tg_safety.classify(FakeBlocked()) == tg_safety.KIND_TERMINAL,
      f"got {tg_safety.classify(FakeBlocked())!r}")

print("\n[2] Счётчик неудач растёт только на вине врача")
# Сценарий отказа: врач ходит в чат, читает, но в ЛС не пишет. Бот трижды
# попробовал его пригласить, трижды поймал FloodWait из-за своей же рассылки — и
# больше не пригласит никогда. Врач об этом не узнает: приглашений просто нет.
SOURCE = io.open("assistant.py", encoding="utf-8").read()
CODE = "\n".join(l for l in SOURCE.split("\n") if not l.lstrip().startswith("#"))
flood_guards = CODE.count("tg_safety.classify(send_err) == tg_safety.KIND_FLOOD")
check("оба цикла приглашений проверяют флуд", flood_guards == 2,
      f"найдено {flood_guards} проверок из 2 — цикл без проверки снова спишет "
      f"нашу скорость на врача")
# Порядок важен: проверка обязана стоять ДО инкремента, иначе она ничего не
# спасает.
for label, marker in (("ЛС", "Failed to deliver DM ping"),
                      ("чат", "Failed to send group activity ping")):
    tail = CODE.split(marker, 1)[0]
    guard_at = tail.rfind("KIND_FLOOD")
    incr_at = tail.rfind("ping_failures\", 0) + 1")
    check(f"в цикле {label} проверка флуда стоит до инкремента",
          guard_at > 0 and guard_at < incr_at, f"guard={guard_at} incr={incr_at}")
# После флуда рассылка обязана прекратиться: продолжать в закрытую дверь значит
# копить наказание и терять остальных из выборки. Смотрим, что в обеих
# флуд-ветках стоит выход из цикла, а не continue.
for label, marker in (("ЛС", "DM ping hit FloodWait"),
                      ("чат", "Group ping hit FloodWait")):
    branch = SOURCE.split(marker, 1)
    check(f"в цикле {label} флуд-ветка есть", len(branch) == 2, "ветка не найдена")
    if len(branch) == 2:
        check(f"в цикле {label} после флуда цикл прерывается",
              "break" in branch[1][:600].split("except")[0],
              "продолжение рассылки под флудом копит наказание")
# Инвариант в поведении: 3 флуда подряд НЕ должны исчерпать лимит.
check("трёх флудов не хватает, чтобы исчерпать лимит",
      A.MAX_PING_FAILURES == 3,
      f"MAX_PING_FAILURES={A.MAX_PING_FAILURES}: смысл проверки в том, что "
      f"именно столько флудов раньше и хватало")

print("\n[3] Пачка приглашений ограничена и разнесена по времени")
# 20% от 749 врачей — 150 сообщений подряд. Потолок и пауза стоят как ПРИЧИНА
# флуда: без них любая правка обработчика лечит следствие.
check("потолок на пачку объявлен", isinstance(A.GROUP_PING_BATCH_MAX, int)
      and 0 < A.GROUP_PING_BATCH_MAX <= 50, f"got {A.GROUP_PING_BATCH_MAX!r}")
check("пауза между отправками объявлена", A.GROUP_PING_DELAY_SECONDS >= 0.5,
      f"got {A.GROUP_PING_DELAY_SECONDS!r}")
check("потолок ниже двадцати процентов от 749 врачей",
      A.GROUP_PING_BATCH_MAX < 749 * 0.20,
      f"{A.GROUP_PING_BATCH_MAX} против 150 — иначе потолок не срабатывает никогда")

# Поведение, а не наличие константы: проверка «константа объявлена» проходила и
# после того, как применение потолка убрали из кода.
all_doctors = list(range(1, 750))
targets = A.select_ping_targets(all_doctors)
check("пачка на 749 врачах урезана потолком",
      len(targets) == A.GROUP_PING_BATCH_MAX,
      f"выбрано {len(targets)} — без потолка ушло бы 150 сообщений подряд")
check("адресаты не повторяются", len(set(targets)) == len(targets))
check("все адресаты из числа кандидатов", set(targets) <= set(all_doctors))
check("малая выборка потолком не режется",
      len(A.select_ping_targets(list(range(10)))) == 2,
      f"got {len(A.select_ping_targets(list(range(10))))}")
check("один кандидат получает приглашение", len(A.select_ping_targets([42])) == 1)
check("пустой список не роняет выбор", A.select_ping_targets([]) == [])
check("выборка случайная, а не первые по списку",
      len({tuple(sorted(A.select_ping_targets(all_doctors))) for _ in range(5)}) > 1,
      "одна и та же группа врачей получала бы все приглашения")
# Полный цикл не должен занимать больше часа: планировщик ходит раз в час.
cycle_seconds = A.GROUP_PING_BATCH_MAX * A.GROUP_PING_DELAY_SECONDS
check("пачка укладывается в час между запусками", cycle_seconds < 3600,
      f"{cycle_seconds:.0f} с на пачку")
check("урезание пачки попадает в журнал", "Group ping batch capped" in SOURCE,
      "молча разослать 25 из 150 и промолчать — это ложь о покрытии")

print("\n[4] Сон telethon внутри await ограничен сверху")
MAIN_SRC = io.open("main.py", encoding="utf-8").read()
check("порог задан обоим клиентам", MAIN_SRC.count("flood_sleep_threshold=") == 2,
      f"найдено {MAIN_SRC.count('flood_sleep_threshold=')} из 2: "
      f"клиент без порога спит по стандартным 60 с")
import main  # noqa: E402
check("порог существенно ниже стандартных 60 с",
      0 < main.TELETHON_FLOOD_SLEEP_THRESHOLD <= 30,
      f"got {main.TELETHON_FLOOD_SLEEP_THRESHOLD}")
check("порог не нулевой", main.TELETHON_FLOOD_SLEEP_THRESHOLD > 0,
      "ноль превратил бы пятисекундную задержку в потерю сообщения на ~120 "
      "вызовах с голым except")
check("оба клиента получили одно значение",
      MAIN_SRC.count("flood_sleep_threshold=TELETHON_FLOOD_SLEEP_THRESHOLD") == 2,
      "разные пороги у слушателя и писателя объяснить нечем")
worst_case = main.TELETHON_FLOOD_SLEEP_THRESHOLD * 10  # request_retries=10
check("худший случай сна стал меньше прежних 600 с", worst_case < 600,
      f"{worst_case} с против 600 с")
check("значение переопределяется переменной окружения",
      "STOMCHAT_FLOOD_SLEEP_THRESHOLD" in MAIN_SRC,
      "на живом боте подкрутить будет нечем")

print("\n[5] Модуль устойчивости импортируется в ядро без побочных эффектов")
check("ядро импортирует tg_safety", "import tg_safety" in SOURCE,
      "модуль устойчивости построен и снова никем не вызывается")
TG_SRC = io.open("tg_safety.py", encoding="utf-8").read()
check("telethon импортируется лениво, а не на уровне модуля",
      "\nimport telethon" not in TG_SRC and "\nfrom telethon" not in TG_SRC,
      "жёсткий импорт уронил бы тесты и любую машину без telethon")
check("логирование модуль не настраивает", "basicConfig" not in TG_SRC)

print("\n[6] Проверки выше ловят поломку")
check("классификатор действительно смотрит на класс, а не на текст",
      tg_safety.classify(Exception("A wait of 30 seconds is required"))
      != tg_safety.KIND_FLOOD,
      "иначе любое сообщение со словом wait объявлялось бы флудом")
check("неизвестное исключение считается терминальным, а не повторяемым",
      not tg_safety.is_retryable(Exception("что-то новое")),
      "повторять неизвестное — это жечь бюджет вслепую")

shutil.rmtree(_TMPDIR, ignore_errors=True)

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
