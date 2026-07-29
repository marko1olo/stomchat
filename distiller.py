# -*- coding: utf-8 -*-
"""
Сито: превращает реплики чата (`stomat_archive.db`) в статьи базы знаний
(`stomat_wiki.db`).

=== ЧТО БЫЛО ЗАМЕРЕНО НА ЖИВЫХ БАЗАХ 2026-07-29 (только чтение, mode=ro) ===

    реплик в архиве                        117 847  (msg_id 5..139701)
    помечено is_processed_for_wiki=1        117 403
    кандидатов к дистилляции сейчас               0
    фактов в вики                           12 784
    уникальных msg_id в source_ids          48 361, из них есть в архиве 47 832
    ПОКРЫТИЕ АРХИВА ФАКТАМИ                  40.59 %
    НИКОГДА не отражено ни в одном факте     70 015 реплик (59.41 %)
      из них длиннее 150 символов            3 974  (это не шум, это статьи)
      из них с медиа                         9 794

Прогон 2026-02-17..19 по `distiller.log`: 1 731 вызов модели (200: 1 636,
429: 72, 404: 19, 400: 4), провалов парсинга JSON — 0. То есть транспорт
работал; 59 % архива выжжено не отказами API, а тем, что пачку помечали
обработанной независимо от результата.

=== ЧТО ЗДЕСЬ ПОЧИНЕНО (по величине потери знания) ===

F1. `print()` с эмодзи. На этой машине stdout = cp1251, и `print("*лампочка*")`
    поднимает UnicodeEncodeError. Первый же print (счётчик прогресса) убивал
    прогон ДО первой пачки — сито было нерабочим в принципе. Хуже: вывод текста
    факта стоял ВНУТРИ `async with wiki` ДО `commit()`, поэтому любой
    непечатаемый символ терял ВСЮ пачку фактов молча. Замер: 65 из 12 784
    фактов (0.51 %) содержат символы, непечатаемые в cp1251 (`₽`, `²`, `é`,
    `ö`), а 10 074 из 107 316 реплик архива (9.4 %) — тем более. Теперь весь
    вывод идёт через `say()` с гарантированной заменой, а печать фактов вынесена
    из транзакции.

F2. `is_processed_for_wiki = 1` ставился вне ветки `if facts:`. Три разных
    состояния были слиты в одно «пусто»: отказ транспорта (`raw_res is None`),
    провал парсинга JSON и честное «фактов в пачке нет». Пачка выжигалась
    навсегда в любом из трёх случаев. Теперь `process_batch` возвращает статус:
    отказ -> пачка НЕ помечается и повторяется (до MAX_BATCH_ATTEMPTS), честное
    «пусто» -> помечается, но в лог уходит диапазон msg_id и клиническая
    плотность пачки, чтобы потеря была видна, а не молчала.

F3. `source_ids` писались без нормализации: `",".join(map(str, f.get('s', [])))`.
    Замер: у 353 фактов (2.76 %) ВСЕ токены имеют вид `MSG_12345` — 2 073 битых
    токена; `savdel.py:113` фильтрует их `isdigit()` и выбрасывает, поэтому у
    этих фактов нет ни провенанса, ни иллюстраций. Ещё 759 фактов (5.94 %)
    ссылаются на 2 049 msg_id, которых в архиве нет вообще (модель придумала
    цифры: `1080108`, `1144522` — семизначных msg_id в архиве не бывает).
    Теперь id вытаскивается регуляркой и пересекается с msg_id ЭТОЙ пачки;
    отброшенное логируется поштучно.

F4. Код категории брался как есть. Замер: в базе 110 различных кодов при 52
    легальных, 393 присвоения приходятся на 58 самопальных (`6.1.2` — 82,
    `1.1` — 21, `2.0.0` — 19), и 51 факт не имеет ни одного кода из
    `savdel.py` CAT_MAP, то есть невидим для экспорта. Теперь код сверяется с
    деревом; несуществующий поднимается до легального предка (`1.1.0` -> `1.1.1`
    нельзя, поэтому до раздела: `2.0.0` -> `2`), и это логируется.

F5. `reply_to_msg_id` заполнен у 68 725 из 117 847 реплик (58.3 %) и не
    подавался модели вообще. Замер: 9 301 пара «ответ-родитель» (14.6 %)
    попадает в РАЗНЫЕ пачки, 3 712 разнесены дальше, чем спасает OVERLAP.
    Модель получала плоское окно из нескольких параллельных обсуждений и
    склеивала их в одну «статью». Теперь строка промпта — `MSG_x -> MSG_y`.

F6. Пример качества в промпте возвращался как факт. Замер: 23 факта начинаются
    дословно «Методика BOPT (Biologically Oriented Preparation Technique)»,
    16 содержат дословную фразу примера, 40 — «коррекции зенитов». Провенанс у
    них указывает на реплики, где про BOPT не было ни слова. Удалять не стал —
    BOPT реальная методика, и часть фактов законна; протечка логируется как
    WARNING с id фактов, решение за человеком.

F7. Полностью пустые реплики занимали слоты пачки. Замер: 10 531 реплика с
    пустым `text`, из них 10 136 имеют `vision_description` (полезны), а 395
    пусты целиком. Теперь пустые не попадают в промпт, но помечаются
    обработанными (иначе бесконечный цикл), и их число логируется.

F10. Защиты от дублей не было ни в коде, ни в схеме (единственный индекс —
    `idx_cat`). Точных дублей `content` сейчас 3 группы / 9 строк, по первым
    120 нормализованным символам — 10 групп / 24 строки; обрезанных на полуслове
    фактов 1 из 12 784. То есть база пока чистая, НО повторный прогон вставил бы
    вторые копии всех 12 784 фактов. Теперь дубли отсекаются по хешу
    нормализованного текста — и против уже лежащего в базе, и внутри прогона.
    Схема НЕ меняется: UNIQUE-индекс — это миграция боевой базы, она в патче
    ведущему, а не здесь.

F11. Пути к БД были относительными: запуск из любого каталога, кроме корня,
     создавал пустую фальшивую `stomat_wiki.db`. Теперь пути от `__file__`.

Удалено как мёртвое: `call_groq_llama` (синтез идёт через
`gemini_knowledge.generate_fact_json`), переменная `last_id`, импорты
`httpx`/`random`/`config`. Благодаря этому модуль ИМПОРТИРУЕТСЯ БЕЗ ПОБОЧНЫХ
ЭФФЕКТОВ: не читает `.env`, не печатает баннер config, не создаёт `distiller.log`.
Логирование поднимается в `main()`.

НЕ ПОЧИНЕНО ЗДЕСЬ (нужна правка чужих файлов, см. отчёт ведущему):
  * восстановление `source_ids` у 353 уже лежащих в базе фактов — это UPDATE
    боевой БД;
  * `UNIQUE`-индекс по хешу `content` и колонка `category_code_prev` — миграция;
  * `media_links` пуста у 12 784 из 12 784: промпт не запрашивает поле `m`, а
    `media_remote_url` выбирается и не используется. Иллюстрации восстановимы
    только джойном по `source_ids`;
  * `confidence` — константа 10 у 12 731 факта; поле читают `savdel.py` и
    `filemake.py`, менять его семантику в одностороннем порядке нельзя;
  * `is_case = 1` у 10 862 из 12 784 (85 %) — флаг перестал различать что-либо.

ВАЖНО ПРО ПОВТОРНЫЙ ПРОГОН: правки ниже влияют ТОЛЬКО на будущие пачки.
Существующие 12 784 факта они не трогают и не требуют перепрогона, чтобы быть
верными. Перепрогон нужен, чтобы вернуть те 59 % архива — это отдельное
решение человека, здесь оно не запускается.
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime

import aiosqlite

# === КОНФИГУРАЦИЯ ===
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Пути абсолютные (F11): относительное имя приводило к тому, что запуск из
# другого каталога создавал пустую фальшивую stomat_wiki.db, init_wiki_db()
# создавал в ней таблицу, и падение приходило только на запросе к архиву.
ARCHIVE_DB = os.path.join(BASE_DIR, "stomat_archive.db")
WIKI_DB = os.path.join(BASE_DIR, "stomat_wiki.db")
LOG_PATH = os.environ.get("STOMCHAT_DISTILLER_LOG") or os.path.join(BASE_DIR, "distiller.log")

BATCH_SIZE = 80
OVERLAP = 8  # Нахлёст: последние OVERLAP реплик пачки не помечаются и попадают в следующую.

# Сколько раз повторить пачку, по которой модель не ответила или ответила
# мусором, прежде чем признать её потерянной. Раньше повтора не было вообще:
# первый же отказ транспорта выжигал 72 реплики навсегда.
MAX_BATCH_ATTEMPTS = 3

# Границы. Обе логируют срабатывание — молчаливая обрезка запрещена.
# Замер по архиву: самая длинная реплика 4 072 символа, самое длинное
# vision_description 1 276; на пачку из 80 реплик приходится p50 = 7 854,
# p99 = 15 194, максимум 20 961 символ. То есть обе границы взяты с запасом
# больше двукратного и в норме не срабатывают вообще.
MSG_CHAR_CAP = 4200        # на одну реплику в промпте
PROMPT_CHAR_BUDGET = 48000  # на весь лог сообщений в промпте
CONTENT_CHAR_CAP = 6000     # на текст одного факта; максимум в базе 5 477

# === ДЕРЕВО ЗНАНИЙ 5.0 ===
# Раньше здесь стояло дерево 4.0, и это была не просто «четвёртая копия
# таксономии», а копия с ДРУГИМИ ЗНАЧЕНИЯМИ У ТЕХ ЖЕ КОДОВ. Замер: из 34
# кодов, общих для дерева 4.0 и `savdel.py` CAT_MAP (по которому раскладывается
# экспорт), 14 означают другую клиническую тему:
#
#   код     дерево 4.0 (что видела модель)   savdel.py (куда попадёт файл)
#   3.3.1   Синус-лифтинг                ->  Пластика_Десны
#   6.1.1   Анализ КЛКТ/ОПТГ             ->  Оборудование_Оптика
#   5.3.1   Оптика/Микроскопы            ->  Цифра_3D_Печать
#   1.3.2   Отбеливание/Чувствительность ->  Пародонтология_SRP
#   2.2.3   Фиксация                     ->  Орто_Оттиски
#   3.1.2   Пародонтология/Пластика десны->  Хирургия_Апикальная
#   3.2.3   Осложнения/Периимплантит     ->  Имплантация_Компоненты
#   2.1.2   Накладки/Вкладки             ->  Орто_Коронки
#   2.1.4   Съемное протезирование       ->  Орто_Микропротезирование
#   2.2.2   Ретракция/Оттиски            ->  Орто_Техника_Уступ
#   2.3.2   Сплинт-терапия/Каппы         ->  Гнатология_ВНЧС
#   1.2.2   Морфология/Бугры/Фронт       ->  Реставрация_Спиртовой_протокол
#   1.2.3   Инструментарий/Матрицы       ->  Реставрация_Морфология
#   1.2.4   Полировка/Финиш              ->  Реставрация_Матрицы
#
# То есть статья про синус-лифтинг ложилась в файл про пластику десны, а статья
# про КЛКТ — в файл про микроскопы. Врач, открывший рубрику, читает не то, что
# в ней должно быть. Держалось это только на том, что reclass.py прогоняли
# вторым проходом и он переразмечал всё по 5.0 (сейчас is_reclassified=1 у
# 12 784 из 12 784). Первый же прогон без reclass дал бы порчу.
#
# Здесь теперь дерево 5.0 — ровно то, по которому размечает reclass.py и по
# которому раскладывает экспорт savdel.py CAT_MAP (52 L3-кода, сверено
# код-в-код). Побочный выигрыш: 18 листьев (`1.1.6, 1.2.5, 1.2.6, 1.3.3,
# 2.2.4, 2.2.5, 2.2.6, 2.3.3, 2.4.1, 2.4.2, 2.4.3, 3.1.3, 3.2.4, 4.1.3,
# 6.3.1, 7.1.1, 7.2.1, 7.3.1`), которые до сих пор были физически недостижимы
# на первом проходе, стали доступны — раздел «Фиксация цементами» и весь
# менеджмент больше не пустуют по построению.
#
# Разделы 8-10 в дереве 5.0 отсутствуют, и из-за этого reclass.py переклеил
# 80 фактов детской стоматологии и 35 по материаловедению в разделы 1-7 — как
# классы они исчезли. Оставляю их здесь ЯВНЫМИ листьями, чтобы не терять класс
# повторно; в CAT_MAP их нет, поэтому normalize_category логирует, что такой
# факт не экспортируется, и в патче ведущему стоит пункт добавить их в CAT_MAP.
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
8. ДЕТСКАЯ СТОМАТОЛОГИЯ:
   8.1.1 Прием детей (Коронки/Седация/Молочные зубы).
9. МАТЕРИАЛОВЕДЕНИЕ:
   9.1.1 Обзоры брендов и честные отзывы о материалах.
10. ПРОЧЕЕ:
   10.1.1 Не относится к клинике (кулуары/организационное).
"""

