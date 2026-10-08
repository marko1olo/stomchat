import asyncio
import aiosqlite
import json
import os
import re
import sqlite3
import config
# Сканер сбалансированных объектов берём из distiller, а не пишем третью копию:
# разбор ответа модели уже трижды жил в дереве по-разному, и расхождение копий —
# это расхождение того, найдёт врач факт или нет.
from distiller import _iter_json_objects
from datetime import datetime
from google import genai
from google.genai import types
import time

DB_PATH = "stomat_wiki.db"
MODEL_ID = "models/gemma-3-27b-it"
BACKUP_PREFIX = "wiki_backup_"
# Пауза между фактами: 15k TPM на ключ. Вынесена в константу, чтобы прогон можно
# было проверить, не ожидая полутора часов реального сна.
SLEEP_BETWEEN_FACTS = 1.8
SLEEP_ON_ROTATE = 2
# Сколько раз подряд разрешено споткнуться на одном факте. Без предела
# `idx` не двигался при ошибке классификатора, и скрипт бесконечно долбил один и
# тот же факт: до врача не доходило НИ ОДНОГО обновления, а квота выгорала.
MAX_FAILS_PER_FACT = 3
MAX_ROTATIONS_PER_FACT = 12

# === УЛЬТИМАТИВНОЕ ДЕРЕВО ЗНАНИЙ 5.0 (ПОЛНОЕ) ===
KNOWLEDGE_TREE = """
1. ТЕРАПИЯ
   1.1. Эндодонтия: 
      1.1.1 Доступ и поиск каналов (МБ2), 
      1.1.2 Инструментация и системы файлов, 
      1.1.3 Ирригация и активация растворов, 
      1.1.4 Обтурация (Гуттаперча/Биокерамика), 
      1.1.5 Перелечивание (Ретрит/Ступеньки/Обломки), 
      1.1.6 Апекслокаторы и диагностика.
   1.2. Реставрация: 
      1.2.1 Адгезивные протоколы и IDS, 
      1.2.2 Спиртовой протокол и влажность дентина, 
      1.2.3 Морфология (Бугры/Фиссуры/Эмалевое кольцо), 
      1.2.4 Матричные системы (Клинья/Кольца/Матрицы), 
      1.2.5 Билдап и Штифты (СВШ/Анкеры), 
      1.2.6 Полировка и финишная обработка.
   1.3. Пародонтология и Профилактика: 
      1.3.1 Профгигиена (GBT/AirFlow/Ультразвук), 
      1.3.2 Консервативное лечение десен (SRP/Кюретаж), 
      1.3.3 Химическое и ламповое отбеливание.
2. ОРТОПЕДИЯ
   2.1. Конструкции: 
      2.1.1 Виниры (Керамика/Композит), 
      2.1.2 Коронки (Диоксид циркония/Emax/Металлокерамика), 
      2.1.3 Мостовидные протезы и консоли, 
      2.1.4 Микропротезирование (Inlay/Onlay/Overlay).
   2.2. Техника и Протоколы: 
      2.2.1 Вертикальное препарирование (BOPT/Vertiprep), 
      2.2.2 Традиционное уступное препарирование, 
      2.2.3 Оттиски (Силиконы/Полиэфиры), 
      2.2.4 Ретракция десны и гемостаз, 
      2.2.5 Временное протезирование (ПММА/Бисакрил), 
      2.2.6 Адгезивная и цементная фиксация (Fuji/Panavia).
   2.3. Гнатология: 
      2.3.1 Окклюзия, прикус и центральное соотношение (ЦС), 
      2.3.2 ВНЧС (МРТ/Диагностика/Сплинты), 
      2.3.3 Инструментальный анализ (Артикуляторы/Лицевые дуги).
   2.4. Съемное протезирование: 
      2.4.1 Полные и частичные съемные протезы (Акрил/Нейлон), 
      2.4.2 Бюгельное протезирование и замковые крепления, 
      2.4.3 Перебазировка и починка протезов.
3. ХИРУРГИЯ
   3.1. Амбулаторная хирургия: 
      3.1.1 Удаление зубов любой сложности (Восьмерки), 
      3.1.2 Апикальная микрохирургия (Резекция/Ретроградное пломбирование), 
      3.1.3 Зубосохраняющие операции (Гемисекция/Коронковое удлинение).
   3.2. Имплантация: 
      3.2.1 Планирование, шаблоны и навигационная хирургия, 
      3.2.2 Системы имплантатов и протоколы установки, 
      3.2.3 Ортопедические компоненты (Мультиюниты/Абатменты), 
      3.2.4 Осложнения (Периимплантит/Расфиксация винтов).
   3.3. Реконструктивная хирургия: 
      3.3.1 Мукогингивальная пластика (ССТ/СДТ/Пластика десны), 
      3.3.2 Костная пластика (НКР/Синус-лифтинг/Блоки).
4. ОРТОДОНТИЯ: 
   4.1.1 Брекет-системы и механика перемещения, 
   4.1.2 Элайнеры и цифровое планирование, 
   4.1.3 Ортодонтическая диагностика (ТРГ/Фотометрия).
5. ЦИФРОВЫЕ ТЕХНОЛОГИИ: 
   5.1.1 Интраоральное сканирование, 
   5.2.1 Цифровое моделирование (Exocad/3Shape), 
   5.3.1 Производство (3D-печать/Фрезеровка).
6. ОБЩЕЕ: 
   6.1.1 Эргономика и Оборудование (Микроскопы/Бинокуляры/Свет), 
   6.2.1 Фармакология (Анестезия/Антибиотики/НПВС), 
   6.3.1 Стоматологический фотопротокол.
7. МЕНЕДЖМЕНТ: 
   7.1.1 Экономика клиники (Зарплаты/Цены/Маркетинг), 
   7.2.1 Юридическая защита и документация (Медкарты/ИДС), 
   7.3.1 Психология общения и управление конфликтами.
"""

