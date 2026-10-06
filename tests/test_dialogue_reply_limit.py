import os
import sys
import tempfile
import unittest

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_test_dialogue_limit_")
os.environ["STOMCHAT_DATA_DIR"] = _TMPDIR
os.environ["STOMCHAT_DB_PATH"] = os.path.join(_TMPDIR, "stomat_bot.db")

import assistant
import runtime_guard

runtime_guard.SUMMARY_STATUS_PATH = os.path.join(_TMPDIR, "bot_summary_status.json")
runtime_guard.HEARTBEAT_PATH = os.path.join(_TMPDIR, "bot_heartbeat.json")
runtime_guard.WATCHDOG_DUMP_PATH = os.path.join(_TMPDIR, "bot_watchdog_dump.txt")
assistant.STATE_PATH = os.path.join(_TMPDIR, "assistant_state.json")
assistant.STATE_TMP_PATH = assistant.STATE_PATH + ".tmp"
assistant.STATE_BAK_PATH = assistant.STATE_PATH + ".bak"

PASS = []
FAIL = []

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))

print("\n[1] Проверка константы MAX_DIALOGUE_BOT_REPLIES")
check("Константа определена", hasattr(assistant, "MAX_DIALOGUE_BOT_REPLIES"))
check("Лимит равен 6 (до 6 ответов бота в ветке)", getattr(assistant, "MAX_DIALOGUE_BOT_REPLIES", 0) == 6)

print("\n[2] Проверка условий ветвления в диалоге")
limit = assistant.MAX_DIALOGUE_BOT_REPLIES
check("При 1 ответе бота диалог разрешен", 1 < limit)
check("При 2 ответах бота диалог разрешен", 2 < limit)
check("При 3 ответах бота диалог разрешен (раньше блокировался)", 3 < limit)
check("При 5 ответах бота диалог разрешен", 5 < limit)
check("При 6 ответах бота ветка закрывается", not (6 < limit))
check("При 7 ответах бота ветка закрывается", not (7 < limit))

print(f"\n{'='*62}\nИТОГО: PASSED={len(PASS)}   FAILED={len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
