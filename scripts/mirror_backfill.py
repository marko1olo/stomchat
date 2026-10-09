import sqlite3
import urllib.request
import json
import ssl
import sys
import time
import os

sys.stdout.reconfigure(encoding='utf-8')

# Ensure we can load config if needed
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

TOKEN = config.BOT_TOKEN
SOURCE_CHAT_ID = config.SOURCE_CHAT_ID or -1001820467444
TARGET_CHAT_ID = int(os.getenv("MIRROR_CHAT_ID", "-1004326633527"))
STATE_FILE = "mirror_backfill_state.json"
DB_PATH = config.DB_PATH or "stomat_bot.db"

ctx = ssl.create_default_context()

def tg_api(method, params=None):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    if params:
        data = json.dumps(params).encode('utf-8')
        req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    else:
        req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8')
        try:
            return {"ok": False, "code": e.code, "body": json.loads(body)}
        except Exception:
            return {"ok": False, "code": e.code, "body": body}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_msg_id": 0, "processed_count": 0}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def main():
    print("=== Запуск заливки истории в зеркало ===")
    print(f"Источник: {SOURCE_CHAT_ID}")
    print(f"Цель: {TARGET_CHAT_ID}")
    print("Период: с 2026-09-01 (текущий месяц)")

    state = load_state()
    last_id = state.get("last_msg_id", 0)
    print(f"Текущее состояние: last_msg_id = {last_id}, уже переслано = {state.get('processed_count', 0)}")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT msg_id, date 
        FROM messages 
        WHERE date >= '2026-09-01' AND msg_id > ?
        ORDER BY msg_id ASC
    """, (last_id,))
    rows = cursor.fetchall()
    conn.close()

    total_to_send = len(rows)
    print(f"Найдено сообщений к отправке: {total_to_send}")
    if total_to_send == 0:
        print("Все сообщения за этот месяц уже перенесены!")
        return

    # Split into chunks of 50
    chunk_size = 50
    chunks = [rows[i:i + chunk_size] for i in range(0, total_to_send, chunk_size)]
    print(f"Всего пачек: {len(chunks)} (по {chunk_size} сообщений)")

    processed = state.get("processed_count", 0)
    for idx, chunk in enumerate(chunks, 1):
        msg_ids = [r[0] for r in chunk]
        first_id, last_id = msg_ids[0], msg_ids[-1]
        first_date, last_date = chunk[0][1], chunk[-1][1]

        while True:
            res = tg_api("forwardMessages", {
                "chat_id": TARGET_CHAT_ID,
                "from_chat_id": SOURCE_CHAT_ID,
                "message_ids": msg_ids
            })

            if res.get("ok"):
                new_msgs = res.get("result", [])
                processed += len(new_msgs)
                state["last_msg_id"] = last_id
                state["processed_count"] = processed
                save_state(state)
                print(f"[{idx}/{len(chunks)}] Успешно переслана пачка ({len(new_msgs)} шт.) "
                      f"IDs {first_id}..{last_id} ({first_date} .. {last_date}). Всего переслано: {processed}")
                time.sleep(2.5)  # Защита от троттлинга Telegram
                break
            else:
                code = res.get("code")
                body = res.get("body", {})
                error_desc = body.get("description", str(body)) if isinstance(body, dict) else str(body)

                # Если все сообщения в пачке были удалены в источнике
                if code == 400 and "there are no messages to forward" in error_desc:
                    print(f"[{idx}/{len(chunks)}] Сообщения пачки {first_id}..{last_id} удалены в источнике. Пропускаем.")
                    state["last_msg_id"] = last_id
                    save_state(state)
                    time.sleep(1.0)
                    break

                # Если Flood Control / 429
                if code == 429:
                    retry_after = 5
                    if isinstance(body, dict):
                        retry_after = body.get("parameters", {}).get("retry_after", 5)
                    print(f"[FloodWait] Превышен лимит. Ожидание {retry_after + 2} сек...")
                    time.sleep(retry_after + 2)
                    continue

                print(f"[{idx}/{len(chunks)}] Ошибка при отправке {first_id}..{last_id}: {res}")
                time.sleep(5.0)
                break

    print(f"\nЗаливка истории успешно завершена! Всего перенесено сообщений: {processed}")

if __name__ == "__main__":
    main()
