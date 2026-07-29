import asyncio
import aiosqlite
import os
import re
import sys
from pathlib import Path

# Экспорт запускают с перенаправлением вывода в файл (python savdel.py > log).
# Тогда stdout берёт кодировку системы — замер на этой машине: cp1251, — и
# первый же print с эмодзи роняет экспорт целиком: ни одного файла ревью, а в
# трейсбеке UnicodeEncodeError вместо настоящей причины.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ARCHIVE_DB = "stomat_archive.db"
WIKI_DB = "stomat_wiki.db"
OUTPUT_DIR = "wiki_final_review"
# Свалка для фактов, чьих кодов нет в CAT_MAP. Расширение НЕ .txt намеренно:
# prompter.py забирает из этой папки каждый .txt и заказывает по нему платную
# монографию у Гемини — по мусорному коду это выброшенные деньги. Без файла
# такие факты не видны нигде: замер по боевой вике — 51 факт.
LEFTOVER_FILE = "00_НЕРАЗОБРАННОЕ.md"

# ПОЛНАЯ КАРТА ИМЕН (Соответствует дереву 5.0)
CAT_MAP = {
   # 1. ТЕРАПИЯ
   "1.1.1": "Эндо_Доступ_МБ2",
   "1.1.2": "Эндо_Инструментация",
   "1.1.3": "Эндо_Ирригация",
   "1.1.4": "Эндо_Обтурация",
   "1.1.5": "Эндо_Перелечивание",
   "1.1.6": "Эндо_Диагностика",
   "1.2.1": "Реставрация_Адгезия_IDS",
   "1.2.2": "Реставрация_Спиртовой_протокол",
   "1.2.3": "Реставрация_Морфология",
   "1.2.4": "Реставрация_Матрицы",
   "1.2.5": "Реставрация_Билдап_Штифты",
   "1.2.6": "Реставрация_Полировка",
   "1.3.1": "Профгигиена_GBT",
   "1.3.2": "Пародонтология_SRP",
   "1.3.3": "Отбеливание",
   # 2. ОРТОПЕДИЯ
   "2.1.1": "Орто_Виниры",
   "2.1.2": "Орто_Коронки",
   "2.1.3": "Орто_Мосты",
   "2.1.4": "Орто_Микропротезирование",
   "2.2.1": "Орто_Техника_BOPT_Verti",
   "2.2.2": "Орто_Техника_Уступ",
   "2.2.3": "Орто_Оттиски",
   "2.2.4": "Орто_Ретракция",
   "2.2.5": "Орто_Временные",
   "2.2.6": "Орто_Фиксация_Цементы",
   "2.3.1": "Гнатология_Окклюзия",
   "2.3.2": "Гнатология_ВНЧС",
   "2.3.3": "Гнатология_Артикуляторы",
   "2.4.1": "Съемное_Полные",
   "2.4.2": "Съемное_Бюгельные",
   "2.4.3": "Съемное_Перебазировка",
   # 3. ХИРУРГИЯ
   "3.1.1": "Хирургия_Удаление",
   "3.1.2": "Хирургия_Апикальная",
   "3.1.3": "Хирургия_Зубосохраняющие",
   "3.2.1": "Имплантация_Планирование",
   "3.2.2": "Имплантация_Системы",
   "3.2.3": "Имплантация_Компоненты",
   "3.2.4": "Имплантация_Осложнения",
   "3.3.1": "Пластика_Десны",
   "3.3.2": "Костная_Пластика",
   # 4. ОРТОДОНТИЯ
   "4.1.1": "Ортодонтия_Брекеты",
   "4.1.2": "Ортодонтия_Элайнеры",
   "4.1.3": "Ортодонтия_Диагностика",
   # 5. ЦИФРА
   "5.1.1": "Цифра_Сканеры",
   "5.2.1": "Цифра_Exocad",
   "5.3.1": "Цифра_3D_Печать",
   # 6. ОБЩЕЕ
   "6.1.1": "Оборудование_Оптика",
   "6.2.1": "Фармакология",
   "6.3.1": "Фотопротокол",
   # 7. МЕНЕДЖМЕНТ
   "7.1.1": "Менеджмент_Экономика",
   "7.2.1": "Менеджмент_Юридическое",
   "7.3.1": "Менеджмент_Психология",
   # 8-10. Разделы дерева distiller.py, которых не было в этой карте. Дерево
   # distiller.py:259-264 их выдаёт, FALLBACK_CODE там же — "10.1.1". Пока в
   # боевой вике таких фактов 0 (reclass.py переклеил их в разделы 1-7 ещё до
   # выгрузки), но без имени каждый новый факт детской стоматологии уходил бы
   # в никуда молча — врач не узнает, что раздела нет.
   "8.1.1": "Детская_Прием_детей",
   "9.1.1": "Материаловедение_Обзоры_брендов",
   "10.1.1": "Прочее_Не_клиника"
}

