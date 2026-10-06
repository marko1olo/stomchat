import asyncio
import os
import sys
import json
import time
import inspect
from telethon import TelegramClient
from telethon.tl.types import DocumentAttributeVideo
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

# ── FastTelethon (параллельные MTProto соединения) ──────────────────────────
try:
    from FastTelethonhelper import FastTelethon as _FT
    _fast_dl = _FT.download_file
    _fast_ul = _FT.upload_file
    FAST_MODE = True
except Exception as _e:
    FAST_MODE = False
    _fast_dl = None
    _fast_ul = None

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

    if not os.path.exists(MAPPING_FILE):
        log(f"❌ Файл {MAPPING_FILE} не найден!")
        return

    with open(MAPPING_FILE, "r", encoding="utf-8") as f:
        mapping = json.load(f)

    state = load_state()

    log("=" * 65)
    log("⚡️ КОНВЕЙЕРНАЯ МИГРАЦИЯ v2 (FastTelethon: " + ("ON" if FAST_MODE else "OFF") + ")")
    log("Секретные материалы -> Канал Елисеева")
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
    log(f"FastTelethon режим: {'✅ ВКЛ' if FAST_MODE else '⚠️ ВЫКЛ (fallback)'}")

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

    all_videos.sort(key=lambda x: x["id"])
    state["total"] = len(all_videos)
    save_state(state)

    to_transfer = [v for v in all_videos if str(v["id"]) not in state["transferred"]]
    log(f"Всего видео: {len(all_videos)}")
    log(f"Уже перенесено: {len(state['transferred'])}")
    log(f"Осталось в очереди: {len(to_transfer)}")

    if not to_transfer:
        log("🎉 Все видео уже перенесены!")
        await client.disconnect()
        return

    queue = asyncio.Queue(maxsize=1)

    # ─────────────────────── DOWNLOADER WORKER ─────────────────────────────
    async def downloader_worker():
        for idx, v in enumerate(to_transfer, 1):
            first_line = v["caption"].split("\n")[0] if v["caption"] else "Без названия"
            log(f"\n[DL {idx}/{len(to_transfer)}] ID {v['id']} ({v['size_mb']} MB): {first_line[:50]}")

            temp_video = os.path.join(TEMP_DIR, f"stream_dl_{v['id']}.mp4")
            temp_thumb = os.path.join(TEMP_DIR, f"thumb_{v['id']}.jpg")
            downloaded = False

            for attempt in range(1, 4):
                try:
                    msg_obj = await client.get_messages(src_entity, ids=v["id"])
                    if not msg_obj:
                        log(f"  ⚠️ Сообщение {v['id']} не найдено, пропуск.")
                        break

                    # 1. Thumbnail
                    thumb_path = None
                    if os.path.exists(temp_thumb) and os.path.getsize(temp_thumb) > 0:
                        thumb_path = temp_thumb
                    else:
                        try:
                            thumb_path = await client.download_media(msg_obj, file=temp_thumb, thumb=-1)
                            if thumb_path and (not os.path.exists(thumb_path) or os.path.getsize(thumb_path) == 0):
                                thumb_path = None
                        except Exception as te:
                            log(f"  ⚠️ Превью ID {v['id']}: {te}")
                            thumb_path = None

                    # 2. DocumentAttributeVideo
                    orig_vid = None
                    if msg_obj.document and msg_obj.document.attributes:
                        for a in msg_obj.document.attributes:
                            if isinstance(a, DocumentAttributeVideo):
                                orig_vid = a
                                break

                    video_attrs = []
                    if orig_vid:
                        video_attrs.append(DocumentAttributeVideo(
                            duration=int(orig_vid.duration),
                            w=orig_vid.w,
                            h=orig_vid.h,
                            supports_streaming=True
                        ))

                    # 3. Видео — кэш или скачать
                    cached = (
                        os.path.exists(temp_video)
                        and abs(os.path.getsize(temp_video) - v["size_bytes"]) <= 1024
                    )
                    if cached:
                        log(f"  ⚡️ [ID {v['id']}] Кэш на диске ({os.path.getsize(temp_video)/(1024*1024):.1f} MB)")
                        dl_path = temp_video
                    else:
                        if os.path.exists(temp_video):
                            try:
                                os.remove(temp_video)
                            except Exception:
                                pass

                        t0 = time.time()
                        last_pct = [-1]

                        if FAST_MODE:
                            def dl_progress_fast(current, total):
                                pct = int((current / total) * 100) if total else 0
                                if pct != last_pct[0] and (pct % 10 == 0 or pct == 100):
                                    last_pct[0] = pct
                                    speed = (current / (1024*1024)) / max(0.1, time.time() - t0)
                                    log(f"  📥 [ID {v['id']}] {pct}% ({current/(1024*1024):.1f}/{v['size_mb']:.1f} MB, {speed:.2f} МБ/с)")

                            with open(temp_video, "wb") as fout:
                                await _fast_dl(client, msg_obj.document, fout, progress_callback=dl_progress_fast)
                            dl_path = temp_video
                        else:
                            def dl_progress(current, total):
                                pct = int((current / total) * 100) if total else 0
                                if pct != last_pct[0] and (pct % 25 == 0 or pct == 100):
                                    last_pct[0] = pct
                                    speed = (current / (1024*1024)) / max(0.1, time.time() - t0)
                                    log(f"  📥 [ID {v['id']}] {pct}% ({current/(1024*1024):.1f}/{v['size_mb']:.1f} MB, {speed:.2f} МБ/с)")

                            dl_path = await client.download_media(
                                msg_obj, file=temp_video, progress_callback=dl_progress
                            )

                    if dl_path and os.path.exists(dl_path) and os.path.getsize(dl_path) > 0:
                        downloaded = True
                        dur_str = f"{orig_vid.duration:.1f}s" if orig_vid else "?"
                        res_str = f"{orig_vid.w}x{orig_vid.h}" if orig_vid else "?"
                        log(f"  ✅ [ID {v['id']}] {res_str}, {dur_str}, превью: {'Да' if thumb_path else 'Нет'}")
                        await queue.put((v, dl_path, thumb_path, video_attrs, msg_obj))
                        break
                    else:
                        log(f"  ⚠️ Попытка {attempt}: файл пуст")

                except FloodWaitError as fe:
                    log(f"  [FloodWait DL] Ожидание {fe.seconds + 3} сек...")
                    await asyncio.sleep(fe.seconds + 3)
                except Exception as e:
                    log(f"  ⚠️ Ошибка DL ID {v['id']} (попытка {attempt}): {type(e).__name__}: {e}")
                    await asyncio.sleep(5)

            if not downloaded:
                log(f"  ❌ ПРОПУСК ID {v['id']} (не скачался после 3 попыток)")
                for p in [temp_video, temp_thumb]:
                    if os.path.exists(p):
                        try:
                            os.remove(p)
                        except Exception:
                            pass

        await queue.put(None)
        log("🏁 Downloader завершил работу.")

    # ─────────────────────── UPLOADER WORKER ─────────────────────────────
    async def uploader_worker():
        t_start = time.time()
        sent_count = 0

        while True:
            item = await queue.get()
            if item is None:
                queue.task_done()
                break

            v, dl_path, thumb_path, video_attrs, msg_obj = item
            mid_str = str(v["id"])
            src_t = str(v["src_topic_id"])
            target_info = mapping.get(src_t, {})
            target_topic_id = target_info.get("target_id", 1)
            target_title = target_info.get("title", "Общее")

            log(f"\n[UL] ID {v['id']} ({v['size_mb']} MB) -> '{target_title}'")

            success = False
            for attempt in range(1, 4):
                try:
                    t1 = time.time()
                    last_up_pct = [-1]

                    if FAST_MODE:
                        # Параллельная загрузка через FastTelethon
                        def up_progress_fast(current, total):
                            pct = int((current / total) * 100) if total else 0
                            if pct != last_up_pct[0] and (pct % 10 == 0 or pct == 100):
                                last_up_pct[0] = pct
                                speed = (current / (1024*1024)) / max(0.1, time.time() - t1)
                                log(f"  📤 [ID {v['id']}] {pct}% ({current/(1024*1024):.1f}/{v['size_mb']:.1f} MB, {speed:.2f} МБ/с)")

                        fname = os.path.basename(dl_path)
                        with open(dl_path, "rb") as fin:
                            input_file = await _fast_ul(client, fin, fname, progress_callback=up_progress_fast)

                        # Отправляем уже загруженный InputFile с нужными атрибутами
                        sent = await client.send_file(
                            dst_entity,
                            input_file,
                            thumb=thumb_path,
                            attributes=video_attrs if video_attrs else None,
                            caption=msg_obj.message,
                            formatting_entities=msg_obj.entities,
                            reply_to=target_topic_id,
                            supports_streaming=True,
                        )
                    else:
                        def up_progress(current, total):
                            pct = int((current / total) * 100) if total else 0
                            if pct != last_up_pct[0] and (pct % 25 == 0 or pct == 100):
                                last_up_pct[0] = pct
                                speed = (current / (1024*1024)) / max(0.1, time.time() - t1)
                                log(f"  📤 [ID {v['id']}] {pct}% ({current/(1024*1024):.1f}/{v['size_mb']:.1f} MB, {speed:.2f} МБ/с)")

                        sent = await client.send_file(
                            dst_entity,
                            dl_path,
                            thumb=thumb_path,
                            attributes=video_attrs if video_attrs else None,
                            caption=msg_obj.message,
                            formatting_entities=msg_obj.entities,
                            reply_to=target_topic_id,
                            supports_streaming=True,
                            progress_callback=up_progress
                        )

                    dt_up = time.time() - t1
                    avg_speed = v["size_mb"] / max(0.1, dt_up)
                    log(f"  ✅ [ID {v['id']}] -> Msg {sent.id} | {dt_up:.1f}с | ~{avg_speed:.2f} МБ/с")

                    state["transferred"][mid_str] = {
                        "new_msg_id": sent.id,
                        "target_topic_id": target_topic_id,
                        "target_title": target_title,
                        "size_mb": v["size_mb"],
                        "date": v["date"],
                        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                    }
                    save_state(state)
                    sent_count += 1
                    success = True
                    break

                except FloodWaitError as fe:
                    log(f"  [FloodWait UL] Ожидание {fe.seconds + 3} сек...")
                    await asyncio.sleep(fe.seconds + 3)
                except Exception as e:
                    log(f"  ⚠️ Ошибка UL ID {v['id']} (попытка {attempt}): {type(e).__name__}: {e}")
                    if attempt < 3:
                        await asyncio.sleep(5)

            # Удаляем файлы только если успешно отправили (или все попытки исчерпаны)
            for p in [dl_path, thumb_path]:
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass

            if not success:
                log(f"  ❌ ПРОПУСК ID {v['id']}: не удалось отправить после 3 попыток")

            queue.task_done()
            await asyncio.sleep(2.0)

        total_time = time.time() - t_start
        log(f"🏁 Uploader завершил работу. Отправлено: {sent_count} за {total_time/60:.1f} мин.")

    await asyncio.gather(downloader_worker(), uploader_worker())

    log("=" * 65)
    log("🎉 ВСЯ МИГРАЦИЯ ЗАВЕРШЕНА!")
    log(f"Всего в целевом канале: {len(state['transferred'])}")
    log("=" * 65)

    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