# Легальные коды выводятся ИЗ ТЕКСТА ДЕРЕВА, а не переписываются вторым
# списком: второй список — это пятая копия таксономии, которая разойдётся так
# же, как разошлись четыре существующие.
LEGAL_CODES = frozenset(re.findall(r"\b(\d{1,2}(?:\.\d+){1,2})\b", KNOWLEDGE_TREE))
LEGAL_SECTIONS = frozenset(re.findall(r"(?m)^(\d{1,2})\.\s", KNOWLEDGE_TREE))
# Экспорт `savdel.py` разложен по L3-листам: код уровня L2 («1.1») в CAT_MAP не
# входит, и факт с ним не попадёт ни в один файл. Замер: 51 факт в базе уже
# невидим для экспорта именно по этой причине.
LEAF_CODES = frozenset(c for c in LEGAL_CODES if c.count(".") == 2)
# Родитель -> его листья. Нужно, чтобы поднять несуществующий код до
# ЕДИНСТВЕННОГО возможного листа, не выдумывая специфичности: `6.1.2` (82
# присвоения в базе) однозначно поднимается до `6.1.1`, потому что других
# листьев в разделе Рентгенология нет.
LEAF_CHILDREN = {}
for _c in sorted(LEAF_CODES):
    LEAF_CHILDREN.setdefault(_c.rsplit(".", 1)[0], []).append(_c)