class ModelJsonError(Exception):
    """В ответе модели нет ни одного целого объекта с кодами категорий."""


def extract_codes(text):
    """Собрать коды из ответа модели, чем бы модель объект ни обложила.

    Прежний разбор искал объект жадно: от первой открывающей скобки до
    ПОСЛЕДНЕЙ закрывающей во всём ответе. Замер на 9 реалистичных ответах: 6 не
    разбирались вообще. Хватало одной закрывающей скобы в болтовне после объекта
    («надеюсь, помогло }»), второго объекта, вложенного "meta": {...} или
    повтора образца формата из промпта. Для врача это значит, что факт остаётся
    под ЧУЖОЙ категорией и он его не найдёт, а видео-протокол не записывается
    вовсе — при том, что модель ответила правильно.

    Коды собираются из ВСЕХ целых объектов ответа, по порядку и без дублей:
    ответ из двух объектов — это два набора кодов, а не мусор. Образец формата
    из промпта безопасен: он написан как X.X.X, и цифровой фильтр вызывающего
    его выбрасывает.

    Пусто не возвращается никогда: либо коды, либо ModelJsonError с причиной.
    Молчаливая пустота здесь означала бы код-заглушку в базе, а факт с ней врачу
    недостижим навсегда.
    """
    objects = []
    for chunk in _iter_json_objects(text or ""):
        try:
            obj = json.loads(chunk)
        except Exception:
            continue
        if isinstance(obj, dict):
            objects.append(obj)

    codes = []
    for obj in objects:
        value = obj.get("codes")
        if isinstance(value, list):
            for item in value:
                if item not in codes:
                    codes.append(item)
        elif isinstance(value, (str, int, float)):
            # Модель периодически отдаёт один код строкой, а не массивом.
            if value not in codes:
                codes.append(value)
    if codes:
        return codes

    raw = (text or "").strip()
    if not objects:
        raise ModelJsonError(
            f"целого объекта в ответе нет (ответ обрезан?): {len(raw)} симв., "
            f"конец ответа: {raw[-80:]!r}")
    raise ModelJsonError(
        f"объект в ответе есть, но кодов в нём нет (ключа codes нет либо массив "
        f"пуст): ключи {[sorted(o)[:5] for o in objects[:3]]}")

