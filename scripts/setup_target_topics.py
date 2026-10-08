import asyncio
import os
import sys
import json
import random
from telethon import TelegramClient
from telethon.tl.functions.messages import (
    CreateForumTopicRequest,
    EditForumTopicRequest,
    DeleteTopicHistoryRequest,
    GetForumTopicsRequest
)

sys.path.insert(0, r"c:\Users\danat\Desktop\stomchat")
os.chdir(r"c:\Users\danat\Desktop\stomchat")
import config

sys.stdout.reconfigure(encoding='utf-8')

TARGET_CHAT_ID = -1003735006121
MAPPING_FILE = "topics_mapping.json"

# Full list of original topics from Секретные материалы
SOURCE_TOPICS = [
    {"src_id": 14, "title": "👑 ВРЕМЕННЫЕ КОРОНКИ", "raw_title": "ВРЕМЕННЫЕ КОРОНКИ", "color": 7322096},
    {"src_id": 89, "title": "🛡 Изоляция", "raw_title": "Изоляция", "color": 9367192},
    {"src_id": 92, "title": "📐 ДИЗАЙН КОРОНОК", "raw_title": "ДИЗАЙН КОРОНОК", "color": 16749490},
    {"src_id": 100, "title": "⚡️ ПРЕПАРИРОВАНИЕ", "raw_title": "ПРЕПАРИРОВАНИЕ", "color": 16766590},
    {"src_id": 115, "title": "🦷 ТЕРАПИЯ", "raw_title": "ТЕРАПИЯ", "color": 5707770},
    {"src_id": 125, "title": "🎙 ПРЯМЫЕ ЭФИРЫ С ГОСТЯМИ", "raw_title": "ПРЯМЫЕ ЭФИРЫ С ГОСТЯМИ", "color": 16749490},
    {"src_id": 303, "title": "💎 PRO V CLASS", "raw_title": "PRO V CLASS", "color": 7322096},
    {"src_id": 322, "title": "🔬 ЭМАЛЕВЫЕ ПРИЗМЫ", "raw_title": "ЭМАЛЕВЫЕ ПРИЗМЫ", "color": 9367192},
    {"src_id": 324, "title": "🧪 ФИКСАЦИЯ КОРОНОК", "raw_title": "ФИКСАЦИЯ КОРОНОК", "color": 5707770},
    {"src_id": 340, "title": "🧭 НАВИГАЦИЯ КАНАЛА", "raw_title": "НАВИГАЦИЯ КАНАЛА", "color": 16766590},
    {"src_id": 344, "title": "📺 ПРЯМЫЕ ЭФИРЫ", "raw_title": "ПРЯМЫЕ ЭФИРЫ", "color": 16749490},
    {"src_id": 352, "title": "⚙️ ЛАБОРАТОРНЫЙ ЭТАП", "raw_title": "ЛАБОРАТОРНЫЙ ЭТАП", "color": 7322096},
    {"src_id": 399, "title": "🚨 ОШИБКИ", "raw_title": "ОШИБКИ", "color": 16749490},
    {"src_id": 404, "title": "🧵 РЕТРАКЦИЯ", "raw_title": "РЕТРАКЦИЯ", "color": 9367192},
    {"src_id": 423, "title": "📖 ОРТОПЕДИЯ ОБЩЕЕ", "raw_title": "ОРТОПЕДИЯ ОБЩЕЕ", "color": 5707770},
    {"src_id": 430, "title": "🌐 VK", "raw_title": "VK", "color": 7322096},
    {"src_id": 457, "title": "🔩 ПРОТЕЗИРОВАНИЕ НА ИМПЛАНТАТАХ", "raw_title": "ПРОТЕЗИРОВАНИЕ НА ИМПЛАНТАТАХ", "color": 16766590}
]

async def main():
    print("=== 1. Подключение к целевому каналу ===")
    client = TelegramClient("dumper_session", config.API_ID, config.API_HASH)
    await client.connect()
    entity = await client.get_entity(TARGET_CHAT_ID)
    print(f"Канал: {entity.title} (ID {entity.id})")

    # 1. Удаляем мусорный топик 18
    print("\n=== 2. Очистка мусорных топиков ===")
    try:
        await client(DeleteTopicHistoryRequest(peer=entity, top_msg_id=18))
        print("✅ Топик 18 ('ааааы') успешно удалён.")
    except Exception as e:
        print(f"Топик 18: {e}")

    # 2. Переименовываем топик 26
    print("\n=== 3. Оформление топика 26 (Дайджесты) ===")
    try:
        await client(EditForumTopicRequest(
            peer=entity,
            topic_id=26,
            title="📚 Дайджесты и сводки"
        ))
        print("✅ Топик 26 переименован в '📚 Дайджесты и сводки'.")
    except Exception as e:
        print(f"Ошибка переименования топика 26: {e}")

    # 3. Сканируем существующие топики
    print("\n=== 4. Проверка существующих разделов ===")
    existing_topics = {}
    try:
        res = await client(GetForumTopicsRequest(
            channel=entity,
            offset_date=0,
            offset_id=0,
            offset_topic=0,
            limit=100
        ))
        for t in res.topics:
            existing_topics[t.title.strip().lower()] = t.id
            print(f"Существующий топик: ID {t.id} -> '{t.title}'")
    except Exception as e:
        print(f"Ошибка получения существующих топиков: {e}")

    # 4. Создаём недостающие топики
    print("\n=== 5. Создание разделов архива ===")
    mapping = {}
    
    for t_spec in SOURCE_TOPICS:
        src_id = t_spec["src_id"]
        title = t_spec["title"]
        raw = t_spec["raw_title"].strip().lower()
        
        target_id = None
        # Проверяем, есть ли уже такой топик
        for ex_title, ex_id in existing_topics.items():
            if raw in ex_title or ex_title in raw or title.strip().lower() == ex_title:
                target_id = ex_id
                print(f"Раздел '{title}' уже существует (ID {target_id}).")
                break
                
        if target_id is None:
            try:
                res_create = await client(CreateForumTopicRequest(
                    peer=entity,
                    title=title,
                    icon_color=t_spec.get("color", 7322096),
                    random_id=random.randint(1, 1000000000)
                ))
                # В ответе Updates ищем созданное сообщение топика
                for update in getattr(res_create, 'updates', []):
                    if hasattr(update, 'message') and hasattr(update.message, 'id'):
                        target_id = update.message.id
                        break
                    elif hasattr(update, 'id'):
                        target_id = update.id
                
                if target_id is None:
                    # fallback to id
                    target_id = getattr(res_create, 'id', None)
                    
                print(f"✅ Создан новый раздел: '{title}' -> ID {target_id}")
                await asyncio.sleep(1.0)
            except Exception as e:
                print(f"❌ Ошибка создания '{title}': {e}")

        if target_id:
            mapping[str(src_id)] = {
                "src_id": src_id,
                "target_id": target_id,
                "title": title
            }

    with open(MAPPING_FILE, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)

    print(f"\nСопоставление топиков сохранено в {MAPPING_FILE} ({len(mapping)} разделов).")
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