# Код категории — только цифры и точки, от 2 до 4 уровней. Проверка нужна не для
# порядка: код уходит в шаблон LIKE, где '_' и '%' — подстановочные знаки, и
# кривой код молча набрал бы в файл чужие факты.
CODE_RE = re.compile(r"^\d{1,2}(?:\.\d{1,2}){1,3}$")


def read_only_uri(path):
    """Боевую вику и архив открываем только на чтение.

    Выгрузка из них лишь читает, а случайная запись стоила бы 12 784 фактов:
    резервную копию вики в этом лане не делает никто.
    """
    return Path(path).resolve().as_uri() + "?mode=ro"


def category_patterns(cat_code):
    """Два шаблона LIKE для одного кода: сам код и его подкоды.

    До правки стояло '%2.1.1%' — совпадение подстрокой без границ токена. Замер
    по backup_wiki_18_0140.db (тот же конвейер, снимок до реклассификации): 51
    факт разложен по 14 ЧУЖИМ файлам — 1.3.10 попал в файл профгигиены 1.3.1,
    11.1.1 в файл эндодонтии 1.1.1, 2.2.3.1 в файл окклюзии 2.3.1. Врач читает
    чужой раздел как свой, и это хуже потери: пропажу хотя бы видно.
    """
    if not CODE_RE.match(cat_code):
        raise ValueError(f"недопустимый код категории: {cat_code!r}")
    return f"%,{cat_code},%", f"%,{cat_code}.%"


