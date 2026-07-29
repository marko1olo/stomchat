"""
Провал аварийной отметки медиа перестал заводить бесконечный круг молча.

На аварийных путях process_media_message (таймаут и исключение) стояла отметка
«разобрано», а вокруг неё — `except Exception: pass`, двумя одинаковыми блоками.
Отметка существует ровно для того, чтобы строка не осталась в состоянии «ещё не
разбирали»: recover_pending_media_analysis такую строку тянет из Telegram и качает
файл ЗАНОВО на каждом рестарте. То есть молчал именно тот отказ, который и заводит
вечный круг: бюджет зрения тратится на сообщение, уже признанное неразбираемым, и
ни одной строки в журнале.

Плюс три немых пути, где врач видит последствие своими глазами:
- голый `except:` на удалении служебного сообщения `.weekly` ловил и
  CancelledError с KeyboardInterrupt — остановка бота выглядела как «нет прав»;
- два `except Exception: pass` вокруг event.answer() — спиннер на кнопке у врача
  крутится до таймаута клиента, и он жмёт снова.

Проверки поведенческие: смотрят на возврат функции и на записи в журнале.

Запуск: python test_media_loop_guard.py
"""
import asyncio
import logging
import os
import shutil
import sys
import tempfile
import time

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_medialoop_")
os.environ["STOMCHAT_LOG_PATH"] = os.path.join(_TMPDIR, "t.log")

import config  # noqa: E402

# База уводится в temp ДО импорта database. Запись сюда и так подменяется
# заглушкой, но полагаться на это нельзя: уберут заглушку в одной проверке — и
# набор начнёт писать в боевую stomat_bot.db, а заметит это только
# test_isolation.py, и то по статическому признаку. Дешевле увести путь.
config.DB_PATH = os.path.join(_TMPDIR, "isolated.db")

import database  # noqa: E402
import main  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


class Msg:
    def __init__(self, mid):
        self.id = mid