async def classify_fact(client, content, f_id):
    prompt = f"""
    ТЫ — СТОМАТОЛОГИЧЕСКИЙ ЭКСПЕРТ-АНАЛИТИК.
    Твоя задача: проанализировать текст и присвоить ему подходящие коды категорий.

    ДЕРЕВО КАТЕГОРИЙ:
    {KNOWLEDGE_TREE}

    ТЕКСТ ДЛЯ КЛАССИФИКАЦИИ:
    {content}

    ПРАВИЛА:
    1. ИСПОЛЬЗУЙ МУЛЬТИ-ТЕГИ: Если факт затрагивает несколько тем, укажи ВСЕ подходящие коды. 
    2. МАКСИМАЛЬНАЯ ТОЧНОСТЬ: Выбирай L3 коды (три цифры), если это возможно.
    3. ОГРАНИЧЕНИЕ: Не более 5 кодов на один факт.
    4. ФОРМАТ ОТВЕТА: СТРОГО JSON: {{"codes": ["X.X.X", "Y.Y.Y"]}}
    5. ТОЛЬКО ЦИФРЫ: В массив "codes" пиши ТОЛЬКО цифровой код. Названия текстом писать КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО.
    """
    try:
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
        if not response or not response.text:
            # Пустой ответ раньше уезжал в базу кодом 10.1 с is_reclassified = 1.
            # Этого кода нет ни в одной подтеме рубрикатора, а прежний код уже
            # затёрт: факт становился недостижим для врача НАВСЕГДА, потому что
            # следующий прогон помеченный факт не берёт. Лучше не перезаписывать.
            print(f"   [ОТКАЗ] ID {f_id}: модель вернула пустой ответ, "
                  f"код НЕ перезаписан.")
            return None

        raw_codes = extract_codes(response.text)
        clean_codes = []
        for c in raw_codes:
            # Регулярка вырезает только паттерны типа 1.1 или 1.1.1
            match = re.search(r'(\d+\.\d+(?:\.\d+)?)', str(c))
            if match:
                clean_codes.append(match.group(1))
        if not clean_codes:
            print(f"   [ОТКАЗ] ID {f_id}: в ответе нет ни одного цифрового кода "
                  f"({raw_codes[:5]}), код НЕ перезаписан.")
            return None
        # dict.fromkeys, а не set: порядок кодов не должен меняться от прогона к
        # прогону, иначе один и тот же ответ модели даёт разную строку в базе.
        return ", ".join(dict.fromkeys(clean_codes))
    except ModelJsonError as exc:
        # Отдельно и ПЕРВЫМ, до общей ветки: сообщение json.loads про обрезанный
        # ответ — "Expecting ',' delimiter", и подстрока "limit" сидит внутри
        # слова delimiter. Общая ветка читала это как лимит квоты и возвращала
        # RETRY: замер — 13 вызовов модели, 26 с сна и обрыв ВСЕГО прогона на
        # первом же обрезанном ответе с ложным «Все ключи в лимите».
        print(f"   [ОТКАЗ РАЗБОРА] ID {f_id}: {exc}")
        return None
    except Exception as e:
        err = str(e).lower()
        # \b обязателен: без границ слова «limit» находится внутри «delimiter»,
        # и ошибка разбора выдавала себя за лимит квоты.
        if "429" in err or "exhausted" in err or re.search(r"\b(limit|quota)\b", err):
            return "RETRY"
        print(f"   [!] Error on ID {f_id}: {err[:100]}")
        return None

def backup_path_for(db_path, now=None):
    """Имя копии рядом с базой: wiki_backup_<дата>_<время>.db.

    Время в имени, а не одна дата: VACUUM INTO отказывается писать в уже
    существующий файл ("output file already exists"), и второй прогон за сутки
    падал бы на имени, так и не переклассифицировав ни один факт.
    """
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    folder = os.path.dirname(os.path.abspath(db_path))
    return os.path.join(folder, f"{BACKUP_PREFIX}{stamp}.db")

async def backup_before_write(db_path, now=None):
    """Снять копию базы ДО первой перезаписи category_code.

    Перезапись категории безвозвратна: прежний код нигде не хранился. Ошибка
    классификатора на 12 784 фактах означала бы, что врач ищет «ВНЧС» и не
    находит его больше никогда, а вернуть прежнюю разметку нечем. Поэтому копия
    здесь не удобство: если она не снялась или снялась битой, прогон обязан
    остановиться, не перезаписав ничего. Возвращает путь копии, иначе RuntimeError.
    """
    target = backup_path_for(db_path, now)
    if os.path.exists(target):
        raise RuntimeError(f"файл копии уже существует: {target}")
    try:
        # Отдельное соединение и никакой открытой транзакции: VACUUM внутри
        # транзакции отказывает ("cannot VACUUM from within a transaction").
        async with aiosqlite.connect(db_path, timeout=60) as db:
            await db.execute("VACUUM INTO ?", (target,))
            cursor = await db.execute("SELECT COUNT(*) FROM distilled_facts")
            expected = (await cursor.fetchone())[0]
    except Exception as exc:
        raise RuntimeError(f"VACUUM INTO не выполнен: {type(exc).__name__}: {exc}") from exc

    size, copied = verify_copy(target, expected)
    print(f"--- Копия базы снята: {target} ({size} байт, {copied} фактов) ---")
    return target


