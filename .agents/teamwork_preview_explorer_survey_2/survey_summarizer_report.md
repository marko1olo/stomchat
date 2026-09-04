# Технический отчет исследования: Архитектура `summarizer.py` и интеграция клинических профилей (R2)

**Дата и время**: 2026-09-04  
**Исследователь**: Explorer Survey 2 (Summarizer & Profile Integration)  
**Область аудита**: `summarizer.py`, `user_memory.py`, `database.py`, взаимодействие с LLM, бюджетирование контекста и безопасное мокирование.

---

## 1. Резюме (Executive Summary)

В ходе детального аудита кодовой базы StomChat выявлены ключевые архитектурные особенности и узкие места для реализации требования R2:
1. **Текущее состояние рубрики «ЭКСПЕРТ ДНЯ»**: В текущих промптах (`summarizer.py:716-718` для дня и `1156-1158` для недели) рубрика опирается **исключительно на сиюминутный текст чата** (кто дал совет, кому сказали «спасибо»). Профили врачей, их подтвержденный клинический опыт, специализация, арсенал оборудования и используемые протоколы в дайджесты сейчас **не передаются вовсе**.
2. **Идентификация авторов в выборке**: Таблица `messages` в БД содержит колонку `sender_id INTEGER`, однако запросы выборки сообщений для дайджестов (`get_messages_for_daily_summary` и `get_messages_for_range` в `database.py`) сейчас запрашивают только 8 колонок без `sender_id`. В `summarizer.py` распаковка сообщений жестко зафиксирована на 8 элементах (`m_id, name, username, text, m_desc, date, reply_id, m_url = msg`), что требует аккуратного расширения с сохранением обратной совместимости.
3. **Функция интеграции профилей**: В `user_memory.py` уже реализована функция `format_users_chunk_context(user_ids: List[int]) -> str`, формирующая компактный блок профилей авторов. Ее можно бесшовно подключить в `process_summary_batch` и `process_weekly_batch`.
4. **Критический скрытый риск бюджетирования (ЛОВУШКА РЕГРЕССИОННЫХ ТЕСТОВ)**:
   - В тестах `test_digest_formatting.py` (строки 228-230) и `test_fix_weekly.py` (строки 254-260) работают строгие регулярные выражения: `re.findall(r"(\d{4,5})\s*символ", prompt)`.
   - Если в текст промпта или инжектируемого блока поместить буквальную фразу вида `"не более 2000 символов"` или `"2000 символов"`, тесты упадут с ошибкой `в промпте найдено несколько цифр длины`!
   - Поэтому лимит `<= 2000 символов` обязан контролироваться **строго на уровне Python-кода** (`MAX_USERS_CONTEXT_CHARS = 2000` в `summarizer.py` и параметр `max_chars` в `format_users_chunk_context`), а в самом тексте промпта фразы `2000 символов` быть не должно.
5. **Текущий статус линтера ruff**:
   - `user_memory.py`: 0 ошибок.
   - `database.py`: 0 ошибок.
   - `summarizer.py`: 4 предсуществующие ошибки `E701 Multiple statements on one line (colon)` (строки 567, 972, 1248, 1307).
   - `assistant.py`: 21 предсуществующая ошибка (E402, E701, F841, E712, F401).

---

## 2. Конвейеры дайджестов в `summarizer.py` (Pipelines Analysis)

### 2.1. Ежедневный дайджест (Daily Digest)
- **Точка входа**: `summarizer.process_summary_batch(messages, client, chat_id, topic_id=None, msg_count=0, cached_message=None, delivery_hook=None)` (`summarizer.py:561`).
- **Сбор сообщений**:
  - Вызывается из `main.py:scheduler_task` (строки 649–720).
  - Срабатывает ежедневно при `now.hour >= config.REPORT_HOUR` (по умолчанию 10:00).
  - Окно сообщений: `start_time = daily_window_start(now, last_sent_date)` до `end_time = now`.
  - Запрос к БД: `database.get_messages_for_daily_summary(start_time, end_time, min_count=100)`.