FALLBACK_CODE = "10.1.1"
# Листья, которых нет в `savdel.py` CAT_MAP: факт с таким кодом синтезируется и
# сохраняется, но ни в один файл экспорта не попадёт. Держим списком, чтобы
# логировать это явно, а не терять 51 факт молча, как сейчас в базе.
NON_EXPORTABLE_LEAVES = frozenset({"8.1.1", "9.1.1", "10.1.1"})

# Фрагмент примера качества из промпта. Если он вернулся в тексте факта —
# модель пересказала промпт, а не чат (F6). Замер по базе: 16 фактов.
PROMPT_EXAMPLE_MARKER = "позволяет добиться прироста мягких тканей"

logger = logging.getLogger(__name__)

_LOGGING_READY = False


def setup_logging():
    """
    Поднимает файловый + консольный лог. Вызывается из main(), НЕ при импорте.

    Раньше `logging.basicConfig(handlers=[FileHandler("distiller.log")])` стоял
    на уровне модуля, поэтому `import distiller` создавал файл лога в текущем
    каталоге. Модуль обязан импортироваться без побочных эффектов — иначе его
    нельзя ни протестировать, ни импортировать из другого инструмента.
    """
    global _LOGGING_READY
    if _LOGGING_READY:
        return
    handlers = [logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=handlers,
    )
    _LOGGING_READY = True


