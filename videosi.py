import asyncio
import aiosqlite
import re
import os
import sqlite3
import config
# Разбор ответа модели — ОДИН на оба скрипта дистилляции: своя копия здесь уже
# была и ломалась ровно так же, как в reclass, а расхождение копий означает, что
# один и тот же ответ модели в одном скрипте разбирается, а в другом теряется.
# Импорт безопасен: reclass на уровне модуля только объявляет константы и функции.
from reclass import ModelJsonError, extract_codes
from google import genai
from google.genai import types
from datetime import datetime

# Имя файла с твоим текстом (проверь, что он так называется!)
INPUT_FILE = "videos.txt"
DB_PATH = "stomat_wiki.db"
ARCHIVE_PATH = "stomat_archive.db"
MODEL_ID = "models/gemma-3-27b-it"
# Начало каждого видео-факта. Байт-в-байт как у 53 записей, уже лежащих в базе
# (U+1F3A5 = 🎥): по нему и опознаётся «этот протокол уже импортирован». Стоит
# изменить хоть пробел — защита от повтора перестанет видеть старые записи и
# повторный прогон удвоит все 53 протокола.
VIDEO_MARKER = "\U0001f3a5 [ВИДЕО-ПРОТОКОЛ | MSG "
# Типы медиа архива, при которых ссылка на msg_id считается проверенной.
# Замер архива: media_type бывает только NULL (101 845), photo (14 754),
# video (804) и file (444). Тип file сюда НЕ включён намеренно: под ним лежат
# документы, и назвать документ видео — то же самое угадывание, из-за которого
# провенанс и оказался ложным.
VIDEO_MEDIA_TYPES = ("video",)
# Код, который классификатор отдаёт, когда не смог определить категорию: все
# ключи в лимите или модель ответила словами вместо цифр. Ни одна подтема
# рубрикатора этот код не несёт (замер: 53 кода в WIKI_TREE, 10.1 среди них нет,
# и ни один не является его подстрокой), поэтому факт с таким кодом врачу
# недостижим — он лежит в базе и не показывается ни в одном разделе.
FALLBACK_CODE = "10.1"
# Сколько раз ждать освобождения базы. Без предела цикл был вечным: ошибка
# «нет такой таблицы» — тоже OperationalError, и импорт молча висел навсегда.
DB_BUSY_ATTEMPTS = 30
# Пауза между протоколами, чтобы не душить API. Вынесена в константу, чтобы
# защиту от повторного импорта можно было проверить тестом, не высиживая
# 53 x 2 секунды: непроверяемая защита от дублей — это просто надежда.
SLEEP_BETWEEN_VIDEOS = 2

KNOWLEDGE_TREE = """
1. ТЕРАПИЯ
   1.1. Эндодонтия: 1.1.1 Доступ/МБ2, 1.1.2 Инструментация/Файлы, 1.1.3 Ирригация/Активация, 1.1.4 Обтурация, 1.1.5 Перелечивание, 1.1.6 Апекслокаторы.
   1.2. Реставрация: 1.2.1 Адгезивные протоколы, 1.2.2 Спиртовой протокол, 1.2.3 Морфология, 1.2.4 Матрицы/Клинья, 1.2.5 Билдап/Штифты, 1.2.6 Полировка.
   1.3. Пародонтология: 1.3.1 Профгигиена/GBT, 1.3.2 SRP/Кюретаж, 1.3.3 Отбеливание.
2. ОРТОПЕДИЯ
   2.1. Конструкции: 2.1.1 Виниры, 2.1.2 Коронки, 2.1.3 Мосты, 2.1.4 Накладки.
   2.2. Техника: 2.2.1 BOPT/Vertiprep, 2.2.2 Уступ, 2.2.3 Оттиски, 2.2.4 Ретракция, 2.2.5 Временные, 2.2.6 Фиксация/Цементы.
   2.3. Гнатология: 2.3.1 Окклюзия, 2.3.2 ВНЧС, 2.3.3 Артикуляторы.
   2.4. Съемное: 2.4.1 Полные/Акрил, 2.4.2 Бюгельные, 2.4.3 Перебазировка.
3. ХИРУРГИЯ
   3.1. Амбулаторная: 3.1.1 Удаление, 3.1.2 Резекция, 3.1.3 Зубосохраняющие.
   3.2. Имплантация: 3.2.1 Планирование, 3.2.2 Системы, 3.2.3 Мультиюниты, 3.2.4 Осложнения.
   3.3. Реконструкция: 3.3.1 Пластика десны, 3.3.2 Костная пластика.
4. ОРТОДОНТИЯ: 4.1.1 Брекеты, 4.1.2 Элайнеры, 4.1.3 Диагностика.
5. ЦИФРА: 5.1.1 Сканеры, 5.2.1 Exocad, 5.3.1 3D-печать.
6. ОБЩЕЕ: 6.1.1 Оборудование, 6.2.1 Фармакология, 6.3.1 Фотопротокол.
7. МЕНЕДЖМЕНТ: 7.1.1 Экономика, 7.2.1 Юридическое, 7.3.1 Психология.
"""