- **Шаги конвейера внутри `process_summary_batch`**:
  1. *Быстрый путь (кэш)*: Если передан `cached_message`, отправляет готовый тизер без повторного вызова нейросети (для рассылки во второй/третий чат).
  2. *Фильтрация*: `filter_useful_messages(messages)` отсекает флуд, короткие приветствия и неинформативные реплики (`summarizer.py:524-559`).
  3. *Контекст ответов*: Извлекает `reply_ids`, подтягивает тексты родителей через `database.get_texts_by_ids` и подставляет краткие цитаты только для тех сообщений, чей родитель не попал в выборку дня (`summarizer.py:128-163`).
  4. *Бонусные блоки*: `select_bonus_blocks` отбирает от 1 до 3 блоков (`summarizer.py:476-498`) на основе триггерных слов в переписке дня (`BONUS_TRIGGERS`).
  5. *Сборка промпта*: Формирует многостраничное ТЗ, включающее рубрики 0–9 и лог переписки `{full_text}` (`summarizer.py:653-723`).
  6. *Генерация LLM*: `_generate_text_singleflight(prompt, "daily", chat_id, topic_id, len(messages), len(prompt))` с таймаутом `GEMINI_GENERATION_TIMEOUT_SECONDS = 2100` под `asyncio.Lock()` (`summarizer.py:270-293`).
  7. *Постобработка HTML*:
     - `clean_markdown_to_html(raw_summary)` (`html_safe.py`).
     - Подстановка разметки клинических снимков вместо плейсхолдеров `[IMG_{m_id}]`.
     - Добавление подвала `Сообщений за период — {msg_count}`.
     - Безопасная обрезка `_safe_truncate_html(final_html, max_len=11000 - len(footer)) + footer`.
  8. *Публикация и доставка*:
     - Если `< 1500` символов: прямая отправка в чат через `_send_message_once`.
     - Если `>= 1500` символов: создание страницы Telegraph через `create_telegraph_page_async(title, final_html, timeout=60)`, сборка привлекательного тизера со ссылкой (случайный хук из 17 вариантов, набор буллетов из 10 сетов, кнопка CTA из 5 вариантов) и отправка тизера в Telegram.
  9. *Закрепление*: `_pin_message_safely(client, chat_id, sent_msg.id)`.

### 2.2. Еженедельная газета (Weekly Digest)
- **Точка входа**: `summarizer.process_weekly_batch(messages, client, chat_id, topic_id=None, delivery_hook=None, cached_message=None)` (`summarizer.py:962`).
- **Сбор сообщений**:
  - Вызывается из `main.py:scheduler_task` (строки 749–800) по понедельникам (`now.weekday() == 0`, 10:00) либо в режиме догона.
  - Окно сообщений: последние 7 полных дней через `database.get_messages_for_range(start_weekly, end_weekly)`.