async def export_v7():
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)

    print(f"🚀 Начинаю экспорт v7 (Мульти-теги и Имена)...")

    # id фактов, попавших хотя бы в один файл. Всё, чего здесь нет, ниже уходит
    # в свалку с названным кодом, а не пропадает.
    matched_ids = set()

    async with aiosqlite.connect(read_only_uri(WIKI_DB), timeout=60, uri=True) as db:
        # Путь уходит ПАРАМЕТРОМ. С подстановкой в текст SQL апостроф в пути
        # (проверено на каталоге с кавычкой) давал syntax error и ронял весь
        # экспорт, а сообщение указывало на середину пути, а не на путь.
        await db.execute("ATTACH DATABASE ? AS archive", (read_only_uri(ARCHIVE_DB),))

        # Проходим по нашему эталонному словарю
        for cat_code, cat_name in CAT_MAP.items():
            print(f"📦 Сборка: {cat_code} ({cat_name})...", end=" ")

            # Ищем факты, где этот код стоит ОТДЕЛЬНЫМ токеном списка: список
            # обрамляем запятыми, чтобы у каждого кода была граница.
            cursor = await db.execute('''
                SELECT f.id, f.content, f.source_ids, f.is_case, f.confidence
                FROM distilled_facts f
                WHERE (',' || REPLACE(f.category_code, ' ', '') || ',') LIKE ?
                   OR (',' || REPLACE(f.category_code, ' ', '') || ',') LIKE ?
            ''', category_patterns(cat_code))

            facts = await cursor.fetchall()

            # Имя файла: Код + Название
            safe_name = f"{cat_code}_{cat_name}".replace('/', '_').replace(' ', '_')
            file_path = os.path.join(OUTPUT_DIR, f"{safe_name}.txt")

            if not facts:
                # Категория опустела — именно так reclass.py вычистил разделы 8-10
                # целиком (замер по снимку до реклассификации: 139 фактов). Файл
                # прошлой выгрузки надо снять: prompter.py:19 забирает из этой папки
                # КАЖДЫЙ .txt и заказывает по нему платную монографию у Гемини. Иначе
                # врач получит методичку по фактам, которых в вике уже нет, и ничем
                # не отличит её от актуальной — а деньги за неё уже уплачены.
                if os.path.exists(file_path):
                    os.remove(file_path)
                    print("пусто (снят устаревший файл прошлой выгрузки)")
                else:
                    print("пусто")
                continue

            with open(file_path, "w", encoding="utf-8") as f:
                f.write(f"=== {cat_code}: {cat_name.replace('_', ' ').upper()} ===\n")
                f.write(f"Найдено записей: {len(facts)}\n")
                f.write("="*60 + "\n\n")

                for fact_id, content, s_ids, is_case, conf in facts:
                    matched_ids.add(fact_id)
                    type_str = "🌟 [ЭКСПЕРТНЫЙ КЕЙС]" if is_case else "📍 [ФАКТ]"
                    f.write(f"{type_str}\n")
                    f.write(f"{content}\n")

                    # Подтягиваем фото. isascii() рядом с isdigit() обязателен:
                    # isdigit() истинен и для '²', и для арабских цифр — такой
                    # "id" доходил до SQL и ронял экспорт целиком.
                    ids = [x.strip() for x in (s_ids or '').split(',')
                           if x.strip().isascii() and x.strip().isdigit()]
                    if ids:
                        # В текст запроса подставляются только знаки '?', сами
                        # id уходят параметрами.
                        holders = ",".join("?" * len(ids))
                        async with db.execute(f'''
                            SELECT msg_id, vision_description
                            FROM archive.archive_messages
                            WHERE msg_id IN ({holders}) AND vision_processed=1
                            AND vision_description NOT IN ('', 'SKIP', 'SKIP_ERROR')
                        ''', ids) as c:
                            images = await c.fetchall()
                            for m_id, v_desc in images:
                                f.write(f"   📷 [ИЛЛЮСТРАЦИЯ: {m_id}] {v_desc}\n")

                    f.write("-" * 40 + "\n")
            print(f"✅ ({len(facts)} шт.)")

        # Факты, чьих кодов нет в CAT_MAP, до правки не попадали НИ В ОДИН файл
        # и в выводе об этом не было ни строки. Замер по боевой вике: 58 кодов
        # без имени и 51 факт, невидимый целиком. Врач их не прочитает и не
        # узнает, что их нет.
        async with db.execute(
                "SELECT id, category_code, content, is_case FROM distilled_facts") as c:
            leftovers = [row for row in await c.fetchall() if row[0] not in matched_ids]

    if leftovers:
        unknown = {}
        for _id, codes, _content, _is_case in leftovers:
            for token in str(codes or "").split(','):
                token = token.strip()
                if token and token not in CAT_MAP:
                    unknown[token] = unknown.get(token, 0) + 1
        leftover_path = os.path.join(OUTPUT_DIR, LEFTOVER_FILE)
        with open(leftover_path, "w", encoding="utf-8") as f:
            f.write("=== ФАКТЫ БЕЗ КАТЕГОРИИ В CAT_MAP ===\n")
            f.write(f"Найдено записей: {len(leftovers)}\n")
            f.write("Коды, которых нет в карте: "
                    + ", ".join(f"{k} ({v})" for k, v in sorted(unknown.items(),
                                                                key=lambda kv: -kv[1]))
                    + "\n")
            f.write("=" * 60 + "\n\n")
            for _id, codes, content, is_case in leftovers:
                type_str = "🌟 [ЭКСПЕРТНЫЙ КЕЙС]" if is_case else "📍 [ФАКТ]"
                f.write(f"{type_str} код: {codes}\n")
                f.write(f"{content}\n")
                f.write("-" * 40 + "\n")
        # Без эмодзи: этот текст печатается и когда reconfigure выше не удался.
        print(f"[!] Вне карты: {len(leftovers)} фактов, {len(unknown)} кодов "
              f"-> {LEFTOVER_FILE}")
    else:
        # Свалка прошлой выгрузки тоже устаревает: если её не снять, врач читает
        # список "потерянных" фактов, которые давно разложены по разделам.
        stale_leftover = os.path.join(OUTPUT_DIR, LEFTOVER_FILE)
        if os.path.exists(stale_leftover):
            os.remove(stale_leftover)
        print("Вне карты: 0 фактов")

    print(f"\n🏁 Готово! Папка: {OUTPUT_DIR}")

if __name__ == '__main__':
    asyncio.run(export_v7())