def clean_codes(raw_codes):
    """Оставить в category_code только цифровые коды.

    Модель периодически отвечает названием категории словами. Такое название
    уезжало в category_code как есть, и факт выпадал из выдачи целиком: и
    экспорт (savdel.py), и поиск ходят по коду, а не по названию. Врач видео-
    разбор просто не найдёт. Регулярка та же, что в reclass.py.
    """
    out = []
    for code in raw_codes:
        match = re.search(r'(\d+\.\d+(?:\.\d+)?)', str(code))
        if match:
            out.append(match.group(1))
    # dict.fromkeys, а не set: порядок кодов не должен меняться от прогона к прогону.
    return ", ".join(dict.fromkeys(out)) if out else FALLBACK_CODE

def verify_provenance(msg_id, archive_path=None):
    """Вернуть номер источника, только если ссылка проверяема; иначе пустую строку.

    Номера в videos.txt — это нумерация текстового файла, а НЕ msg_id архива.
    Замер на боевых базах: 35 из 53 номеров случайно совпали с существующими
    msg_id, и это чужие реплики — медиа есть только у 6, тип video ровно у 1.
    То есть врач шёл по «источнику» и читал разговор про контактные пункты
    вместо видео-разбора, после чего справедливо считал, что бот врёт.
    Пустой провенанс честнее ложного: пустой ничего не обещает.
    """
    path = archive_path or ARCHIVE_PATH
    text = str(msg_id).strip()
    if not text.isdigit() or not os.path.exists(path):
        return ""
    try:
        uri = "file:" + os.path.abspath(path).replace(os.sep, "/") + "?mode=ro"
        archive = sqlite3.connect(uri, uri=True)
        try:
            row = archive.execute(
                "SELECT has_media, media_type FROM archive_messages WHERE msg_id = ?",
                (int(text),)).fetchone()
        finally:
            archive.close()
    except sqlite3.Error:
        # Архива нет или он битый — подтвердить ссылку нечем, значит её нет.
        return ""
    if not row or not row[0]:
        return ""
    if (row[1] or "").strip().lower() not in VIDEO_MEDIA_TYPES:
        return ""
    return text

async def classify_video(body):
    """Классификация с ротацией ключей."""
    prompt = f"""
    ТЫ — МЕДИЦИНСКИЙ КЛАССИФИКАТОР.
    Определи, к какой категории относится этот клинический разбор. Проанализировать текст и присвоить ему подходящие коды категорий.
    ДЕРЕВО: {KNOWLEDGE_TREE}
    ТЕКСТ: {body[:4000]}...
    ПРАВИЛА:
    1. ИСПОЛЬЗУЙ МУЛЬТИ-ТЕГИ: Если факт затрагивает несколько тем, укажи ВСЕ подходящие коды. 
    2. МАКСИМАЛЬНАЯ ТОЧНОСТЬ: Выбирай L3 коды (три цифры), если это возможно.
    3. ОГРАНИЧЕНИЕ: Не более 5 кодов на один факт.
    4. ФОРМАТ ОТВЕТА: СТРОГО JSON: {{"codes": ["X.X.X", "Y.Y.Y"]}}
    5. ТОЛЬКО ЦИФРЫ: В массив "codes" пиши ТОЛЬКО цифровой код. Названия текстом писать КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО.
    """
    
    # Пробуем все ключи по очереди, если один не сработал
    for api_key in config.GOOGLE_KEYS:
        try:
            client = genai.Client(api_key=api_key)
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=MODEL_ID,
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.0, safety_settings=[
            types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
            types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
            types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
            types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE")
        ])
            )
            if response and response.text:
                return clean_codes(extract_codes(response.text))
        except ModelJsonError as exc:
            # Модель ОТВЕТИЛА, но объекта с кодами в ответе нет (обрыв, отказ
            # словами). Другой ключ этого не лечит: прежний код перебирал молча
            # все 10 ключей — замер 10 вызовов на один протокол и ни одной строки
            # в журнале, а протокол всё равно не записывался. Теперь причина
            # названа сразу и вызов один: протокол остаётся в videos.txt, и
            # повторный прогон разметит его нормально.
            print(f"\n   [ОТКАЗ РАЗБОРА] {exc}")
            break
        except Exception:
            continue # Пробуем следующий ключ молча

    return FALLBACK_CODE # Если все ключи сдохли