def say(text):
    """
    Печать, которая не может уронить прогон (F1).

    stdout на этой машине — cp1251. `print()` любого символа вне cp1251
    поднимает UnicodeEncodeError: проверено, `python -c "print('\\U0001F4A1')"`
    падает. Прежний код печатал эмодзи в семи местах, и самый первый print
    (счётчик прогресса) убивал сито ДО первой пачки. Плюс печать текста факта
    стояла внутри открытой транзакции вики, поэтому падение теряло всю пачку
    фактов без коммита.

    Замер, зачем это не «косметика»: 65 из 12 784 фактов содержат символы,
    непечатаемые в cp1251 (`₽`, `²`, `é`, `ö`), и 10 074 из 107 316 реплик
    архива (9.4 %) — тоже.
    """
    stream = None
    try:
        stream = sys.stdout
        enc = getattr(stream, "encoding", None) or "utf-8"
        stream.write(str(text).encode(enc, errors="replace").decode(enc, errors="replace") + "\n")
    except Exception:
        try:
            if stream is not None:
                stream.write(str(text).encode("ascii", errors="replace").decode("ascii") + "\n")
        except Exception:
            pass


def norm_content_key(text):
    """Ключ для отсева дублей: регистр, пробелы и пунктуация не считаются."""
    t = re.sub(r"[^\w\s]", " ", (text or "").lower(), flags=re.UNICODE)
    return " ".join(t.split())


def content_hash(text):
    return hashlib.sha1(norm_content_key(text).encode("utf-8")).hexdigest()


def clip_at_sentence(text, cap):
    """
    Обрезает текст по границе предложения, а не по середине слова.

    Возвращает (текст, сколько символов отброшено). Обрезка на полуслове —
    отдельный класс порчи: замер по базе показал 1 факт из 12 784, который
    кончается буквой без знака конца предложения ('...использование клиньев и
    матриц в стоматологии'), и восстановить его уже нечем. Поэтому режем по
    последнему `.`/`!`/`?`/`;`, а если такого в пределах разумного нет — по
    границе слова.
    """
    t = text or ""
    if len(t) <= cap:
        return t, 0
    head = t[:cap]
    cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "), head.rfind("; "))
    if cut < cap // 2:
        # Ни одной границы предложения во второй половине — режем по слову.
        cut = head.rfind(" ")
    if cut <= 0:
        cut = cap - 1
    else:
        cut += 1
    return t[:cut].rstrip(), len(t) - cut


def normalize_source_ids(raw_list, allowed_ids):
    """
    Приводит `s` из ответа модели к списку целых msg_id ЭТОЙ пачки (F3).

    Возвращает (ids, dropped) — dropped логируется вызывающим, молча не
    теряется ничего.

    Зачем: промпт подаёт строки как `MSG_12345`, а просит `s: [ID]`. Часть
    пачек модель возвращала ярлыком, и запись шла как есть. Замер по живой
    вики: 353 факта (2.76 %), у которых ВСЕ токены `source_ids` вида
    `MSG_...` — 2 073 битых токена, из них регуляркой восстанавливается
    1 985 уникальных id, 1 972 реально есть в архиве. `savdel.py:113`
    отбрасывает их фильтром `isdigit()`, поэтому у этих 353 фактов нет ни
    провенанса, ни блока `[ИЛЛЮСТРАЦИЯ]`.

    Второй слой — сверка с id пачки. Замер: 759 фактов (5.94 %) ссылаются на
    2 049 msg_id, которых в архиве нет; 8 токенов вообще семизначные
    (`1080108`, `1144522`), тогда как максимум архива — 139 701. Модель
    придумывает цифры, и без сверки провенанс лжёт.
    """
    ids, dropped = [], []
    seen = set()
    items = raw_list if isinstance(raw_list, (list, tuple, set)) else [raw_list]
    for item in items:
        if isinstance(item, bool):
            dropped.append(repr(item))
            continue
        if isinstance(item, int):
            found = [str(item)]
        else:
            found = re.findall(r"\d+", str(item))
        if not found:
            dropped.append(str(item))
            continue
        for token in found:
            value = int(token)
            if allowed_ids is not None and value not in allowed_ids:
                dropped.append(str(item) if len(found) == 1 else token)
                continue
            if value in seen:
                continue
            seen.add(value)
            ids.append(value)
    return ids, dropped


def normalize_category(code):
    """
    Сверяет код категории с деревом (F4). Возвращает (код, причина_подмены).

    Замер по живой вики: 110 различных кодов при 52 легальных; 393 присвоения
    приходятся на 58 самопальных (`6.1.2` — 82, `1.1` — 21, `2.0.0` — 19,
    `7.2.2` — 19); 51 факт не имеет НИ ОДНОГО кода из `savdel.py` CAT_MAP и
    потому невидим для экспорта — статья есть, но ни в один файл не попадёт.

    Несуществующий код не выбрасывается, а поднимается до легального предка, и
    если у предка ровно один лист — до этого листа: `6.1.2` -> `6.1.1`
    (в разделе Рентгенология других листьев нет, специфичность не выдумывается).
    Если листьев несколько, возвращается L2-код и в лог уходит предупреждение,
    что для экспорта `savdel.py` этого недостаточно.
    """
    raw = str(code or "").strip()
    if raw in LEAF_CODES:
        if raw in NON_EXPORTABLE_LEAVES:
            return raw, (f"код {raw} легален, но его нет в savdel.py CAT_MAP — "
                         f"факт сохранится, но ни в один файл экспорта не попадёт")
        return raw, None
    m = re.search(r"\d{1,2}(?:\.\d+)*", raw)
    if not m:
        return FALLBACK_CODE, f"код {raw!r} нечитаем -> {FALLBACK_CODE}"
    parts = m.group().split(".")
    while parts:
        candidate = ".".join(parts)
        leaves = LEAF_CHILDREN.get(candidate)
        if leaves and len(leaves) == 1:
            return leaves[0], f"код {raw!r} нет в дереве -> единственный лист {leaves[0]}"
        if candidate in LEGAL_CODES or candidate in LEGAL_SECTIONS:
            if candidate == raw:
                return candidate, (f"код {candidate!r} — уровень L2, экспорт savdel.py "
                                   f"разложен по L3, факт в файл не попадёт")
            return candidate, (f"код {raw!r} нет в дереве -> предок {candidate}; это не L3-лист, "
                               f"для экспорта savdel.py его недостаточно")
        parts.pop()
    return FALLBACK_CODE, f"код {raw!r} вне дерева -> {FALLBACK_CODE}"


