# Recon: производительность на живых данных

Область: `database.py`, `assistant.py` (запросы к базам), `summarizer.py`.
Все замеры — на КОПИЯХ баз в `%TEMP%\recon_perf\`: `stomat_bot.db` (32 883 сообщения),
`stomat_wiki.db` (12 784 факта), `stomat_archive.db` (117 847 реплик). Правки не вносились.
Время — лучшее из 3-5 прогонов на прогретом кеше страниц, если не сказано иное.

Оговорка по снимку: `stomat_bot.db` устарел (последнее сообщение 2026-06-21), и в нём
физически нет `pm_messages`, `bot_sent_messages`, `clinical_bookmarks`, `user_profiles`,
`idx_reply_to` — их создаёт `init_db` на боевой машине. Числа по `messages` — про масштаб
и план запроса, не про поведение боевого бота. Где таблицы в снимке нет, замер
синтетический и это помечено.

---

## 1. `PRAGMA journal_mode = WAL` на КАЖДОМ открытии соединения — 20× накладных на любой запрос

**Место:** `database.py:20` (внутри `_connect()`, а его вызывает `_connection()` — то есть
каждая из 30 функций модуля), плюс `assistant.py:856` в `query_db_async`.

`journal_mode` — персистентный параметр ФАЙЛА базы, а не соединения. Он уже записан:
`PRAGMA journal_mode` на копии возвращает `wal`. Устанавливать его заново на каждом
`connect` — чистая потеря: SQLite при смене режима берёт эксклюзивную блокировку и трогает
заголовок.

Замер (60 прогонов, `stomat_bot.db`, среднее / лучшее):

| что делаем | лучшее | среднее |
|---|---|---|
| `sqlite3.connect` + `close` | 0.219 мс | 0.445 мс |
| + `PRAGMA busy_timeout` | 0.200 мс | **0.249 мс** |
| + `PRAGMA journal_mode = WAL` | 1.479 мс | 3.448 мс |
| оба, как в коде (`database.py:18-21`) | 1.600 мс | **5.026 мс** |

Сценарий отказа: `save_message` вызывается на КАЖДОЕ входящее сообщение чата, и
`_DB_EXECUTOR` (`database.py:12`) создан с `max_workers=1` — все обращения к базе
сериализованы в одном потоке. То есть 5 мс впустую на каждую запись, каждое чтение
контекста, каждый профиль, каждый вызов из планировщика; в очереди единственного потока
они складываются. Полная эмуляция `remove_bot_sent_message`: 3.55 мс на вызов, из которых
полезной работы — микросекунды.

Минимальная правка: убрать строку `db.execute("PRAGMA journal_mode = WAL")` из `_connect()`
(и из `query_db_async`), выставив WAL один раз в `init_db`.

---

## 2. Синхронный sqlite прямо в цикле событий — 3 места, до 29 мс блокировки за запрос

`assistant.py` в трёх местах открывает sqlite и выполняет полный скан БЕЗ
`run_in_executor` / `_run_db`. Соседние функции того же файла executor используют
(`search_knowledge_corpus`:848, `wiki_subtopic_counts`:3939, `query_wiki_fact_page`:4009) —
то есть это не стиль модуля, а три пропущенных места.

| место | запрос | план | замер |
|---|---|---|---|
| `assistant.py:2843-2864` — обработчик `/search` | `SELECT category_code, content FROM distilled_facts WHERE content LIKE '%kw%' LIMIT 5`, **в цикле по ключевым словам** | `SCAN distilled_facts` | 0.13-0.15 мс если 5 строк нашлись рано; **28.9 мс** когда слова в вике нет — LIMIT не срабатывает, скан идёт до конца |
| `assistant.py:4086-4092` `query_random_wiki_fact` (кнопка «🎲 Ещё факт», :4234) | `SELECT content FROM distilled_facts ORDER BY RANDOM() LIMIT 1` | `SCAN distilled_facts` + `USE TEMP B-TREE FOR ORDER BY` | **22.8 мс** на каждое нажатие |
| `assistant.py:4037-4075` `query_wiki_subtopic` | `category_code LIKE '%code%'` в цикле по кодам, затем fallback `content LIKE '%kw%' LIMIT 10` в цикле по словам | `SCAN distilled_facts` | 0.38 мс на код, **4.49 мс** на слово fallback, ×(до 3 кодов + до 5 слов) |

Сценарий отказа: `/search` берёт ключи через `extract_keywords` (`assistant.py:2839`) —
БЕЗ ограничителя `select_search_keywords`, который применяют все остальные вызовы
(:1459, :1777, :2166, :3139). Число слов равно числу значимых слов в запросе врача.
Фраза из 10 неспецифичных слов, которых в вике нет → 10 × 28.9 мс = 289 мс, в течение
которых цикл событий не может обработать НИЧЕГО: ни входящие сообщения основного чата,
ни ответы в других диалогах, ни таймеры планировщика. `query_random_wiki_fact` даёт
фиксированные 22.8 мс на каждое нажатие кнопки, и жать её можно подряд.

Минимальная правка: обернуть тело каждого из трёх мест в
`await loop.run_in_executor(None, sync_fn)` по образцу `assistant.py:848`, и пропустить
ключи `/search` через `select_search_keywords`.

---

## 3. `get_active_pm_users` — 794 мс, и индекс сам по себе НЕ помогает (нужен `INDEXED BY`)

**Место:** `database.py:997-1009`, вызывается из почасовой джобы
`assistant.py:5033` (`check_group_activity` → пинги в ЛС).

```sql
SELECT DISTINCT user_id FROM pm_messages WHERE date >= datetime('now', ?)
```

`pm_messages` имеет только `idx_pm_user(user_id, id)` (`database.py:260`), индекса по
`date` нет. `DISTINCT user_id` заставляет планировщик выбрать `idx_pm_user` — и тогда для
КАЖДОЙ строки индекса приходится идти в таблицу за колонкой `date`.

Замер — синтетическая таблица боевой формы (в снимке `pm_messages` нет): 120 000 строк,
400 пользователей, даты размазаны по 400 дням, 8 999 строк попадают в окно 30 дней:

| вариант | план | время |
|---|---|---|
| как в коде | `SCAN pm_messages USING INDEX idx_pm_user` | **794 мс** |
| `GROUP BY` вместо `DISTINCT` | `SCAN pm_messages USING INDEX idx_pm_user` | 728 мс |
| + `CREATE INDEX idx_pm_date ON pm_messages(date, user_id)` + `ANALYZE` | план НЕ изменился | 968 мс |
| + тот же индекс и `INDEXED BY idx_pm_date` | `SEARCH pm_messages USING COVERING INDEX idx_pm_date (date>?)` \| `USE TEMP B-TREE FOR DISTINCT` | **5.79 мс** (137×) |

Сценарий отказа: `pm_messages` не чистится и растёт бессрочно — это прямо
задокументировано в `database.py:256-259`. Джоба почасовая, поток базы один
(`max_workers=1`), поэтому раз в час вся работа с базой встаёт почти на секунду при
120 тыс. строк и линейно дольше дальше. Ключевая деталь: одного `CREATE INDEX` НЕ
достаточно — планировщик даже после `ANALYZE` продолжает выбирать `idx_pm_user`.

Минимальная правка: добавить `idx_pm_date(date, user_id)` в `init_db` И указать
`FROM pm_messages INDEXED BY idx_pm_date` в самом запросе.

---

## 4. RAG-поиск по архиву: LIKE-скан 117 847 реплик на каждое ключевое слово, до 12 слов на вопрос

**Место:** `assistant.py:824-830` (`search_knowledge_corpus`) — горячий путь, выполняется
на каждый сработавший вопрос (`:1459`, `:1777`, `:2166`, `:3139`, `:3540`).

`archive_messages` не имеет НИ ОДНОГО индекса (`sqlite_master`: только сама таблица), план
— `SCAN archive_messages`. Условие `text LIKE '%kw%' AND LENGTH(TRIM(text)) >= 40 AND
TRIM(text) NOT LIKE '%?'` индексом непокрываемо в принципе (ведущий wildcard + функции от
колонки), так что речь не о пропущенном индексе, а о неограниченной цене.

Замер, `LIMIT 48` (бюджет `_rows_per_keyword` при одном ключе):

| слово | найдено | время |
|---|---|---|
| `циркон` | 48 | 1.65 мс |
| `гипохлорит` | 48 | 5.78 мс |
| `bopt` | 48 | 49.45 мс |
| `вертипреп` | 26 (меньше LIMIT) | **158.0 мс** |
| отсутствующее слово | 0 | **140.6 мс** |

LIMIT спасает только при частых совпадениях. Реалистичный вопрос с 12 профильными
терминами (`_MAX_SEARCH_KEYWORDS = 12`, `LIMIT 8` на ключ) — прогоны 1/2/3 подряд:
**2438 мс / 1034 мс / 877 мс** только на архив, плюс 175 мс на вике. Итого 1.0-2.6 с на
один вопрос. Кандидатов при этом набралось 67, то есть `_CORPUS_CANDIDATE_CAP = 60`
досрочно цикл почти не обрывает — на редких словах он проходит все 12.

Цикл событий это не блокирует (`run_in_executor`, :848), но целиком ложится в задержку
ответа врачу, и `run_in_executor(None, ...)` занимает поток общего пула на всё это время.

Минимальная правка: FTS5-таблица по `archive_messages.text`
(`CREATE VIRTUAL TABLE ... USING fts5(text, content='archive_messages')`) и `MATCH` вместо
`LIKE`; без миграции — кеш результатов по ключевому слову на время жизни процесса.

---

## 5. `query_wiki_fact_page` — два полных скана с двумя TEMP B-TREE на каждое нажатие кнопки; `COUNT(*)` пересчитывается всегда

**Место:** `assistant.py:3990-4003`. На каждое листание энциклопедии выполняется
`COUNT(*)` над `GROUP BY content` и затем сама строка по `OFFSET`.

Замер, подтема из 3 кодов, `total = 4919`:

| запрос | план | время |
|---|---|---|
| `SELECT COUNT(*) FROM (SELECT 1 ... GROUP BY content)` | `CO-ROUTINE` \| `SCAN distilled_facts` \| `USE TEMP B-TREE FOR GROUP BY` \| `SCAN (subquery-1)` | **19.2 мс** |
| `SELECT content, MIN(id) ... GROUP BY content ORDER BY ord LIMIT 1 OFFSET 0` | `SCAN distilled_facts` \| `USE TEMP B-TREE FOR GROUP BY` \| `USE TEMP B-TREE FOR ORDER BY` | 20.9 мс |
| то же, `OFFSET 1000` | тот же | 22.5 мс |
| то же, `OFFSET 4000` | тот же | **43.3 мс** |

То есть 40 мс на первой странице и до 62 мс на глубокой — при том, что докстринг на
`assistant.py:3975-3977` утверждает «Листание в SQL стоит 4 мс на самом крупном разделе».
Замер этого не подтверждает: разница на порядок.

Повторный одинаковый запрос: `total` не меняется НИКОГДА между нажатиями — вика статична,
её пересобирает офлайновый дистиллятор, и это прямо сказано в докстринге
`wiki_subtopic_counts` (`assistant.py:3908-3910`), где числа по подтемам уже кешируются в
`_WIKI_COUNT_CACHE`. Здесь такого же кеша нет, и `COUNT(*)` со сканом и temp b-tree
выполняется на каждое нажатие «вперёд/назад».

Минимальная правка: кешировать `total` по `subtopic_id` в словаре рядом с
`_WIKI_COUNT_CACHE` и считать его только при промахе.

---

## 6. `/wipe` — N соединений и N транзакций вместо одной

**Место:** `assistant.py:2529-2530` — `for m_id in msg_ids: await
database.remove_bot_sent_message(m_id)`. Каждый вызов проходит полный
`_connection()` → новый `sqlite3.connect`, два PRAGMA, отдельная транзакция.

Замер (синтетическая `bot_sent_messages` боевой формы, 20 000 строк, индекс
`idx_bot_sent_chat` на месте):

| как | время |
|---|---|
| 50 × полный `_connection()` + одиночный `DELETE` | 3.55 мс на вызов → **177 мс** |
| одно соединение + `executemany` на те же 50 удалений | **0.26 мс** |

680× на пустяковой операции; для админа это заметная пауза в чате, и все 177 мс держат
единственный поток `_DB_EXECUTOR`, то есть параллельно ничего не сохраняется.

Минимальная правка: добавить в `database.py` функцию, делающую `executemany`
`DELETE ... WHERE msg_id = ? AND chat_id = ?` по списку, и вызвать её один раз (`chat_id`
здесь известен — `c_id` из `by_chat`, а сейчас он не передаётся вообще, вопреки
`database.py:888-895`).

---

## Опровергнутые гипотезы (проверял, находкой НЕ является)

- **`category_code LIKE '%code%'` заменить на префикс `'code%'` — НЕЛЬЗЯ, сломает выдачу.**
  Гипотеза была: ведущий `%` обесценивает существующий `idx_cat`, и правка на префикс даст
  `SEARCH ... USING INDEX idx_cat`. Индекс действительно начинает работать
  (`GLOB '2.1.1*'` → `MULTI-INDEX OR`, 10.43 мс → 2.38 мс), но **семантика другая**:
  12 667 из 12 784 строк хранят в `category_code` СПИСОК кодов через запятую
  (`'1.1.1, 2.1.1, 1.2.1'`, distinct-значений 6 711). `LIKE '%2.1.1%'` находит 1 426
  фактов, `LIKE '2.1.1%'` — 243. Правка «на индекс» отняла бы у врача 83% статей раздела.
  Настоящее решение — нормализовать коды в отдельную таблицу связи, это не одна строка.
- **`get_unsummarized_count` (`database.py:467`), `get_messages_for_summary` (:476),
  `get_messages_for_period` (:692) — мёртвый код, боевой цены нет.** Полный скан у них
  реальный: `get_messages_for_summary` → `SCAN messages USING INDEX idx_date`, **67.8 мс**,
  6 637 строк и 678 КБ текста в память БЕЗ LIMIT; `get_unsummarized_count` →
  `SCAN messages` (индекс не используется вообще), 24.6 мс. Но вызовов из продакшена нет
  ни одного: `get_messages_for_summary` встречается только в `test_edit_delete.py:114,157`
  и `test_digest_window.py:96`, у двух других не встречается нигде. Индекс
  `idx_summarized` заводить не под что; сначала решить, нужны ли сами функции.
- **`assistant.py:1370` и `assistant.py:1415` — не повторный запрос в одном обработчике.**
  Запросы посимвольно идентичны (`WHERE msg_id = ? OR reply_to_msg_id = ? ORDER BY date
  ASC`), но ветки взаимоисключающие: :1369 внутри `if not triggered and reply_to_msg_id`,
  где тут же `triggered = True`; :1414 внутри `if not triggered`. За один вызов
  `check_and_trigger_assistant` выполняется ровно один из двух.
- **`idx_reply_to` — уже исправлено**, `database.py:143`. Отсутствие индекса в снимке —
  свойство устаревшего снимка. Для полноты, на снимке БЕЗ индекса:
  `COUNT(*) WHERE reply_to_msg_id = ?` → `SCAN messages`, 12.8 мс; ветка
  `msg_id = ? OR reply_to_msg_id = ?` → `SCAN messages USING INDEX idx_date`, 16.5 мс —
  совпадает с числами в комментарии `database.py:131-142`.
- **Оконные выборки по `date` индекс используют, LIMIT им не нужен.**
  `get_messages_for_range` (:336) и добор в `get_messages_for_daily_summary` (:316) —
  `SEARCH messages USING INDEX idx_date`, 0.03 и 1.02 мс. Объём ограничен окном времени,
  а не размером таблицы.
- **`get_pending_media_message_ids` (:676) — цена задокументирована и разовая.**
  `SCAN messages USING INDEX sqlite_autoindex_messages_1`, 0.27 мс пока очередь непуста
  (745 снимков без описания на снимке — сходится с докстрингом); 33.6 мс — случай пустой
  очереди, один раз за старт процесса.
- **`get_last_msg_id` (:494), `get_messages_from` (:359), `COUNT(*) WHERE msg_id > ?`
  (`assistant.py:1303`)** — покрывающий `sqlite_autoindex_messages_1`, 0.02-1.16 мс.
- **`get_last_n_messages` (:393)** — `SCAN messages USING INDEX idx_date` +
  `USE TEMP B-TREE FOR LAST TERM OF ORDER BY`, 3.58 мс на `LIMIT 300`. Сходится с
  докстрингом, отдельной находкой не является.
- **`_scan_topic_statistics` (`assistant.py:2062-2082`) — тяжёлый, но прикрытый.**
  `SELECT text FROM archive_messages` без LIMIT: `fetchall` 530 мс, 22.4 МиБ строк Python,
  плюс 3.9 МБ текста из `messages`; сверху 8 regex на 150 тыс. строк. Но код итерирует
  курсор, а не `fetchall`, работает в executor (:2094) и кешируется на 6 часов
  (`STATS_CACHE_TTL_SECONDS`, :2040). Улучшить можно (считать в SQL, кеш на диск), но
  как дефект производительности это не квалифицируется.
- **`summarizer.py` прямых обращений к sqlite не содержит вообще** — ни
  `sqlite3.connect`, ни `.execute`, ни `executemany`. Вся работа с базой идёт через
  `database.*`, то есть через `_run_db`. Синхронного sqlite в цикле событий там нет.