def verify_copy(target, expected):
    """Проверить копию КАК КОПИЮ, а не как факт вызова VACUUM.

    Вынесено отдельной функцией, чтобы каждую ветку отказа можно было проверить
    подложенным файлом: непроверяемая проверка копии — это та же надежда, только
    записанная кодом. Замер на подложенных файлах (SQLite 3.50.4):
    обрезанная копия ОТКРЫВАЕТСЯ и отдаёт COUNT(*) = 200, а integrity_check при
    этом говорит «row 181 missing from index idx_cat» — то есть без сверки
    integrity такая копия молча считалась бы годной, и врач узнал бы правду в
    день, когда пошёл бы по ней восстанавливать разметку.
    Возвращает (размер, число фактов), иначе RuntimeError.
    """
    # VACUUM INTO мог не создать файл (нет прав, нет места, нет каталога).
    if not os.path.exists(target):
        raise RuntimeError(f"копия не создана: {target}")
    size = os.path.getsize(target)
    if size <= 0:
        raise RuntimeError(f"копия пустая (0 байт): {target}")
    try:
        uri = "file:" + os.path.abspath(target).replace(os.sep, "/") + "?mode=ro"
        probe = sqlite3.connect(uri, uri=True)
        try:
            integrity = probe.execute("PRAGMA integrity_check").fetchone()[0]
            copied = probe.execute("SELECT COUNT(*) FROM distilled_facts").fetchone()[0]
        finally:
            probe.close()
    except Exception as exc:
        raise RuntimeError(f"копия не читается: {type(exc).__name__}: {exc}") from exc
    if integrity != "ok":
        raise RuntimeError(f"integrity_check копии: {integrity}")
    if copied != expected:
        raise RuntimeError(f"в копии {copied} фактов вместо {expected}")
    return size, copied

async def ensure_schema(db_path):
    """Идемпотентная миграция: is_reclassified и category_code_prev.

    category_code_prev хранит код, который прогон только что затёр. Без него
    единственная деструктивная операция лана неоткатываема на уровне строки, и
    факт остаётся висеть под чужой категорией — врач его не найдёт.

    Прежняя версия глотала ЛЮБУЮ ошибку голым `except: pass`: занятая база,
    отсутствующая таблица и опечатка в DDL выглядели одинаково успешно.
    """
    added = []
    async with aiosqlite.connect(db_path, timeout=60) as db:
        cursor = await db.execute("PRAGMA table_info(distilled_facts)")
        columns = {row[1] for row in await cursor.fetchall()}
        if not columns:
            raise RuntimeError(
                f"в {db_path} нет таблицы distilled_facts — переклассифицировать нечего")
        for name, ddl in (("is_reclassified", "BOOLEAN DEFAULT 0"),
                          ("category_code_prev", "TEXT")):
            if name in columns:
                continue
            try:
                await db.execute(f"ALTER TABLE distilled_facts ADD COLUMN {name} {ddl}")
            except sqlite3.OperationalError as exc:
                # Гонка двух прогонов: колонку успел добавить сосед. Всё
                # остальное (нет таблицы, база занята) должно быть слышно.
                if "duplicate column" not in str(exc).lower():
                    raise
            else:
                added.append(name)
        await db.commit()
    if added:
        print(f"--- Схема обновлена, добавлены колонки: {', '.join(added)} ---")
    return added

