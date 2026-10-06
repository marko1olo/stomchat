import asyncio
import os
import sys
import json
import time
import re
from telethon import TelegramClient

sys.path.insert(0, r"c:\Users\danat\Desktop\stomchat")
os.chdir(r"c:\Users\danat\Desktop\stomchat")
import config

sys.stdout.reconfigure(encoding='utf-8')

TARGET_CHAT_ID = -1003629549922
OUTPUT_DIR = os.path.join(os.getcwd(), "secret_materials_videos")
STATE_FILE = "secret_dump_state.json"
CATALOG_FILE = os.path.join(OUTPUT_DIR, "CATALOG.md")

def sanitize_filename(name):
    # Remove invalid characters for Windows paths
    clean = re.sub(r'[\\/*?:"<>|]', "", name)
    clean = re.sub(r'\s+', " ", clean).strip()
    return clean[:80]

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"downloaded": {}, "total": 0}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def update_catalog(all_videos, state):
    lines = [
        "# Каталог видеоматериалов из «Секретные материалы»\n",
        f"**Всего видео в архиве:** {len(all_videos)}\n",
        f"**Успешно скачано:** {len(state['downloaded'])}\n\n",
        "| № | ID | Дата | Размер | Название / Тема | Файл |\n",
        "|---|----|------|--------|-----------------|------|\n"
    ]
    
    for idx, v in enumerate(all_videos, 1):
        mid = str(v["id"])
        is_dl = mid in state["downloaded"]
        status_mark = "✅" if is_dl else "⏳"
        fn = state["downloaded"].get(mid, {}).get("filename", v.get("proposed_filename", "-"))
        size_mb = v["size_mb"]
        date_str = v["date"][:10]
        title = v["caption"] or "Без описания"
        title_short = (title[:65] + "...") if len(title) > 65 else title
        title_escaped = title_short.replace("|", "/")
        
        lines.append(f"| {idx} | {status_mark} `{v['id']}` | {date_str} | {size_mb:.1f} MB | {title_escaped} | `{fn}` |\n")

    lines.append("\n## Подробные описания видео\n\n")
    for idx, v in enumerate(all_videos, 1):
        mid = str(v["id"])
        fn = state["downloaded"].get(mid, {}).get("filename", v.get("proposed_filename", "-"))
        lines.append(f"### {idx}. [{v['date'][:10]}] ID {v['id']} ({v['size_mb']:.1f} MB)\n")
        lines.append(f"**Файл:** `{fn}`\n\n")
        if v.get("full_caption"):
            lines.append(f"{v['full_caption']}\n\n")
        else:
            lines.append("*Описание отсутствует*\n\n")
        lines.append("---\n\n")

    with open(CATALOG_FILE, "w", encoding="utf-8") as f:
        f.writelines(lines)

