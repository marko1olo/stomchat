import asyncio
import aiosqlite
import os
import sys
from pathlib import Path

import taxonomy

# Проверку классификации запускают с перенаправлением вывода в файл. Тогда stdout
# берёт кодировку системы — на этой машине cp1251, — и первый же print с эмодзи
# роняет весь инструмент: врач (и ведущий) не увидит ни одной строки о том, куда
# легли факты, а в трейсбеке будет UnicodeEncodeError вместо настоящей причины.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

DB_PATH = "stomat_wiki.db"

# Имена рубрик берутся из taxonomy.py — это ТОТ ЖЕ объект, а не копия. Своя карта
# здесь была третьей копией и уже разошлась: 53 кода против 55 в выгрузке, причём
# `10.1` она называла "UNCLASSIFIED", а разделов 8-10 (детская стоматология,
# материаловедение, прочее) не знала вообще. Проверка классификации показывала
# «неизвестный код» там, где рубрика существует, и наоборот — то есть врал именно
# тот инструмент, которым проверяют, куда легли факты врача.
CAT_NAMES = taxonomy.DISPLAY_NAMES

async def inspect():
    if not os.path.exists(DB_PATH):
        print("❌ База не найдена.")
        return

    # Вику открываем ТОЛЬКО на чтение: это инструмент осмотра, а любая случайная
    # запись стоила бы 12 784 фактов — резервную копию вики в этом лане не делает
    # никто, и восстанавливать врачу базу знаний будет не из чего.
    uri = Path(DB_PATH).resolve().as_uri() + "?mode=ro"
    async with aiosqlite.connect(uri, uri=True) as db:
        # Берем 10 случайных фактов, которые уже прошли реклассификацию
        cursor = await db.execute('SELECT id, content, category_code FROM distilled_facts WHERE is_reclassified = 1 ORDER BY RANDOM() LIMIT 25')
        rows = await cursor.fetchall()
        
        print("\n🔎 ВЫБОРОЧНАЯ ПРОВЕРКА КЛАССИФИКАЦИИ:\n")
        
        for f_id, content, cat_codes in rows:
            print(f"🆔 FACT ID: {f_id}")
            print(f"📝 ТЕКСТ: {content[:250]}...") # Показываем начало текста
            
            print("🏷  КАТЕГОРИИ:")
            if cat_codes:
                # Разбор списка через запятую — общий на весь проект: 99.1 % записей
                # хранят в category_code список, и каждый файл резал его по-своему.
                for c in taxonomy.parse_codes(cat_codes):
                    # describe(), а не подстановка «неизвестный код»: у 49 живых
                    # кодов из 110 достоверного имени нет, и честное «БЕЗ ИМЕНИ» с
                    # номером раздела говорит, где искать, а «неизвестный код»
                    # выглядит как сбой инструмента и осмотр на этом останавливался.
                    print(f"   • {c} -> {taxonomy.describe(c)}")
            else:
                print("   ❌ Нет категорий!")
            
            print("-" * 50)

if __name__ == '__main__':
    asyncio.run(inspect())