def clinical_density(rows):
    """
    Сколько реплик пачки содержат клинический термин.

    Нужно, чтобы отличить «пачка была флудом, фактов честно нет» от «пачка была
    клинической, а модель промолчала». Без этого числа обе ситуации выглядят
    одинаково — именно так и потерялись 92 непрерывные серии по >= 60 реплик
    (7 439 реплик суммарно) с 9-10 тыс. символов текста каждая.

    Словарь берётся из `dental_vocab` — единственного источника истины по
    клинической лексике. Если модуль недоступен, возвращаем None, а не ноль:
    ноль соврал бы, что пачка была пустой.
    """
    try:
        import dental_vocab
    except Exception:
        return None
    n = 0
    try:
        for r in rows:
            text = " ".join(str(x) for x in (r[3], r[4]) if x)
            if dental_vocab.has_dental_term(text):
                n += 1
    except Exception as e:
        # Сломанный словарь не имеет права уронить ночной прогон: это
        # диагностический счётчик, а не часть синтеза.
        logger.error("clinical_density: словарь недоступен (%s), плотность неизвестна", e)
        return None
    return n


def build_prompt_log(messages):
    """
    Собирает блок сообщений для промпта. Возвращает (текст, статистика).

    Что изменилось:
      * `reply_to_msg_id` попал в строку как `MSG_x -> MSG_y` (F5). Замер:
        поле заполнено у 68 725 из 117 847 реплик (58.3 %) и не подавалось
        модели вообще; 9 301 пара «ответ-родитель» (14.6 %) лежит в разных
        пачках, 3 712 разнесены дальше, чем спасает OVERLAP = 8. Модель
        получала плоское окно из нескольких параллельных обсуждений и склеивала
        их в одну статью — отсюда факт про синус-лифтинг с меткой «брекеты».
      * полностью пустые реплики (нет ни text, ни vision_description) не
        занимают слот (F7). Замер: 395 таких.
      * границы MSG_CHAR_CAP и PROMPT_CHAR_BUDGET логируют всё, что отбросили.
    """
    stats = {"used": 0, "empty": 0, "clipped": 0, "clipped_chars": 0, "deferred": []}
    lines = []
    total = 0
    for i, m in enumerate(messages):
        msg_id, _date, name, text, vision, _url = m[0], m[1], m[2], m[3], m[4], m[5]
        reply_to = m[6] if len(m) > 6 else None
        body = (text or "").strip()
        img = f" [ИЗОБРАЖЕНИЕ: {vision}]" if vision else ""
        if not body and not img:
            stats["empty"] += 1
            continue
        body, dropped = clip_at_sentence(body, MSG_CHAR_CAP)
        if dropped:
            stats["clipped"] += 1
            stats["clipped_chars"] += dropped
            logger.warning("MSG_%s обрезан по границе предложения: отброшено %d симв.", msg_id, dropped)
        head = f"MSG_{msg_id}" + (f" -> MSG_{reply_to}" if reply_to else "")
        line = f"{head} | {name}: {body}{img}\n"
        if total + len(line) > PROMPT_CHAR_BUDGET and lines:
            stats["deferred"] = [x[0] for x in messages[i:]]
            break
        lines.append(line)
        total += len(line)
        stats["used"] += 1
    if stats["deferred"]:
        logger.warning(
            "Бюджет промпта %d симв. исчерпан: отложено %d реплик (msg_id %s..%s), "
            "они попадут в следующую пачку, не потеряны",
            PROMPT_CHAR_BUDGET, len(stats["deferred"]), stats["deferred"][0], stats["deferred"][-1],
        )
    return "".join(lines), stats


