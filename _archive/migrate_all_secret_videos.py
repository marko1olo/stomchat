import asyncio
import os
import sys
import json
import time
from telethon import TelegramClient
from telethon.errors import FloodWaitError

sys.path.insert(0, r"c:\Users\danat\Desktop\stomchat")
os.chdir(r"c:\Users\danat\Desktop\stomchat")
import config

sys.stdout.reconfigure(encoding='utf-8')

SRC_CHAT = -1003629549922  # Секретные материалы
DST_CHAT = -1003735006121  # Канал Сергея Елисеева
STATE_FILE = "migration_progress.json"
MAPPING_FILE = "topics_mapping.json"
LOG_FILE = "migration.log"
TEMP_DIR = os.path.join(os.getcwd(), "temp_media")

def log(msg: str):
    ts = time.strftime("[%Y-%m-%d %H:%M:%S]")
    line = f"{ts} {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"transferred": {}, "total": 0}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

async def main():
    os.makedirs(TEMP_DIR, exist_ok=True)
    
    # Очистка возможных старых временных файлов
    for f in os.listdir(TEMP_DIR):
        if f.startswith("stream_dl_") or f.endswith(".mp4"):
            try:
                os.remove(os.path.join(TEMP_DIR, f))
            except Exception:
                pass

    if not os.path.exists(MAPPING_FILE):
        log(f"❌ Файл {MAPPING_FILE} не найден! Запустите setup_target_topics.py")
        return
        
    with open(MAPPING_FILE, "r", encoding="utf-8") as f:
        mapping = json.load(f)

    state = load_state()
    
    log("=" * 65)
    log("🚀 СТРИМИНГОВАЯ МИГРАЦИЯ: Секретные материалы -> Канал Елисеева")
    log(f"Источник: {SRC_CHAT}")
    log(f"Цель: {DST_CHAT}")
    log("=" * 65)

    client = TelegramClient("dumper_session", config.API_ID, config.API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        log("❌ Сессия dumper_session не авторизована!")
        await client.disconnect()
        return

    src_entity = await client.get_entity(SRC_CHAT)
    dst_entity = await client.get_entity(DST_CHAT)
    log(f"Источник: {src_entity.title}")
    log(f"Цель: {dst_entity.title}")

    log("Сканирование всех видео-сообщений в источнике...")
    all_videos = []
    async for msg in client.iter_messages(src_entity, limit=2000):
        is_video = bool(msg.video or (msg.document and msg.document.mime_type and "video" in msg.document.mime_type))
        if is_video:
            f_size = 0
            if msg.video:
                f_size = msg.video.size
            elif msg.document:
                f_size = msg.document.size
                
            src_topic_id = None
            if msg.reply_to and getattr(msg.reply_to, "forum_topic", False):
                src_topic_id = msg.reply_to.reply_to_top_id or msg.reply_to.reply_to_msg_id

            all_videos.append({
                "id": msg.id,
                "src_topic_id": src_topic_id,
                "date": str(msg.date),
                "size_bytes": f_size,
                "size_mb": round(f_size / (1024 * 1024), 2),
                "caption": (msg.message or "").strip()
            })

    # Сортируем хронологически от старых к новым
    all_videos.sort(key=lambda x: x["id"])
    state["total"] = len(all_videos)
    save_state(state)

    transferred_count = len(state["transferred"])
    log(f"Всего найдено видео: {len(all_videos)}")
    log(f"Уже перенесено: {transferred_count} из {len(all_videos)}")

    t_start = time.time()
    for idx, v in enumerate(all_videos, 1):
        mid_str = str(v["id"])
        
        # Проверяем, перенесено ли уже
        if mid_str in state["transferred"]:
            continue

        # Определяем целевой топик
        src_t = str(v["src_topic_id"])
        target_info = mapping.get(src_t, {})
        target_topic_id = target_info.get("target_id", 1)  # 1 = General fallback
        target_title = target_info.get("title", "Общее")

        first_line = v["caption"].split("\n")[0] if v["caption"] else "Без названия"
        log(f"\n[{idx}/{len(all_videos)}] ID {v['id']} ({v['size_mb']} MB) -> '{target_title}': {first_line[:50]}...")

        temp_file = os.path.join(TEMP_DIR, f"stream_dl_{v['id']}.mp4")
        success = False
        
        for attempt in range(1, 4):
            try:
                # Получаем свежий объект сообщения с актуальным file_reference
                msg_obj = await client.get_messages(src_entity, ids=v["id"])
                if not msg_obj:
                    log(f"  ⚠️ Сообщение {v['id']} не найдено на сервере!")
                    break

                # 1. Скачиваем с отображением прогресса
                t0 = time.time()
                last_pct = [-1]
                
                def dl_progress(current, total):
                    pct = int((current / total) * 100) if total else 0
                    if pct != last_pct[0] and (pct % 25 == 0 or pct == 100):
                        last_pct[0] = pct
                        speed = (current / (1024*1024)) / max(0.1, time.time() - t0)
                        log(f"    📥 Скачивание: {pct}% ({current/(1024*1024):.1f}/{total/(1024*1024):.1f} MB, {speed:.2f} МБ/с)")

                dl_path = await client.download_media(
                    msg_obj,
                    file=temp_file,
                    progress_callback=dl_progress
                )
                
                if not dl_path or not os.path.exists(dl_path) or os.path.getsize(dl_path) == 0:
                    log(f"  ⚠️ Попытка {attempt}: Ошибка скачивания (файл пуст)")
                    continue
                dt_dl = time.time() - t0

                # 2. Заливаем в нужный топик со стримингом и оригинальным текстом/форматированием
                t1 = time.time()
                last_up_pct = [-1]
                
                def up_progress(current, total):
                    pct = int((current / total) * 100) if total else 0
                    if pct != last_up_pct[0] and (pct % 25 == 0 or pct == 100):
                        last_up_pct[0] = pct
                        speed = (current / (1024*1024)) / max(0.1, time.time() - t1)
                        log(f"    📤 Отправка в топик: {pct}% ({current/(1024*1024):.1f}/{total/(1024*1024):.1f} MB, {speed:.2f} МБ/с)")

                sent = await client.send_file(
                    dst_entity,
                    dl_path,
                    caption=msg_obj.message,
                    formatting_entities=msg_obj.entities,
                    reply_to=target_topic_id,
                    supports_streaming=True,
                    progress_callback=up_progress
                )
                dt_up = time.time() - t1
                
                log(f"  ✅ Готово за {dt_dl + dt_up:.1f}с (скачивание {dt_dl:.1f}с, загрузка {dt_up:.1f}с) -> New msg ID: {sent.id}")
                
                state["transferred"][mid_str] = {
                    "new_msg_id": sent.id,
                    "target_topic_id": target_topic_id,
                    "target_title": target_title,
                    "size_mb": v["size_mb"],
                    "date": v["date"],
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                }
                save_state(state)
                success = True
                break

            except FloodWaitError as fe:
                log(f"  [FloodWait] Ожидание {fe.seconds + 3} сек...")
                await asyncio.sleep(fe.seconds + 3)
            except Exception as e:
                log(f"  ⚠️ Попытка {attempt} завершилась с ошибкой: {type(e).__name__}: {e}")
                await asyncio.sleep(3)
            finally:
                # 3. Всегда удаляем временный файл сразу после загрузки
                if os.path.exists(temp_file):
                    try:
                        os.remove(temp_file)
                    except Exception:
                        pass

        if not success:
            log(f"  ❌ Не удалось перенести ID {v['id']} после 3 попыток.")

        # Безопасная пауза 3 секунды между файлами против флудвейта
        await asyncio.sleep(3.0)

    total_time = time.time() - t_start
    log("=" * 65)
    log("🎉 ВСЯ МИГРАЦИЯ УСПЕШНО ЗАВЕРШЕНА!")
    log(f"Всего перенесено видео: {len(state['transferred'])} из {len(all_videos)}")
    log(f"Затрачено времени: {total_time / 60:.1f} мин")
    log("=" * 65)

    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