- **Шаги конвейера внутри `process_weekly_batch`**:
  1. *Быстрый путь (кэш)*: Позволяет избежать дублирования генераций и страниц Telegraph для нескольких целевых каналов.
  2. *Фильтрация и сборка лога*: В лог добавляются даты (`dt_str = date.strftime('%d.%m')`), префиксы ответов и описания фото/видео.
  3. *Промпт "MEDICAL JOURNALIST"*: Задает жесткую структуру лонгрида (Клиническая панорама, Материаловедение, Энциклопедия, Поле битвы, Бизнес и право, «## 🌟 ДОСКА ПОЧЕТА (ГЕРОИ НЕДЕЛИ)», Юмор).
  4. *Генерация LLM*: `_generate_text_singleflight(prompt, "weekly", ...)`.
  5. *Постобработка*: Преобразование в HTML, вставка фото, добавление подвала `Сообщений за неделю — {msg_count}`, обрезка до `WEEKLY_HTML_LIMIT = 9500`.
  6. *Публикация*: Создание Telegraph страницы `WEEKLY: Большая Стоматологическая Газета ({date_str})`. При сбое Telegraph — аварийная прямая отправка с обрезкой до 3900 символов. Отправка тизера и закрепление.

---

## 3. Идентификация участников и авторов (Author Identification)

### 3.1. Схема данных в базе (`database.py`)
Таблица `messages` (`database.py:131-145`):
```sql
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id INTEGER UNIQUE,
    reply_to_msg_id INTEGER,
    sender_id INTEGER,
    sender_name TEXT,
    sender_username TEXT,
    text TEXT,
    date TIMESTAMP,
    has_media BOOLEAN,
    media_type TEXT,
    media_description TEXT,
    media_remote_url TEXT,
    is_summarized BOOLEAN DEFAULT 0
)
```
При записи сообщений (`save_message`, строки 473–503) параметр `sender_id` **всегда передается и сохраняется**.

### 3.2. Обнаруженный разрыв данных (Gap)
В функциях выборки сообщений для дайджестов колонка `sender_id` сейчас **отсутствует в SELECT**:
- `get_messages_for_daily_summary` (`database.py:353, 384`):
  `SELECT msg_id, sender_name, sender_username, text, media_description, date, reply_to_msg_id, media_remote_url FROM messages`
- `get_messages_for_range` (`database.py:404`):
  `SELECT msg_id, sender_name, sender_username, text, media_description, date, reply_to_msg_id, media_remote_url FROM messages`

В `summarizer.py`:
- Строка 614: `m_id, name, username, text, m_desc, date, reply_id, m_url = msg`
- Строка 1031: `m_id, name, username, text, m_desc, date, reply_id, m_url = msg`

Если просто добавить `sender_id` в SELECT без изменения распаковки в `summarizer.py`, код упадет с `ValueError: too many values to unpack (expected 8)`.

### 3.3. Решение для безопасной обратной совместимости
1. В `database.py`: расширить SELECT в `get_messages_for_daily_summary` и `get_messages_for_range`, добавив `sender_id` девятым полем.
2. В `summarizer.py`: распаковывать кортеж безопасно по срезу:
   ```python
   m_id, name, username, text, m_desc, date, reply_id, m_url = msg[:8]
   sender_id = msg[8] if len(msg) > 8 else None
   ```
   Это гарантирует, что существующие моки и тесты (например, `make_messages()` в `test_fix_weekly.py`, генерирующий 8-кортежи) продолжат работать со 100% успехом!
3. Для подсчета активности участников дня:
   ```python
   from collections import Counter
   author_counts = Counter(
       msg[8] for msg in filtered_messages 
       if len(msg) > 8 and msg[8]
   )
   # Топ-10 самых активных врачей выборки:
   top_user_ids = [uid for uid, _ in author_counts.most_common(10)]
   ```

---

## 4. Аудит рубрики «ЭКСПЕРТ ДНЯ» (Prompt Rubric Audit)

### 4.1. Текущий текст в Daily Prompt (`summarizer.py:716-718`)
```
    9.🌟 ЭКСПЕРТ ДНЯ 
    Выбери врача из чата, который давал самый полезные советы или протокол. Напиши его имя и за что награжден. При выборе эксперта отдавай приоритет тем, чьи сообщения вызвали одобрение коллег или на чьи советы другие врачи отвечали благодарностью. Эксперт — это тот, чьи слова можно вставить в учебник, или тот, кому коллеги сказали "спасибо" за совет
```

### 4.2. Текущий текст в Weekly Prompt (`summarizer.py:1156-1158`)
```
    ## 🌟 ДОСКА ПОЧЕТА (ГЕРОИ НЕДЕЛИ)
    (ЗАПРЕЩЕНО писать просто список имен. Перечисли врачей, которые на этой неделе генерировали контент. Для каждого участника ОБЯЗАТЕЛЬНО укажи его конкретную заслугу или ценный вклад. Пример: "**Доктор Иванов** — за филигранный разбор КЛКТ в сложном кейсе, **Доктор Петров** — за подробный протокол адгезивной фиксации").
```

### 4.3. Критический анализ недостатков
- В промпт передается только плоский лог `MSG_101 | Доктор Алексей: ...`.
- Модель ничего не знает о клиническом профиле: ортопед это, хирург, терапевт или начинающий интерн; на каком оборудовании работает врач (микроскоп, КЛКТ, сканер); каков его подтвержденный клинический статус.
- В результате звание «ЭКСПЕРТ ДНЯ» модель сейчас может присудить за банальную, но громкую эмоциональную фразу, на которую кто-то ответил «спасибо».

### 4.4. Предлагаемая модернизация рубрики
После добавления блока профилей активных участников дня изменить инструкцию рубрики 9:
```
    9.🌟 ЭКСПЕРТ ДНЯ 
    Выбери врача из чата, который продемонстрировал наивысшую клиническую экспертизу и помог коллегам.
    КРИТИЧЕСКИ ВАЖНО: Если в блоке «НАКОПЛЕННЫЕ ПРОФИЛИ УЧАСТНИКОВ ОБСУЖДЕНИЯ» содержатся профили врачей, обязательно сопоставляй советы доктора с его подтвержденной специализацией, клиническим арсеналом и опытом. Приоритет отдавай обоснованным клиническим протоколам и рекомендациям врачей с подтвержденным статусом в данной области, а не случайным или эмоциональным репликам. Укажи имя/ник эксперта, его клиническую специализацию/статус и конкретную пользу, принесенную сообществу.
```
Аналогично для еженедельной «ДОСКИ ПОЧЕТА»:
```
    ## 🌟 ДОСКА ПОЧЕТА (ГЕРОИ НЕДЕЛИ)
    (ЗАПРЕЩЕНО писать просто список имен. Перечисли врачей, внесших наибольший клинический вклад. Опирайся на накопленные профили участников: указывай подтвержденную специализацию доктора и его реальную клиническую заслугу на этой неделе. Пример: "**Доктор Иванов** (ортопед) — за разбор препарирования под виниры, **Доктор Петров** (эндодонтист) — за протокол распломбировки каналов").
```

---

## 5. Интеграция `format_users_chunk_context` из `user_memory.py`

### 5.1. Анализ `user_memory.format_users_chunk_context` (`user_memory.py:150-200`)
```python
async def format_users_chunk_context(user_ids: List[int]) -> str:
```
- Принимает список `user_ids`.
- Берет до 20 уникальных ID: `unique_ids = list(dict.fromkeys(user_ids))[:20]`.
- Пакетно загружает досье из базы: `database.get_users_memory_batch(unique_ids)`.
- Для каждого пользователя компонует:
  - Метку: `Имя (@username)` либо `Врач #{uid}`.
  - Специализацию: `mem.get("specialty")`.
  - Выжимку: `mem.get("group_summary")` (при отсутствии — `mem.get("clinical_summary")`), усеченную до 300 символов.
  - Формирует строку вида: `• Доктор Алексей (@doc_alex): Ортопед; Тотальные реабилитации BOPT...`
- Заворачивает в служебный заголовок:
  ```
  === НАКОПЛЕННЫЕ ПРОФИЛИ УЧАСТНИКОВ ОБСУЖДЕНИЯ (ИЗ БЕСЕДЫ) ===
  [Справочная информация для ассистента: это выжимка из накопленной памяти о врачах-участниках текущего обсуждения, составленная ИИ по их сообщениям в чате. Учитывай специализацию и клинический опыт собеседников]:
  • ...
  ```
- Если профилей нет, возвращает пустую строку `""`.

### 5.2. Модификация функции для контроля бюджета (Параметр `max_chars`)
Для строгого соблюдения ограничения `<=` 2000 символов в `format_users_chunk_context` рекомендуется добавить опциональный параметр:
```python
async def format_users_chunk_context(user_ids: List[int], max_chars: Optional[int] = None) -> str:
```
Логика наполнения:
- При формировании списка `notes` перед добавлением очередного врача проверяется, не превысит ли суммарный размер заголовка и строк лимит `max_chars`.
- Если превышает — цикл прерывается, сохраняя только полные карточки врачей без обрыва слов на полуслове.
- В конце применяется страховочный срез: `return res[:max_chars]` (если `max_chars` задан).

---

## 6. Бюджетирование токенов и символов (Token & Character Budget)

### 6.1. Текущие бюджеты в `summarizer.py`
1. `WEEKLY_HTML_LIMIT = 9500` (порог для Telegraph и дефолтный лимит в `html_safe.safe_truncate_html`).
2. `DAILY_CHAR_BUDGET = WEEKLY_HTML_LIMIT - 1000` = 8500 символов.
3. `WEEKLY_CHAR_BUDGET = WEEKLY_HTML_LIMIT - 1200` = 8300 символов.
4. **Важно понимать**: Эти константы управляют **длиной ответа модели (генерации)**, чтобы итоговая Telegraph-статья не упиралась в обрезку и не теряла хвостовые разделы («ЭКСПЕРТ ДНЯ», «ЮМОР»).

### 6.2. Бюджет инжектируемого контекста пользователей (Input Prompt Budget)
- Требование R2: «Объем инжектируемого контекста пользователей в дайджесте строго ограничен (не более 2000 символов суммарно), предотвращая сжатие других разделов сводки».
- Константа в `summarizer.py`:
  ```python
  MAX_USERS_CONTEXT_CHARS = 2000
  ```
- В `process_summary_batch` и `process_weekly_batch`:
  ```python
  users_chunk_context = ""
  if top_user_ids:
      users_chunk_context = await user_memory.format_users_chunk_context(
          top_user_ids, max_chars=MAX_USERS_CONTEXT_CHARS
      )
      if len(users_chunk_context) > MAX_USERS_CONTEXT_CHARS:
          users_chunk_context = users_chunk_context[:MAX_USERS_CONTEXT_CHARS]
  ```

### 6.3. КРИТИЧЕСКИЙ РЕГРЕССИОННЫЙ РИСК: Регулярные выражения в тестах
При проверке регрессионного сьюта обнаружено:
1. `test_digest_formatting.py:228-230`:
   ```python
   daily_prompt = _SUMM_SRC.split("=== ПРАВИЛА ОФОРМЛЕНИЯ (ЖЕСТКО) ===", 1)[1].split('"""', 1)[0]
   daily_numbers = {int(n) for n in re.findall(r"\{DAILY_CHAR_BUDGET\}|(\d{4,5})\s*символ", daily_prompt) if n}
   check("в дневном промпте нет зашитых цифр длины", not daily_numbers)
   ```
2. `test_fix_weekly.py:254-260`:
   ```python
   numbers = {int(n) for n in re.findall(r"(\d{4,5})\s*символ", prompt)}
   check("в промпте есть цифра объёма", numbers)
   check("объём в промпте — одна и та же цифра во всех упоминаниях", len(numbers) == 1)
   check("цифра взята из константы, а не зашита", numbers == {S.WEEKLY_CHAR_BUDGET})
   ```

**ПРАВИЛО БЕЗОПАСНОСТИ**:
- **КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО** писать в тексте промпта или в возвращаемом значении `format_users_chunk_context` текст вроде `"до 2000 символов"` или `"бюджет 2000 символов"`!
- Любое 4-5 значное число рядом со словом «символ» в промпте немедленно сломает тесты `test_digest_formatting.py` и `test_fix_weekly.py`.
- Лимит 2000 символов реализуется строго программно, без словесного дублирования в промпте.

---

## 7. Карта безопасного мокирования для тестов (Mocking Map)

При тестировании R2 и разработке `test_memory_e2e_integration.py` **СТРОЖАЙШЕ ЗАПРЕЩЕНО** выполнять сетевые вызовы в прод. Все побочные эффекты должны быть полностью изолированы.

| Компонент / Функция | Реальный эффект в проде | Способ обязательного мокирования в тестах |
|---|---|---|
| `client.send_message` | Отправка сообщения в реальный суперчат Telegram | Класс `FakeClient` с массивом `sends = []` и возвратом фейкового объекта `type("Sent", (), {"id": 101})()` |
| `client.get_messages` | Чтение истории сообщений чата по сети | `FakeClient.get_messages` возвращает пустой список `[]` |
| `client.pin_message` | Закрепление сообщения в Telegram группе | `FakeClient.pin_message` собирает `pins.append((chat_id, message_id))` |
| `summarizer._generate_text_singleflight` | Вызов Gemini API через подпроцесс (трата квот, риск блокировки ключей) | Подмена на `fake_generate` с возвратом `type("FakeResp", (), {"text": mock_text})()` |
| `summarizer.create_telegraph_page_async` | Создание страницы на `api.telegra.ph` (сетевой HTTP-запрос) | Подмена на `fake_telegraph`, возвращающий `("https://telegra.ph/mock-page", None)` |
| `runtime_guard.write_summary_status` | Запись статуса в `bot_summary_status.json` | Подмена на `lambda s: stages.append(s)` либо перенаправление `SUMMARY_STATUS_PATH` в `tempfile` |
| База данных SQLite (`database.DB_PATH`) | Чтение/запись в боевую `stomat_bot.db` | Установка `config.DB_PATH = os.path.join(temp_dir, "test.db")` перед вызовами |

---

## 8. Аудит качества кода и линтера (Ruff Audit)

Команда: `python -m ruff check user_memory.py summarizer.py database.py assistant.py`

### Результаты по модулям:
1. `user_memory.py`: **0 ошибок** (Clean).
2. `database.py`: **0 ошибок** (Clean).
3. `summarizer.py`: **4 ошибки E701** (несколько операторов на одной строке через двоеточие):
   - Строка 567: `if topic_id: send_params['reply_to'] = topic_id`
   - Строка 972: `if topic_id: send_params['reply_to'] = topic_id`
   - Строка 1248: `if topic_id: send_params['reply_to'] = topic_id`
   - Строка 1307: `if topic_id: send_params['reply_to'] = topic_id`
   *Рекомендация*: В этапе реализации R2 разбить эти 4 однострочника на стандартные двухстрочные блоки `if topic_id:\n    send_params['reply_to'] = topic_id`, что обеспечит 0 ошибок ruff в `summarizer.py`.
4. `assistant.py`: 21 предсуществующая ошибка (E402, E701, F841, E712, F401). Для выполнения acceptance criteria по `assistant.py` потребуется точечное исправление этих мест (перенос неверно расположенных импортов и форматирование).

---

## 9. Чек-лист готовности к реализации R2 (Implementation Plan Guidance)

- [x] Определен механизм выборки `sender_id` из `messages` без поломки обратной совместимости.
- [x] Определен алгоритм вычисления топ-активных врачей дня/недели на основе частотности реплик.
- [x] Определен интерфейс `format_users_chunk_context` с поддержкой `max_chars=2000`.
- [x] Сформулирован модернизированный текст для рубрики 9 («ЭКСПЕРТ ДНЯ») и еженедельной «ДОСКИ ПОЧЕТА».
- [x] Локализован и предотвращен критический регрессионный сбой тестов `test_digest_formatting.py` и `test_fix_weekly.py`.
- [x] Составлена полная карта мокирования сетевых вызовов для безопасного тестирования.