class LogTrap(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def at_least(self, level):
        return [r for r in self.records if r.levelno >= level]

    def text(self):
        out = []
        for r in self.records:
            try:
                out.append(r.getMessage())
            except Exception:
                out.append(str(r.msg))
        return "\n".join(out)


def run_mark(messages, msg_id, reason, stub):
    """Подменяет запись в базу на заглушку, ловит журнал main, возвращает (итог, журнал)."""
    real = database.update_media_description
    trap = LogTrap()
    logger = logging.getLogger("main")
    logger.addHandler(trap)
    prev_level, prev_prop = logger.level, logger.propagate
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    database.update_media_description = stub
    try:
        return asyncio.run(main._mark_media_processed(messages, msg_id, reason)), trap
    finally:
        database.update_media_description = real
        logger.removeHandler(trap)
        logger.setLevel(prev_level)
        logger.propagate = prev_prop


CALLS = []


async def stub_ok(mid, value):
    CALLS.append((mid, value))
    return True


async def stub_raises(mid, value):
    CALLS.append((mid, value))
    raise RuntimeError("база занята")


async def stub_raises_on_second(mid, value):
    CALLS.append((mid, value))
    if len(CALLS) >= 2:
        raise RuntimeError("база отвалилась на второй строке")
    return True


async def stub_hangs(mid, value):
    CALLS.append((mid, value))
    await asyncio.sleep(60)


print("[1] Здоровый путь: отмечены все строки, паники в журнале нет")
CALLS.clear()
marked, trap = run_mark([Msg(11), Msg(12), Msg(13)], 900, "таймаут обработки", stub_ok)
check("отмечены все три строки", marked == 3, f"вернулось {marked}")
check("в базу ушла именно отметка '-'",
      [v for _, v in CALLS] == ["-", "-", "-"], f"вызовы: {CALLS}")
check("отмечены те же id, что пришли",
      [m for m, _ in CALLS] == [11, 12, 13], f"вызовы: {CALLS}")
check("ни одной записи ERROR на здоровом пути",
      not trap.at_least(logging.ERROR),
      f"лишнее: {[r.getMessage() for r in trap.at_least(logging.ERROR)]}")

print("\n[2] Провал отметки больше не молчит — это и есть исходный дефект")
CALLS.clear()
marked, trap = run_mark([Msg(21), Msg(22)], 901, "таймаут обработки", stub_raises)
check("функция не бросает наружу (аварийный путь не должен рушиться)", marked == 0,
      f"вернулось {marked}")
check("в журнале есть ERROR", bool(trap.at_least(logging.ERROR)),
      "именно это глотал except Exception: pass")
check("названо последствие — строка останется в догоне",
      "догоне" in trap.text(), f"журнал: {trap.text()!r}")
check("названо, что файл будет качаться заново",
      "заново" in trap.text(), f"журнал: {trap.text()!r}")
check("в журнале есть msg_id, иначе искать нечего",
      "901" in trap.text(), f"журнал: {trap.text()!r}")
check("в журнале есть класс исключения",
      "RuntimeError" in trap.text(), f"журнал: {trap.text()!r}")
check("причина аварийного пути названа",
      "таймаут" in trap.text(), f"журнал: {trap.text()!r}")

print("\n[3] Частичный провал: видно, сколько строк успели отметить")
CALLS.clear()
marked, trap = run_mark([Msg(31), Msg(32), Msg(33)], 902, "исключение при обработке",
                        stub_raises_on_second)
check("вернулось число УСПЕШНЫХ, а не всех", marked == 1, f"вернулось {marked}")
check("в журнале сказано 'отмечено 1 из 3'",
      "1" in trap.text() and "3" in trap.text(), f"журнал: {trap.text()!r}")
check("частичный провал — тоже ERROR", bool(trap.at_least(logging.ERROR)))

print("\n[4] Зависшая база не держит аварийный путь вечно")
CALLS.clear()
_t0 = time.monotonic()
marked, trap = run_mark([Msg(41)], 903, "таймаут обработки", stub_hangs)
_elapsed = time.monotonic() - _t0
check("вернулось 0 — отметка не прошла", marked == 0, f"вернулось {marked}")
check("внутренний таймаут сработал, а не ждали 60 с", _elapsed < 30,
      f"заняло {_elapsed:.1f} с — таймаут 10 с не соблюдён")
check("таймаут записи тоже попал в журнал как ERROR",
      bool(trap.at_least(logging.ERROR)), f"журнал: {trap.text()!r}")
check("вложенность бюджета: внутренний таймаут 10 с меньше внешнего 30 с",
      _elapsed < 30, f"заняло {_elapsed:.1f} с")

print("\n[5] Немые пути в main.py закрыты")
SRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py"),
           encoding="utf-8").read()
_code_lines = [ln for ln in SRC.splitlines() if ln.strip().startswith("except")]
check("голого `except:` в коде main.py не осталось",
      not any(ln.strip() == "except:" for ln in _code_lines),
      f"найдено: {[ln.strip() for ln in _code_lines if ln.strip() == 'except:']}")
check("дублирующие блоки отметки свёрнуты в одну функцию",
      SRC.count("_mark_media_processed") == 3,
      f"вхождений {SRC.count('_mark_media_processed')} (ожидается def + 2 вызова)")
check("на аварийных путях медиа не осталось `except Exception: pass`",
      "database.update_media_description(message.id, \"-\"), timeout=10)\n        except Exception:\n            pass" not in SRC)
check("спиннер: event.answer() больше не под немым pass",
      SRC.count("await event.answer()\n            except Exception:\n                pass") == 0
      and SRC.count("await event.answer()\n            except Exception:\n                pass") == 0)

print("\n[6] Проверки выше ловят поломку")


def collect_exc(stub):
    """Возвращает исключение заглушки, чтобы убедиться, что она правда бросает."""
    async def _inner():
        try:
            await stub(1, "-")
            return None
        except Exception as exc:
            return exc
    return asyncio.run(_inner())


check("заглушка базы действительно вызывалась, иначе [1] ничего не значит",
      len(CALLS) > 0, "ни одного вызова записи — проверки пустые")
check("исключение заглушки не проглатывается самой заглушкой",
      isinstance(collect_exc(stub_raises), RuntimeError),
      "если заглушка молчит, проверка [2] проходит на пустом месте")
check("здоровая заглушка НЕ бросает, иначе [1] и [2] неразличимы",
      collect_exc(stub_ok) is None)

shutil.rmtree(_TMPDIR, ignore_errors=True)

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