async def already_imported(db, msg_id):
    """Есть ли уже факт по этому протоколу. Сравнение по началу строки, не LIKE.

    substr(...) = marker, а не LIKE '%MSG 148%': подстрока нашла бы «MSG 1489»
    внутри «MSG 148», и часть протоколов молча не импортировалась бы.
    Закрывающая скобка в маркере даёт границу номера.
    """
    marker = f"{VIDEO_MARKER}{msg_id}]"
    cursor = await db.execute(
        "SELECT id FROM distilled_facts WHERE substr(content, 1, ?) = ?",
        (len(marker), marker))
    return await cursor.fetchone()

async def save_to_db_safe(params, msg_id):
    """Записать факт, если такого протокола ещё нет. True — вставили, False — уже был.

    Повторный запуск на том же videos.txt дублировал все 53 протокола: UNIQUE в
    distilled_facts нет ни одного (PRAGMA index_list -> только idx_cat по
    category_code), поэтому INSERT OR IGNORE сам по себе не отсекает ничего и
    стоит здесь только на будущее — когда лид добавит UNIQUE, отсечёт база.
    Врач в выдаче видел один и тот же разбор дважды и перестаёт верить счётчику.
    """
    for attempt in range(1, DB_BUSY_ATTEMPTS + 1):
        try:
            async with aiosqlite.connect(DB_PATH, timeout=30) as db:
                if await already_imported(db, msg_id):
                    return False
                await db.execute('''
                    INSERT OR IGNORE INTO distilled_facts
                    (category_code, content, source_ids, is_case, confidence, processed_at, is_reclassified)
                    VALUES (?, ?, ?, ?, ?, ?, 1)
                ''', params)
                await db.commit()
                return True # Успех
        except sqlite3.OperationalError as exc:
            reason = str(exc).lower()
            if "locked" not in reason and "busy" not in reason:
                # «no such table» — тоже OperationalError. Прежний код ждал
                # освобождения базы вечно и не импортировал ни одного протокола.
                raise
            print(f"   [!] База занята, жду ({attempt} из {DB_BUSY_ATTEMPTS})...")
            await asyncio.sleep(1)
    raise RuntimeError(
        f"База {DB_PATH} занята {DB_BUSY_ATTEMPTS} попыток подряд, протокол MSG {msg_id} не записан")