def clean_json_string(text):
    """Очищает строку от мусора нейронки (маркеры кода, пояснения) для парсинга JSON."""
    if not text:
        return ""
    text = re.sub(r"```json\s*", "", text)
    text = re.sub(r"```", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        return text[start:end + 1]
    return text


def _iter_json_objects(text):
    """
    Вытаскивает ВСЕ сбалансированные {...} на любой глубине, уважая строки.

    Именно «на любой глубине»: у обрезанного ответа внешний объект `{"facts": [`
    не закрывается никогда, поэтому поиск только верхнеуровневых пар не находит
    ничего, а целые объекты фактов внутри массива при этом есть.
    """
    stack = []
    in_str = False
    esc = False
    for i, ch in enumerate(text or ""):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            yield text[stack.pop():i + 1]


def parse_facts(raw_res):
    """
    Разбирает ответ модели. Возвращает (facts, status).

    status:
      "no_response" — модель не ответила (все ключи выдали 429/404 и
                      `gemini_knowledge.generate_fact_json` вернул None);
      "parse_error" — ответ есть, но JSON не собрался даже частично;
      "salvaged"    — полный JSON не собрался, но целые объекты фактов вынуты;
      "ok"          — разобрано штатно (в том числе честное `{"facts": []}`).

    Раньше все четыре случая возвращали `[]`, и вызывающий помечал пачку
    обработанной в любом из них. Именно этим 70 015 реплик (59.41 % архива)
    были выжжены без единого факта.

    Спасение частичного ответа не роскошь: `max_output_tokens = 8192` в
    `gemini_knowledge.py`, и на обрезанном ответе `rfind('}')` даёт невалидный
    JSON — терялась ВСЯ пачка фактов, хотя первые 8 объектов были целыми.
    (По `distiller.log` за прогон 2026-02-17..19 таких случаев 0, то есть
    механизм профилактический, а не задним числом.)
    """
    if not raw_res:
        return [], "no_response"
    # Сначала пробуем ответ как есть (только без маркдаун-обёртки): модель
    # может отдать верхнеуровневый СПИСОК `[{...}]`, а clean_json_string режет
    # от первой `{` до последней `}` и превращает такой список в один объект.
    fenced = re.sub(r"```(?:json)?\s*", "", str(raw_res)).strip()
    data = None
    for candidate in (fenced, clean_json_string(raw_res)):
        try:
            data = json.loads(candidate)
            break
        except Exception as e:
            err = e
    if data is None:
        logger.error("Провал парсинга JSON: %s | начало ответа: %s", err, str(raw_res)[:160])
        salvaged, seen = [], set()
        for chunk in _iter_json_objects(raw_res):
            try:
                obj = json.loads(chunk)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            if "facts" in obj and isinstance(obj["facts"], list):
                for x in obj["facts"]:
                    if isinstance(x, dict):
                        key = json.dumps(x, sort_keys=True, ensure_ascii=False)
                        if key not in seen:
                            seen.add(key)
                            salvaged.append(x)
            elif "f" in obj or "content" in obj:
                key = json.dumps(obj, sort_keys=True, ensure_ascii=False)
                if key not in seen:
                    seen.add(key)
                    salvaged.append(obj)
        if salvaged:
            logger.warning("Спасено %d фактов из битого JSON вместо потери всей пачки", len(salvaged))
            return salvaged, "salvaged"
        return [], "parse_error"
    if isinstance(data, list):
        facts = [x for x in data if isinstance(x, dict)]
        return facts, "ok"
    if not isinstance(data, dict):
        return [], "parse_error"
    facts = data.get("facts")
    if facts is None:
        # Модель могла назвать ключ иначе. Ищем первый список словарей.
        for value in data.values():
            if isinstance(value, list) and any(isinstance(x, dict) for x in value):
                facts = value
                break
    if facts is None:
        return [], "ok"  # честно пустой ответ без ключа facts
    if not isinstance(facts, list):
        return [], "parse_error"
    good = [x for x in facts if isinstance(x, dict)]
    if len(good) != len(facts):
        logger.warning("В facts пришло %d элементов не-словарей, они отброшены", len(facts) - len(good))
    return good, "ok"


async def init_wiki_db():
    """
    Создаёт таблицу, если её нет. Схему существующей базы НЕ меняет.

    ALTER TABLE / CREATE UNIQUE INDEX по боевой базе — миграция, и её место в
    патче ведущему, а не в рабочем инструменте, который человек запускает на
    ночь.
    """
    async with aiosqlite.connect(WIKI_DB) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS distilled_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_code TEXT,
                content TEXT,
                source_ids TEXT,
                media_links TEXT,
                is_case BOOLEAN,
                confidence INTEGER,
                processed_at TIMESTAMP
            )
        ''')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_cat ON distilled_facts(category_code)')
        await db.commit()


async def load_existing_hashes():
    """
    Хеши нормализованного текста всех фактов в базе — защита от удвоения (F10).

    У `distilled_facts` нет ни одного UNIQUE-индекса (единственный индекс —
    `idx_cat` по `category_code`). Если сбросить `is_processed_for_wiki = 0` и
    запустить сито заново — а именно это и надо сделать, чтобы вернуть 59 %
    архива — INSERT вставит вторые копии всех 12 784 фактов. Точных дублей
    `content` сейчас 3 группы / 9 строк, то есть база чистая, и терять эту
    чистоту на повторном прогоне нельзя.
    """
    hashes = set()
    async with aiosqlite.connect(f"file:{WIKI_DB}?mode=ro", uri=True) as db:
        async with db.execute("SELECT content FROM distilled_facts") as cur:
            async for (content,) in cur:
                hashes.add(content_hash(content))
    return hashes


async def process_batch(messages):
    """Синтез статей по пачке реплик. Возвращает (facts, status, stats)."""
    formatted_msgs, stats = build_prompt_log(messages)
    if not formatted_msgs.strip():
        logger.info("Пачка msg_id %s..%s: ни одной непустой реплики, вызов модели не нужен",
                    messages[0][0], messages[-1][0])
        return [], "ok", stats

    prompt = f"""
    Ты — редактор "Стоматологической Википедии". Твоя задача: переработать чат врачей в профессиональные медицинские статьи.

    === ТРЕБОВАНИЯ К ТЕКСТУ (ЖЕСТКО) ===
    1. НИКАКИХ ЦИТАТ: Запрещено писать "Врач сказал", "Он говорит". Пиши сухим техническим языком.
    2. НИКАКОГО ФЛУДА: Убирай мат, сленг ("засрал", "баба Зина") и личные мнения. Оставляй только суть метода.
    3. СИНТЕЗ: Объединяй диалог из 10 сообщений в ОДНУ глубокую статью-инструкцию.
    4. ТОЛЬКО КОДЫ: В поле "c" используй ТОЛЬКО существующие коды из дерева (например, 1.1.2 или 3.2.1). Создавать свои индексы (1.10.1 и т.д.) КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО.
    5. ТОЛЬКО ЧИСЛА В "s": перечисляй номера реплик БЕЗ префикса MSG_ — не "MSG_12345", а 12345. И только те номера, которые есть в логе ниже.
    6. НЕ ПЕРЕСКАЗЫВАЙ ЭТУ ИНСТРУКЦИЮ: пример ниже дан как образец СТИЛЯ. Если в логе про эту методику ничего нет — пример в ответ не переноси.

    === СТРУКТУРА ЛОГА ===
    Строка вида `MSG_100 -> MSG_98 | Имя: текст` означает, что реплика 100 является ОТВЕТОМ на реплику 98.
    Опирайся на эти связи: в окне идут несколько параллельных обсуждений, и склеивать их в одну статью нельзя.

    === ПРИМЕР КАЧЕСТВА (образец стиля, не факт) ===
    "Методика BOPT (Biologically Oriented Preparation Technique): позволяет добиться прироста мягких тканей и коррекции зенитов за счет создания вертикального уступа без финишной линии..."

    ЛОГ СООБЩЕНИЙ:
    {formatted_msgs}

    Выдай JSON (facts: [ {{c: "код_из_дерева", f: "ТЕКСТ_СТАТЬИ", s: [ID], case: bool}} ]).
    Если полезной информации нет — верни {{"facts": []}}.

    Дерево кодов: {KNOWLEDGE_TREE}
    """

    import gemini_knowledge  # ленивый импорт: держит модуль импортируемым без .env
    loop = asyncio.get_running_loop()
    raw_res = await loop.run_in_executor(None, gemini_knowledge.generate_fact_json, prompt)
    facts, status = parse_facts(raw_res)
    return facts, status, stats


def prepare_fact(f, allowed_ids, known_hashes):
    """
    Готовит одну запись к вставке. Возвращает (row, notes) или (None, notes).

    Здесь собраны все проверки, каждая из которых раньше отсутствовала:
    нормализация провенанса (F3), сверка кода (F4), защита от дублей (F10),
    обрезка текста по границе предложения, детектор протечки промпта (F6).
    """
    notes = []
    content = str(f.get("f") or f.get("content") or "").strip()
    if not content:
        return None, ["факт без текста отброшен"]

    content, dropped_chars = clip_at_sentence(content, CONTENT_CHAR_CAP)
    if dropped_chars:
        notes.append(f"текст факта обрезан по границе предложения: -{dropped_chars} симв.")

    code, why = normalize_category(f.get("c"))
    if why:
        notes.append(why)

    ids, dropped_ids = normalize_source_ids(f.get("s", []), allowed_ids)
    if dropped_ids:
        notes.append(f"отброшено ссылок провенанса {len(dropped_ids)}: {dropped_ids[:8]}")
    if not ids:
        notes.append("провенанс пуст: ни одна ссылка не совпала с msg_id пачки")

    if PROMPT_EXAMPLE_MARKER in content:
        notes.append("ПРОТЕЧКА ПРОМПТА: в тексте дословная фраза примера качества")

    h = content_hash(content)
    if h in known_hashes:
        return None, notes + ["дубль по нормализованному тексту, не вставлен"]
    known_hashes.add(h)

    row = (
        code,
        content,
        ",".join(str(x) for x in ids),
        "",  # media_links: промпт поля `m` не запрашивает, колонка пуста у 12784/12784
        bool(f.get("case", False)),
        10,  # confidence: константа; поле читают savdel.py и filemake.py, семантику не меняю
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    return row, notes


async def fetch_batch():
    """Читает очередную пачку кандидатов. `reply_to_msg_id` добавлен в SELECT (F5)."""
    async with aiosqlite.connect(ARCHIVE_DB, timeout=30) as db:
        cursor = await db.execute('''
            SELECT msg_id, date, sender_name, text, vision_description, media_remote_url, reply_to_msg_id
            FROM archive_messages
            WHERE is_processed_for_wiki = 0
            AND (has_media = 0 OR vision_processed = 1)
            ORDER BY msg_id ASC
            LIMIT ?
        ''', (BATCH_SIZE,))
        return await cursor.fetchall()


async def mark_processed(ids):
    async with aiosqlite.connect(ARCHIVE_DB, timeout=30) as db:
        await db.executemany(
            'UPDATE archive_messages SET is_processed_for_wiki = 1 WHERE msg_id = ?',
            [(m_id,) for m_id in ids],
        )
        await db.commit()


async def main():
    setup_logging()
    await init_wiki_db()
    known_hashes = await load_existing_hashes()
    logger.info("Загружено хешей уже существующих фактов: %d (защита от удвоения базы)", len(known_hashes))

    async with aiosqlite.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True) as db:
        async with db.execute(
            'SELECT COUNT(*) FROM archive_messages WHERE is_processed_for_wiki = 0 '
            'AND (has_media = 0 OR vision_processed = 1)'
        ) as cursor:
            row = await cursor.fetchone()
            total_todo = row[0] if row else 0
        async with db.execute(
            'SELECT COUNT(*) FROM archive_messages WHERE is_processed_for_wiki = 0 '
            'AND has_media = 1 AND vision_processed = 0'
        ) as cursor:
            row = await cursor.fetchone()
            locked = row[0] if row else 0

    say(f"[СИТО] Кандидатов к обработке: {total_todo}")
    if locked:
        # Раньше про это не говорилось вообще: цикл упирался в 0 кандидатов и
        # печатал «Архив полностью обработан», хотя 444 реплики с медиа без
        # Vision не были ни обработаны, ни помечены.
        say(f"[СИТО] Заперто до Vision (has_media=1, vision_processed=0): {locked} реплик — сито их не увидит")
        logger.warning("Вне очереди дистилляции: %d реплик с медиа без vision_processed", locked)

    marked_total = 0
    facts_total = 0
    empty_batches = 0
    lost_batches = []
    attempts = 0
    prev_first_id = None

    while True:
        rows = await fetch_batch()
        if not rows:
            break

        first_id, last_id = rows[0][0], rows[-1][0]
        attempts = attempts + 1 if first_id == prev_first_id else 1
        prev_first_id = first_id

        pct = (marked_total / total_todo * 100) if total_todo else 100.0
        say(f"[СИТО] {pct:.2f}% ({marked_total}/{total_todo}) | пачка msg_id {first_id}..{last_id} ({len(rows)} шт.)")

        facts, status, stats = await process_batch(rows)

        if status in ("no_response", "parse_error"):
            # Ключевая правка (F2): отказ — НЕ повод выжигать реплики.
            if attempts < MAX_BATCH_ATTEMPTS:
                logger.warning(
                    "Пачка msg_id %s..%s: статус %s, попытка %d/%d — НЕ помечаю обработанной, повторю",
                    first_id, last_id, status, attempts, MAX_BATCH_ATTEMPTS,
                )
                say(f"[СИТО] отказ ({status}), попытка {attempts}/{MAX_BATCH_ATTEMPTS}, пачка сохранена в очереди")
                await asyncio.sleep(15)
                continue
            logger.error(
                "ПОТЕРЯ: пачка msg_id %s..%s не поддалась %d раз (%s). Помечаю, чтобы не встать навсегда. "
                "Реплики: %s", first_id, last_id, MAX_BATCH_ATTEMPTS, status,
                [r[0] for r in rows[:len(rows) - OVERLAP if len(rows) == BATCH_SIZE else len(rows)]],
            )
            lost_batches.append((first_id, last_id, status))

        prepared = []
        # Множество id ПАЧКИ — единственный допустимый провенанс: 2 049 ссылок в
        # базе ведут на msg_id, которых в архиве нет вообще.
        allowed = {r[0] for r in rows}
        for f in facts:
            row, notes = prepare_fact(f, allowed, known_hashes)
            for n in notes:
                logger.warning("Пачка %s..%s: %s", first_id, last_id, n)
            if row:
                prepared.append(row)

        if prepared:
            # Печать ВНЕ транзакции (F1): раньше `print` эмодзи и текста факта
            # стоял внутри `async with wiki` до commit(), и один непечатаемый
            # символ терял всю пачку фактов без следа.
            for row in prepared:
                say(f"  [{row[0]}] {row[1][:200]}")
            async with aiosqlite.connect(WIKI_DB, timeout=30) as wiki:
                await wiki.executemany('''
                    INSERT INTO distilled_facts
                        (category_code, content, source_ids, media_links, is_case, confidence, processed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', prepared)
                await wiki.commit()
            facts_total += len(prepared)
            say(f"[СИТО] сохранено фактов: {len(prepared)} из {len(facts)} присланных")
        elif status == "ok":
            empty_batches += 1
            dens = clinical_density(rows)
            # Молчаливое «пусто» и было главной дырой: 70 015 реплик (59.41 %)
            # выжжено без единого факта, среди них 3 974 длиннее 150 символов и
            # 92 непрерывные серии по >= 60 реплик. Теперь каждая пустая пачка
            # оставляет в логе диапазон msg_id и клиническую плотность.
            logger.warning(
                "ПУСТАЯ ПАЧКА msg_id %s..%s: реплик %d, в промпт ушло %d, пустых %d, "
                "клинических по dental_vocab: %s — фактов ноль",
                first_id, last_id, len(rows), stats["used"], stats["empty"],
                "нет данных" if dens is None else dens,
            )
            say(f"[СИТО] пусто (клинических реплик в пачке: {dens})")

        # Пометка обработанными. Нахлёст: последние OVERLAP реплик остаются в
        # очереди и попадут в следующую пачку. max(1, ...) гарантирует прогресс —
        # иначе бюджет промпта мог оставить пачку короче нахлёста и цикл встал бы.
        usable = len(rows) - len(stats["deferred"])
        if len(rows) == BATCH_SIZE or stats["deferred"]:
            mark_count = max(1, usable - OVERLAP)
        else:
            mark_count = usable
        ids_to_mark = [r[0] for r in rows[:mark_count]]
        await mark_processed(ids_to_mark)
        marked_total += len(ids_to_mark)

        say("[СИТО] пауза 5 сек")
        await asyncio.sleep(5)

    say("[СИТО] Очередь кандидатов пуста.")
    logger.info(
        "ИТОГ: помечено %d реплик, сохранено %d фактов, пустых пачек %d, потерянных пачек %d",
        marked_total, facts_total, empty_batches, len(lost_batches),
    )
    if empty_batches:
        # Честная формулировка вместо прежнего «Архив полностью обработан!»:
        # очередь пуста не значит, что знание извлечено.
        say(f"[СИТО] ВНИМАНИЕ: {empty_batches} пачек не дали ни одного факта — см. distiller.log, "
            f"строки 'ПУСТАЯ ПАЧКА'. Очередь пуста, но это НЕ значит, что архив дистиллирован.")
    for first_id, last_id, status in lost_batches:
        say(f"[СИТО] ПОТЕРЯНО: msg_id {first_id}..{last_id} ({status})")


if __name__ == '__main__':
    asyncio.run(main())
