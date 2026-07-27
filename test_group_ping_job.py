"""
Проактивные приглашения в чат: реальный прогон check_and_send_group_activity_pings.

Регресс на боевой инцидент: новым записям подставлялось last_activity
"2000-01-01", после чего почасовой check_and_send_pm_pings видел «молчит 26
лет» и рассылал «ты пропал и не писал уже 2 дня» ВСЕМ, кто писал в ЛС за
последние 30 дней, включая написавших вчера. Проверять это поиском подстроки
в исходнике бессмысленно — здесь job исполняется по-настоящему, с настоящей
базой SQLite и настоящим файлом состояния; заглушены LLM и отправка в Telegram.

Запуск: python test_group_ping_job.py
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import config

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_gping_")
config.DB_PATH = os.path.join(_TMPDIR, "test_messages.db")
config.SOURCE_CHAT_ID = -1001234567890

import database
import assistant

assistant.STATE_PATH = os.path.join(_TMPDIR, "assistant_state.json")
assistant.STATE_TMP_PATH = assistant.STATE_PATH + ".tmp"
assistant.STATE_BAK_PATH = assistant.STATE_PATH + ".bak"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


SENT = []
LLM_CALLS = []


class FakeResponse:
    def __init__(self, text):
        self.text = text


class FakeBot:
    async def send_message(self, entity=None, message=None, parse_mode=None, **kw):
        SENT.append(entity)
        return None


BOT = FakeBot()
_hot = True


async def fake_llm(prompt, status_ctx=None, timeout=None):
    LLM_CALLS.append(status_ctx)
    payload = {"is_hot": _hot, "topic": "границы уступа", "teaser": "в чате горячо спорят"}
    return FakeResponse(json.dumps(payload, ensure_ascii=False)), None


assistant.generate_gemini_text_async = fake_llm


def write_state(pings):
    with open(assistant.STATE_PATH, "w", encoding="utf-8") as handle:
        json.dump({"pm_pings": pings}, handle, ensure_ascii=False)


def read_pings():
    return assistant.load_state().get("pm_pings", {})


def reset():
    SENT.clear()
    LLM_CALLS.clear()


async def seed_chat_and_users(user_ids):
    """Пять сообщений в группе (порог job) и активные собеседники в ЛС."""
    for i, uid in enumerate(user_ids):
        await database.save_pm_message(uid, "User", f"вопрос от {uid}")
    for i in range(6):
        await database.save_message(
            msg_id=9000 + i,
            sender_id=user_ids[i % len(user_ids)],
            sender_name=f"Врач {i}",
            sender_username=None,
            text=f"обсуждаем уступ, реплика {i}",
            date=datetime.now(),
        )


async def run():
    await database.init_db()
    users = [111, 222, 333]
    await seed_chat_and_users(users)

    active = set(await database.get_active_pm_users(days_limit=30))
    check("активные собеседники ЛС найдены", set(users) <= active, f"got {sorted(active)}")

    print("\n[1] Новый пользователь заводится текущим временем, а не эпохой")
    reset()
    write_state({})
    await assistant.check_and_send_group_activity_pings(BOT)

    pings = read_pings()
    check("записи созданы для всех активных", set(map(str, users)) <= set(pings), f"got {sorted(pings)}")

    epoch_seeded = [uid for uid, rec in pings.items()
                    if str(rec.get("last_activity", "")).startswith("2000-01-01")]
    check("ни одной записи с эпохой", epoch_seeded == [], f"эпоха у {epoch_seeded}")

    now = datetime.now()
    fresh = []
    for uid, rec in pings.items():
        try:
            age = now - datetime.fromisoformat(rec.get("last_activity", ""))
        except Exception:
            age = timedelta(days=999)
        fresh.append(age < timedelta(minutes=5))
    check("last_activity выставлен «сейчас»", all(fresh), f"got {pings}")

    print("\n[2] Только что заведённому никто не пишет — 24 часа ещё не прошли")
    check("приглашений не отправлено", SENT == [], f"got {SENT}")

    print("\n[3] Молчащий трое суток получает приглашение")
    reset()
    silent = (datetime.now() - timedelta(days=3)).isoformat()
    write_state({str(u): {"last_activity": silent, "ping_sent": False} for u in users})
    await assistant.check_and_send_group_activity_pings(BOT)
    check("приглашение ушло", len(SENT) >= 1, f"got {SENT}")
    check("получатели — из числа активных", set(SENT) <= set(users), f"got {SENT}")
    check("разослано не всем сразу (выборка 20%)", len(SENT) < len(users), f"got {SENT}")

    print("\n[4] Отписавшихся не беспокоят")
    reset()
    write_state({str(u): {"last_activity": silent, "pings_opted_out": True} for u in users})
    await assistant.check_and_send_group_activity_pings(BOT)
    check("ни одного приглашения", SENT == [], f"got {SENT}")

    print("\n[5] Упёршихся в потолок неудач не добивают")
    reset()
    write_state({str(u): {"last_activity": silent, "ping_failures": assistant.MAX_PING_FAILURES}
                 for u in users})
    await assistant.check_and_send_group_activity_pings(BOT)
    check("ни одного приглашения", SENT == [], f"got {SENT}")

    print("\n[6] Недавний групповой пинг держит 48-часовой кулдаун")
    reset()
    recent_ping = (datetime.now() - timedelta(hours=5)).isoformat()
    write_state({str(u): {"last_activity": silent, "last_group_ping": recent_ping} for u in users})
    await assistant.check_and_send_group_activity_pings(BOT)
    check("ни одного приглашения", SENT == [], f"got {SENT}")

    print("\n[7] Ночью job не тратит даже LLM-вызов")
    reset()
    write_state({str(u): {"last_activity": silent} for u in users})
    original_quiet = assistant.is_ping_quiet_hours
    assistant.is_ping_quiet_hours = lambda now=None: True
    await assistant.check_and_send_group_activity_pings(BOT)
    assistant.is_ping_quiet_hours = original_quiet
    check("LLM не вызывался", LLM_CALLS == [], f"got {LLM_CALLS}")
    check("сообщений не отправлено", SENT == [], f"got {SENT}")

    print("\n[8] Успешное приглашение записывает кулдаун точечно")
    reset()
    write_state({
        "111": {"last_activity": silent},
        "222": {"last_activity": silent, "pings_opted_out": True},
        "333": {"last_activity": silent, "pings_opted_out": True},
    })
    await assistant.check_and_send_group_activity_pings(BOT)
    pings = read_pings()
    check("приглашение получил только не отписавшийся", SENT == [111], f"got {SENT}")
    check("ему проставлен last_group_ping", "last_group_ping" in pings.get("111", {}),
          f"got {pings.get('111')}")
    check("флаг отписки соседей не сброшен",
          pings.get("222", {}).get("pings_opted_out") is True
          and pings.get("333", {}).get("pings_opted_out") is True,
          f"got {pings}")

    print("\n[9] Нет горячего обсуждения — нет рассылки")
    reset()
    global _hot
    _hot = False
    write_state({str(u): {"last_activity": silent} for u in users})
    await assistant.check_and_send_group_activity_pings(BOT)
    _hot = True
    check("LLM спрошен", len(LLM_CALLS) == 1, f"got {LLM_CALLS}")
    check("но никому не написано", SENT == [], f"got {SENT}")


try:
    asyncio.run(run())
finally:
    try:
        database._DB_EXECUTOR.shutdown(wait=True)
    except Exception:
        pass
    shutil.rmtree(_TMPDIR, ignore_errors=True)

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