async def main():
    # Проверяем существование ДО первого connect: aiosqlite создаёт пустой файл
    # молча, и дальше «база найдена» становилось правдой про пустышку.
    if not os.path.exists(DB_PATH):
        print(f"[СТОП] База не найдена: {os.path.abspath(DB_PATH)}")
        return

    if not config.GOOGLE_KEYS:
        print("[СТОП] config.GOOGLE_KEYS пуст — классифицировать нечем, прогон отменён.")
        return

    await ensure_schema(DB_PATH)

    # Считаем работу ДО копии: на боевом снимке is_reclassified = 0 стоит у нуля
    # фактов, и прогон «на всякий случай» оставлял бы копию на 9 158 656 байт,
    # ничего не переклассифицировав. Каталог врача забивается пустыми копиями, а
    # среди них потом не найти ту единственную, которая нужна для отката.
    async with aiosqlite.connect(DB_PATH, timeout=60) as probe:
        cursor = await probe.execute(
            "SELECT COUNT(*) FROM distilled_facts WHERE is_reclassified = 0")
        pending = (await cursor.fetchone())[0]
    if not pending:
        print("--- Нечего переклассифицировать: is_reclassified = 0 ни у одного факта. "
              "Копия не снималась, база не тронута. ---")
        return

    try:
        await backup_before_write(DB_PATH)
    except Exception as exc:
        print(f"[СТОП] Резервная копия базы НЕ снята: {exc}")
        print("       Ни один category_code не перезаписан. Прежняя разметка цела.")
        raise SystemExit(1)

    async with aiosqlite.connect(DB_PATH, timeout=60) as db:
        cursor = await db.execute("SELECT id, content FROM distilled_facts WHERE is_reclassified = 0")
        facts = await cursor.fetchall()
        total_remaining = len(facts)
        
        print(f"--- Processing {total_remaining} facts with Gemma 3 27B ---")
        
        key_idx = 0
        idx = 0
        fails = 0
        rotations = 0
        skipped = 0
        aborted = False
        while idx < len(facts):
            f_id, content = facts[idx]
            current_key = config.GOOGLE_KEYS[key_idx % len(config.GOOGLE_KEYS)]
            client = genai.Client(api_key=current_key)

            new_codes = await classify_fact(client, content, f_id)

            if new_codes == "RETRY":
                rotations += 1
                if rotations > MAX_ROTATIONS_PER_FACT:
                    print(f"   [СТОП] Все ключи в лимите на ID {f_id} после {rotations} ротаций. "
                          f"Переклассифицировано {idx} из {total_remaining}, остальные ждут "
                          f"следующего прогона со старыми кодами.")
                    aborted = True
                    break
                print(f"   [!] Key {key_idx % len(config.GOOGLE_KEYS) + 1} TPM limit. Rotating...")
                key_idx += 1
                await asyncio.sleep(SLEEP_ON_ROTATE)
                continue # Retry same index

            if new_codes:
                # Прежний код уезжает в category_code_prev тем же UPDATE-ом: иначе
                # ошибка классификатора хоронит разметку факта, и врач его не найдёт.
                await db.execute('UPDATE distilled_facts SET category_code_prev = category_code, category_code = ?, is_reclassified = 1 WHERE id = ?', (new_codes, f_id))
                await db.commit()
                print(f"[{idx+1}/{total_remaining}] Key:{key_idx % len(config.GOOGLE_KEYS) + 1} | ID {f_id} -> {new_codes}")
                idx += 1
                key_idx += 1
                fails = 0
                rotations = 0
            else:
                # Без этой ветки idx стоял на месте и скрипт вечно долбил один
                # факт: остальные так и не обновлялись, а квота выгорала молча.
                fails += 1
                if fails >= MAX_FAILS_PER_FACT:
                    print(f"   [ПРОПУСК] ID {f_id} не классифицирован за {fails} попытки, "
                          f"остаётся со старым кодом и is_reclassified = 0.")
                    idx += 1
                    key_idx += 1
                    fails = 0
                    rotations = 0
                    skipped += 1

            # Safe interval to respect 15k TPM limit per key
            await asyncio.sleep(SLEEP_BETWEEN_FACTS)

    if aborted:
        # Прогон, оборванный на первом же факте, раньше заканчивался строкой
        # BASE RECLASSIFIED SUCCESSFULLY: оператор читал успех там, где не
        # переклассифицировано НИ ОДНОГО факта, и повторного прогона не делал.
        print(f"\n--- ПРОГОН ОБОРВАН на {idx} из {total_remaining}: остальные факты "
              f"остались со СТАРЫМИ кодами, нужен повторный прогон (см. [СТОП] выше) ---")
    elif skipped:
        print(f"\n--- ГОТОВО, но {skipped} фактов пропущено (см. [ПРОПУСК] выше) ---")
    else:
        print("\n--- BASE RECLASSIFIED SUCCESSFULLY ---")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped by user.")