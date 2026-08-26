import asyncio
import aiosqlite
import os
import sys
from pathlib import Path

import taxonomy

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

# Карта «код -> имя файла ревью» живёт в taxonomy.py и здесь НЕ дублируется: это
# ТОТ ЖЕ объект, а не копия. Копий было пять, и они разошлись — в checker.py 53
# кода против 55 здесь, в дереве reclass.py разделов 8-10 не было вовсе. Факт под
# кодом, которого не знает выгрузка, врач не найдёт никогда: в интерфейсе раздел
# выглядит пустым, хотя факты в базе есть (замер: 51 такой факт).
CAT_MAP = taxonomy.EXPORT_SLUGS


def read_only_uri(path):
    """Боевую вику и архив открываем только на чтение.

    Выгрузка из них лишь читает, а случайная запись стоила бы 12 784 фактов:
    резервную копию вики в этом лане не делает никто.
    """
    return Path(path).resolve().as_uri() + "?mode=ro"


def category_patterns(cat_code):
    """Два шаблона LIKE для одного кода: сам код и его подкоды.

    Правило границы токена держит taxonomy.token_patterns — там же, где живут коды,
    и там же, где его берёт навигация бота. Пока правило было переписано в каждом
    файле по-своему, экспорт разложил 51 факт по 14 ЧУЖИМ файлам (1.3.10 попал в
    файл профгигиены 1.3.1, 11.1.1 — в файл эндодонтии 1.1.1): врач читал чужой
    раздел как свой, а это хуже потери — пропажу хотя бы видно.
    """
    return taxonomy.token_patterns(cat_code)


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

            # Ищем факты, где этот код стоит ОТДЕЛЬНЫМ токеном списка. Само условие
            # собирает taxonomy.token_sql: если границу токена поправят, поправится
            # и выгрузка, и навигация, а не один из двух путей — иначе врач видит в
            # боте один набор статей, а в методичке по тому же разделу другой.
            cursor = await db.execute(f'''
                SELECT f.id, f.content, f.source_ids, f.is_case, f.confidence
                FROM distilled_facts f
                WHERE {taxonomy.token_sql("f.category_code")}
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
            # Разбор списка кодов один на весь проект: 99.1 % записей вики хранят в
            # category_code список через запятую, и каждый файл резал его по-своему.
            for token in taxonomy.parse_codes(codes):
                if not taxonomy.is_exportable(token):
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