async def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    state = load_state()
    
    print("=" * 60)
    print("🚀 DUMPER: Секретные материалы (108 видео, ~6.33 ГБ)")
    print(f"📁 Папка сохранения: {OUTPUT_DIR}")
    print("=" * 60)

    client = TelegramClient("dumper_session", config.API_ID, config.API_HASH)
    await client.connect()
    
    if not await client.is_user_authorized():
        print("❌ dumper_session не авторизована!")
        await client.disconnect()
        return

    entity = await client.get_entity(TARGET_CHAT_ID)
    print(f"Подключено к чату: {entity.title} (ID: {entity.id})")
    print("Сканирование всех сообщений...")

    all_videos = []
    async for msg in client.iter_messages(entity, limit=2000):
        is_video = False
        f_size = 0
        original_name = None

        if msg.video:
            is_video = True
            f_size = msg.video.size
            for attr in msg.video.attributes:
                if hasattr(attr, "file_name") and attr.file_name:
                    original_name = attr.file_name
        elif msg.document and msg.document.mime_type and "video" in msg.document.mime_type:
            is_video = True
            f_size = msg.document.size
            for attr in msg.document.attributes:
                if hasattr(attr, "file_name") and attr.file_name:
                    original_name = attr.file_name

        if is_video:
            full_text = (msg.message or "").strip()
            first_line = full_text.split("\n")[0] if full_text else ""
            clean_title = sanitize_filename(first_line) or "video"
            proposed_fn = f"ID_{msg.id}_{clean_title}.mp4"
            
            all_videos.append({
                "id": msg.id,
                "msg_obj": msg,
                "date": str(msg.date),
                "size_bytes": f_size,
                "size_mb": round(f_size / (1024 * 1024), 2),
                "caption": first_line,
                "full_caption": full_text,
                "original_name": original_name,
                "proposed_filename": proposed_fn
            })

    # Сортируем хронологически (от старых к новым)
    all_videos.sort(key=lambda x: x["id"])
    state["total"] = len(all_videos)
    total_gb = sum(v["size_mb"] for v in all_videos) / 1024
    print(f"Всего найдено видео: {len(all_videos)} ({total_gb:.2f} ГБ)")
    print(f"Уже скачано ранее: {len(state['downloaded'])} из {len(all_videos)}")

    update_catalog(all_videos, state)

    total_downloaded_bytes = 0
    t_start = time.time()

    for idx, v in enumerate(all_videos, 1):
        mid = str(v["id"])
        proposed_path = os.path.join(OUTPUT_DIR, v["proposed_filename"])

        # Проверяем, скачано ли уже
        if mid in state["downloaded"] and os.path.exists(proposed_path):
            if os.path.getsize(proposed_path) >= v["size_bytes"] * 0.95:
                continue

        print(f"\n[{idx}/{len(all_videos)}] Загрузка ID {v['id']} ({v['size_mb']} MB): {v['caption'][:50]}...")
        
        # Скачиваем с 3 попытками
        success = False
        for attempt in range(1, 4):
            try:
                t0 = time.time()
                tmp_path = proposed_path + ".part"
                
                downloaded_path = await client.download_media(v["msg_obj"], file=tmp_path)
                
                if downloaded_path and os.path.exists(tmp_path):
                    actual_size = os.path.getsize(tmp_path)
                    if actual_size > 0:
                        if os.path.exists(proposed_path):
                            os.remove(proposed_path)
                        os.rename(tmp_path, proposed_path)
                        dt = time.time() - t0
                        speed = (v["size_mb"] / dt) if dt > 0 else 0
                        print(f"  ✅ Готово за {dt:.1f}с ({speed:.2f} МБ/с) -> {v['proposed_filename']}")
                        
                        state["downloaded"][mid] = {
                            "filename": v["proposed_filename"],
                            "size_mb": v["size_mb"],
                            "date": v["date"],
                            "downloaded_at": time.strftime("%Y-%m-%d %H:%M:%S")
                        }
                        save_state(state)
                        total_downloaded_bytes += actual_size
                        success = True
                        break
                    else:
                        print(f"  ⚠️ Попытка {attempt}: Файл пустой, повтор...")
            except Exception as exc:
                print(f"  ⚠️ Попытка {attempt} завершилась с ошибкой: {exc}")
                await asyncio.sleep(2)

        if not success:
            print(f"  ❌ Не удалось скачать видео ID {v['id']} после 3 попыток.")
        
        # Обновляем каталог раз в 5 видео
        if idx % 5 == 0 or idx == len(all_videos):
            update_catalog(all_videos, state)
            
        # Небольшая пауза для стабильности сокета
        await asyncio.sleep(0.5)

    update_catalog(all_videos, state)
    total_time = time.time() - t_start
    print("\n" + "=" * 60)
    print(f"🎉 ВЫГРУЗКА ЗАВЕРШЕНА!")
    print(f"Всего файлов скачано: {len(state['downloaded'])} из {len(all_videos)}")
    print(f"Каталог сформирован: {CATALOG_FILE}")
    print("=" * 60)

    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