async def main():
    # Печать без эмодзи: консоль боевой машины cp1251, а U+274C/U+2705/U+23F3 в
    # неё не кодируются — на этой самой строке импорт падал UnicodeEncodeError,
    # даже не дойдя до базы. Кириллица в cp1251 проходит.
    # SystemExit, а не return: с return процесс заканчивался с кодом 0, и для
    # обёртки, cron и любого «запустил и посмотрел в конец лога» это УСПЕХ.
    # Замер до правки: нет videos.txt -> returncode 0. То есть импорт, не
    # вставивший ни одного протокола, отчитывался как выполненный, врач ждал
    # новые видео-разборы в рубрикаторе, а их там не появлялось никогда — и
    # никто не искал причину, потому что формально всё прошло.
    if not os.path.exists(INPUT_FILE):
        print(f"[СТОП] Файл {INPUT_FILE} не найден! Создай его и вставь текст.")
        raise SystemExit(1)

    if not os.path.exists(DB_PATH):
        print(f"[СТОП] База не найдена: {os.path.abspath(DB_PATH)}")
        raise SystemExit(1)

    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    # Разбиваем по номерам сообщений (ID на отдельной строке)
    parts = re.split(r'\n(\d{3,})\n', "\n" + content)

    print("Старт импорта ВИДЕО-САММАРИ...")
    if not os.path.exists(ARCHIVE_PATH):
        print(f"[!] Архива {ARCHIVE_PATH} рядом нет: провенанс останется пустым у всех "
              f"протоколов. Пустая ссылка честнее ссылки на чужую реплику.")

    count = 0
    skipped = 0
    without_source = 0
    unclassified = 0
    # parts[0] пустой, дальше [ID, Текст, ID, Текст...]
    for i in range(1, len(parts), 2):
        msg_id = parts[i].strip()
        # Файл может кончиться номером без текста — тогда parts[i+1] нет.
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""

        if not body: continue

        # 1. Формируем текст и проверяем, не импортирован ли протокол раньше.
        #    Проверка ДО обращения к модели: иначе повторный прогон платит за
        #    классификацию 53 протоколов, которые всё равно будут отброшены.
        final_content = f"{VIDEO_MARKER}{msg_id}]\n\n{body}"
        async with aiosqlite.connect(DB_PATH, timeout=30) as probe:
            if await already_imported(probe, msg_id):
                print(f"Пропуск Видео MSG_{msg_id}: протокол уже в базе.")
                skipped += 1
                continue

        print(f"Обработка Видео MSG_{msg_id}...", end=" ")

        # 2. Классифицируем
        cat_code = await classify_video(body)

        # 2a. Классификатор не справился — протокол НЕ записываем.
        #     Иначе получается худший из вариантов: факт лежит в базе с
        #     confidence 100, но ни одна подтема рубрикатора код 10.1 не несёт,
        #     значит врач его не увидит ни в одном разделе. И вылечить это уже
        #     нельзя: защита от повтора опознает протокол по номеру и следующий
        #     прогон, когда ключи оживут, честно его пропустит — невидимка
        #     останется в базе навсегда. Не записав, мы оставляем протокол в
        #     videos.txt: повторный прогон возьмёт его и разметит нормально.
        if cat_code == FALLBACK_CODE:
            print(f"НЕ ЗАПИСАН: классификатор не дал код (заглушка {FALLBACK_CODE}). "
                  f"Протокол остаётся в {INPUT_FILE}, повтори прогон.")
            unclassified += 1
            await asyncio.sleep(SLEEP_BETWEEN_VIDEOS)
            continue

        # 3. Провенанс: либо проверенный msg_id архива, либо пусто.
        source_ids = verify_provenance(msg_id)
        if not source_ids:
            without_source += 1

        # 4. Сохраняем (безопасно)
        inserted = await save_to_db_safe((
            cat_code,
            final_content,
            source_ids,
            1,   # is_case
            100, # confidence
            datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ), msg_id)

        if inserted:
            print(f"ОК -> Категория {cat_code}"
                  + (f", источник {source_ids}" if source_ids else ", источник не подтверждён"))
            count += 1
        else:
            print("пропуск: протокол уже в базе.")
            skipped += 1

        # Пауза, чтобы не душить API
        await asyncio.sleep(SLEEP_BETWEEN_VIDEOS)

    print(f"\nИмпорт завершен! Добавлено {count} протоколов, пропущено как повтор {skipped}.")
    if unclassified:
        # Громко и последней строкой: молчаливый ноль оператор принимает за успех.
        print(f"[ВНИМАНИЕ] Без категории и потому НЕ записано: {unclassified}. Эти протоколы врач "
              f"в рубрикаторе не найдёт, поэтому они не в базе; повтори прогон, когда ключи оживут.")
    if without_source:
        print(f"Без подтверждённого источника: {without_source}. Номера из {INPUT_FILE} — "
              f"нумерация файла, а не msg_id архива; ложную ссылку врач читает как обман.")

if __name__ == '__main__':
    asyncio.run(main())