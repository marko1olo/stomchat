# Технический отчет: Архитектура, механика и аудит клинической памяти (user_memory.py & assistant.py)

**Дата**: 2026-09-04  
**Автор**: Explorer Survey 1 (Memory & Assistant)  
**Область**: `user_memory.py`, `assistant.py`, `database.py`, тесты `test_user_memory.py` и смежные компоненты.  
**Целевая задача**: Детальное картирование всех технических аспектов для реализации **R1** (Сквозная E2E-симуляция взаимодействия с памятью врача в ЛС и беседе), проверка соответствия критериям приемки и подготовка к интеграционному тестированию.

---

## 1. Резюме (Executive Summary)

Модуль долговременной памяти `user_memory.py` реализует двухуровневую клиническую память о врачах:
1. **Персональная память в ЛС (PM Memory)**: глубина до 64 КБ (64 000 символов), структурированное клиническое досье, обновляемое не тупым дописыванием (`append`), а интеллектуальным переписыванием и уплотнением нейросетью раз в 4 сообщения или при разборе содержательного кейса.
2. **Память беседы (Group Memory)**: глубина до 8 КБ (8 000 символов), фоновый такт демона (по умолчанию раз в 4 часа), анализирующий накопившиеся сообщения только реальных участников группы (от 3 сообщений). При отсутствии новых сообщений такт мгновенно завершается без единого обращения к LLM.

Все 4 существующих регрессионных теста (`test_user_memory.py`, `test_budget_nesting.py`, `test_fix_pm.py`, `test_startup_boot.py`) завершаются со статусом **100% PASSED**.
Линтер `ruff check` на файлах `user_memory.py` и `database.py` показывает **0 ошибок** (в `assistant.py` присутствуют 21 унаследованная ошибка форматирования/импортов, не связанные с `user_memory.py`).

В ходе аудита выявлены критические технические нюансы, обязательные к учету при разработке E2E-симулятора (`test_memory_e2e_integration.py`):
- Наличие жесткого 15-секундного кулдауна `_PM_MEMORY_COOLDOWN` в памяти процесса, который в быстрой симуляции без сброса/мока подавит вызовы после 1-й реплики.
- Отсутствие программной функции удаления дубликатов предложений в `user_memory.py` (дедупликация сейчас возложена только на промпт LLM и факты в `new_facts`).
- Точное имя функции контекста для ЛС — `format_clinician_memory_prompt` (в ТЗ упоминалась как `format_user_memory_context`).

---

## 2. Структуры данных в SQLite

Все данные памяти хранятся в единой таблице `user_memories` в SQLite (файл базы данных задается `config.DB_PATH`, потокобезопасный доступ через однопоточный исполнитель `_DB_EXECUTOR` в `database.py`). Отдельной таблицы `group_memory` нет — память группы хранится в отдельной колонке `group_summary` той же таблицы.

### Схема таблицы `user_memories` (`database.py:300-317`):
```sql
CREATE TABLE IF NOT EXISTS user_memories (
    user_id INTEGER PRIMARY KEY,
    username TEXT DEFAULT '',
    first_name TEXT DEFAULT '',
    specialty TEXT DEFAULT '',
    clinical_summary TEXT DEFAULT '',
    group_summary TEXT DEFAULT '',
    facts_json TEXT DEFAULT '[]',
    message_count INTEGER DEFAULT 0,
    pm_message_count INTEGER DEFAULT 0,
    group_message_count INTEGER DEFAULT 0,
    last_pm_analyzed_id INTEGER DEFAULT 0,
    last_group_analyzed_id INTEGER DEFAULT 0,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_user_memories_updated ON user_memories(last_updated);
```

### Назначение колонок:
| Колонка | Тип | Описание и лимиты |
|---|---|---|
| `user_id` | `INTEGER` | Telegram ID врача (Primary Key) |
| `username` | `TEXT` | Никнейм в Telegram (без @) |
| `first_name` | `TEXT` | Имя врача |
| `specialty` | `TEXT` | Подтвержденная клиническая специализация (терапевт, эндодонтист, ортопед и т.д.) |
| `clinical_summary` | `TEXT` | Полное переписанное клиническое досье из ЛС (жесткий потолок **64 000 символов**) |
| `group_summary` | `TEXT` | Выжимка профиля по поведению в группе (жесткий потолок **8 000 символов**) |
| `facts_json` | `TEXT` | JSON-список коротких фактов и предпочтений (до 30 элементов) |
| `message_count` | `INTEGER` | Унаследованный общий счетчик |
| `pm_message_count` | `INTEGER` | Число содержательных сообщений в ЛС (для триггера `cnt % 4 == 0`) |
| `group_message_count` | `INTEGER` | Общее число обработанных сообщений автора в группе |
| `last_pm_analyzed_id` | `INTEGER` | ID последнего проанализированного сообщения в ЛС |
| `last_group_analyzed_id` | `INTEGER` | ID последнего обработанного сообщения из таблицы `messages` |
| `last_updated` | `TIMESTAMP` | Дата и время последней актуализации профиля |

### Автомиграция колонок (`database.py:320-330`):
В `database.init_db()` выполняется проверка и добавление колонок (`group_summary`, `pm_message_count`, `group_message_count`, `last_pm_analyzed_id`, `last_group_analyzed_id`) через `ALTER TABLE` для плавного перехода существующих БД.

### Взаимодействие с таблицей `messages` (`database.py:131-165`):
Таблица `messages` используется демоном групповой памяти. В ней хранятся сообщения чата (`sender_id`, `sender_name`, `sender_username`, `text`, `msg_id`, `date`, `has_media` и др.). Демон опирается на индексы `idx_sender`, `idx_date`, `idx_reply_to`.

---

## 3. Механика обновления памяти в ЛС (PM Memory Lifecycle)

### 3.1. Точка вызова в `assistant.py` (`assistant.py:4973-4992`)
После генерации и успешной отправки ответа врача в ЛС (`handle_private_message`):
```python
sender = getattr(event, "sender", None)
sender_username = getattr(sender, "username", "") or ""
sender_first_name = getattr(sender, "first_name", "") or ""
user_msg_summary = text or (f"Снимок: {media_description}" if media_description else "")
import runtime_guard
runtime_guard.create_task(
    user_memory.update_clinician_memory_async(
        user_id=chat_id,
        user_message=user_msg_summary,
        bot_response=reply_text,
        username=sender_username,
        first_name=sender_first_name,
    ),
    name=f"update_clinician_memory_{chat_id}"
)
```
Вызов не блокирует отдачу ответа пользователю и выполняется асинхронно в фоне.

### 3.2. Фильтр тривиальных сообщений (`is_trivial_message`, `user_memory.py:45-70`)
Функция проверяет входящее сообщение пользователя:
1. Если `len(cleaned) < 8` -> `True` (тривиально).
2. Полное совпадение с множеством `_TRIVIAL_USER_PATTERNS` (`"спасибо"`, `"ок"`, `"привет"`, `"понял"`, `"ясно"`, `"/help"`, `"/start"` и др.) -> `True`.
3. Если все слова сообщения состоят из тривиальных слов или слов вежливости (`"большое"`, `"огромное"`, `"очень"`, `"вам"`, `"тебе"`, `"день"`, `"утро"`, `"вечер"`) -> `True`.
4. Короткие фразы (до 3 слов), начинающиеся со слов благодарности или приветствия -> `True`.

**Поведение при тривиальном сообщении**:
В строке 218: `if not user_id or is_trivial_message(user_message): return`.
Функция завершает работу **мгновенно**:
- НЕ увеличивает `pm_message_count`.
- НЕ вызывает LLM.
- НЕ обновляет SQLite.
Это строго удовлетворяет критерию приемки: *"Односложные реплики («спасибо», «ок») не вызывают холостых вызовов LLM и не увеличивают счетчик цикла актуализации."*

### 3.3. Кулдаун защиты квот (`user_memory.py:42-43, 221-226`)
```python
_PM_MEMORY_COOLDOWN = 15.0
_LAST_PM_UPDATE_TS: Dict[int, float] = {}

now = time.time()
last_ts = _LAST_PM_UPDATE_TS.get(user_id, 0.0)
if now - last_ts < _PM_MEMORY_COOLDOWN:
    logger.debug(f"PM clinician memory update for {user_id} throttled by cooldown.")
    return
_LAST_PM_UPDATE_TS[user_id] = now
```
**Внимание**: при кулдауне функция также делает ранний `return` без инкремента счетчика!  
*Важно для симуляции*: в E2E-тестах при симуляции 8-12 реплик необходимо либо сбрасывать словарь `_LAST_PM_UPDATE_TS[user_id] = 0`, либо выставлять `_PM_MEMORY_COOLDOWN = 0`, чтобы последующие сообщения не отсекались.

### 3.4. Логика счетчика и условия срабатывания уплотнения (`user_memory.py:232-250`)
```python
current_pm_count = mem.get("pm_message_count", 0) + 1

is_first_time = not current_summary
is_interval = (current_pm_count % PM_UPDATE_EVERY_N_MESSAGES == 0) # каждые 4 сообщения
has_rich_case = len(user_message) > 250 or any(w in user_message.lower() for w in (
    "снимок", "рентген", "клкт", "пациент", "кейс", "bopt", "имплант", "канал", "протокол"
))

if not (is_first_time or is_interval or has_rich_case):
    # Просто инкрементируем счетчик сообщений без вызова дорогой LLM
    await database.save_user_memory(
        user_id=user_id,
        pm_message_count=current_pm_count,
        username=username,
        first_name=first_name
    )
    return
```
- Если условие не выполнено (например, сообщение 2 или 3 без триггерных слов), в БД сохраняется только увеличенный `pm_message_count`, а вызов LLM **не происходит**.
- Если наступило 4-е сообщение (`current_pm_count % 4 == 0`), либо это первый раз, либо сообщение содержит богатый клинический кейс -> запускается пайплайн переписывания и уплотнения через LLM.

---

## 4. Логика уплотнения (Compaction) и структура досье

### 4.1. Промпт актуализации (`user_memory.py:260-287`)
В модель отправляется:
- Текущая подтвержденная специализация (`current_spec`).
- Текущее клиническое досье (`current_summary`).
- Свежее сообщение врача (до 2000 символов).
- Ответ ассистента (до 1000 символов).

Критические инструкции промпта:
1. **НЕ ДОПИСЫВАТЬ** текст просто в конец! Переписать профиль как связный, структурированный документ.
2. Уточнить специализацию (терапевт, эндодонтист, ортопед, хирург-имплантолог, ортодонт, гнатолог, детский стоматолог).
3. Обновить арсенал, бренды, аппараты и протоколы (BOPT, микроскоп, ультразвук, бинокуляры, OptiBond FL, силеры, импланты).
4. Добавить разобранные кейсы (номер зуба, патология, динамика).
5. Удалить пустые фразы и устаревшие детали.
6. Ответ строго в JSON:
```json
{
  "specialty": "уточненная специализация",
  "rewritten_summary": "полностью переписанный и актуализированный текст клинического профиля врача (до 64 КБ)",
  "new_facts": ["факт 1", "факт 2"]
}
```

### 4.2. Обработка ответа и предотвращение дубликатов (`user_memory.py:302-340`)
1. **Специализация**: обновляется, если длина новой строки > 2 символов.
2. **Факты (`facts_json`)**: дедупликация на уровне строк:
   ```python
   for f in new_facts:
       f_str = str(f).strip()
       if f_str and f_str not in current_facts:
           current_facts.append(f_str)
   if len(current_facts) > 30:
       current_facts = current_facts[-30:]
   ```
3. **Клиническое резюме (`clinical_summary`)**:
   - Заменяется на `rewritten_summary`.
   - Обрезается строго по лимиту: `len(final_summary) > 64000: final_summary = final_summary[:64000]`.
4. **Обратная совместимость**:
   Первая строка `final_summary` вместе со специализацией синхронизируется в `user_profiles.profile_portrait` (до 400 символов).

### 4.3. Аудит предотвращения дублей предложений (Sentence Deduplication)
**Важное наблюдение**:
- Для массива `facts_json` дедупликация реализована программно (`f_str not in current_facts`).
- Для текста `clinical_summary` в текущей реализации `user_memory.py` дедупликация предложений выполняется **на уровне генерации LLM** через системный промпт («НЕ ДОПИСЫВАЙ... актуализируй и ПЕРЕПИСАТЬ... Удали пустые фразы»). Отдельной функции `deduplicate_sentences(text)` в Python-коде нет.
- *Рекомендация для стабильности*: для 100% гарантии отсутствия дублирующихся предложений в `rewritten_summary` при любых ответах модели (включая галлюцинации дешевых моделей) полезно добавить программную нормализацию/дедупликацию идентичных предложений при сохранении в `save_user_memory` или в `update_clinician_memory_async`.

### 4.4. Структура разделов досье (Structured Sections)
В требованиях R1 указаны 4 раздела:
- Специализация
- Оснащение / арсенал (микроскоп, ультразвук)
- Протоколы адгезии и материалы
- Клинические кейсы (разбор зуба 3.6)

В текущем промпте эти 4 темы прямо перечислены в пунктах 2, 3, 4 инструкций. При форматировании памяти для системного промпта ЛС (`format_clinician_memory_prompt`) формируются блоки:
- `• Подтвержденная специализация: ...`
- `• Актуальное клиническое досье (оборудование, материалы, протоколы, кейсы): ...`
- `• Ключевые предпочтения и особенности практики: ...` (до 8 последних фактов)

---

## 5. Демон памяти общей беседы (Group Memory Daemon)

### 5.1. Архитектура и запуск
- Запускается в `main.py:2815` как фоновая задача: `runtime_guard.create_task(user_memory.group_memory_daemon_loop(), "group_memory_daemon")`.
- Функция цикла: `group_memory_daemon_loop(interval_seconds=14400)` (раз в 4 часа).
- Один такт обработки: `process_group_memory_daemon_batch(min_new_messages=3, limit=5)`.

### 5.2. Алгоритм выборки активных врачей (`database.py:1239-1273`)
SQL-запрос функции `get_unprocessed_group_users`:
```sql
SELECT m.sender_id, m.sender_name, m.sender_username,
       COUNT(m.msg_id) as cnt, MAX(m.msg_id) as max_id
FROM messages m
LEFT JOIN user_memories um ON um.user_id = m.sender_id
WHERE m.sender_id IS NOT NULL AND m.sender_id != 0
  AND m.text IS NOT NULL AND LENGTH(TRIM(m.text)) > 15
  AND m.msg_id > COALESCE(um.last_group_analyzed_id, 0)
GROUP BY m.sender_id
HAVING cnt >= ?
ORDER BY max_id DESC
LIMIT ?
```
**Ключевые свойства фильтрации**:
1. Отсекает ботов и пустых отправителей: `m.sender_id IS NOT NULL AND m.sender_id != 0`.
2. Игнорирует шум и короткие реплики: `LENGTH(TRIM(m.text)) > 15`.
3. Учитывает только сообщения новее ранее обработанных: `m.msg_id > COALESCE(um.last_group_analyzed_id, 0)`.
4. Требует накопления минимум 3 сообщений: `HAVING cnt >= 3`.
5. Сортирует по свежести активности: `ORDER BY max_id DESC`.

### 5.3. Поведение при отсутствии новых сообщений
В `user_memory.py:361-363`:
```python
if not users_to_process:
    logger.info("Group memory daemon: в чате нет новых сообщений от участников. Пропуск такта (запросы к LLM опущены).")
    return
```
Если новых сообщений нет, такт завершается **мгновенно с 0 вызовов LLM**.

### 5.4. Обработка врача и лимит 8 КБ
Для каждого отобранного врача:
1. Выбираются сообщения с момента `last_group_analyzed_id`: `get_user_messages_since(user_id, since_msg_id, limit=25)`.
2. Формируется промпт для компактного профиля беседы (1–2 абзаца, до 8 КБ).
3. Парсится JSON `{"specialty": "...", "group_summary": "..."}`.
4. Жесткое ограничение 8 КБ соблюдается дважды:
   - В `user_memory.py:429-430`: `final_grp_summary[:8000]`
   - В `database.save_user_memory`: `if group_summary and len(group_summary) > 8000: group_summary = group_summary[:8000]`
5. Сохраняется `last_group_analyzed_id = max_id`.
6. Соблюдается кулдаун безопасности API: `await asyncio.sleep(2.5)` между врачами.

---

## 6. Встраивание памяти в `assistant.py`

### 6.1. Личные сообщения (PM Chat)
1. **Загрузка и форматирование** (`assistant.py:4708-4709`):
   ```python
   clinician_mem = await user_memory.get_clinician_memory(chat_id)
   portrait = user_memory.format_clinician_memory_prompt(chat_id, clinician_mem)
   ```
2. **Фоновая инициализация при первом визите** (`assistant.py:4712-4726`):
   Если `clinical_summary` пустое и нет `profile_portrait`, запускается фоновая задача генерации `_bg_portrait`, а в текущий ответ подставляется плейсхолдер `"Клинический профиль доктора формируется."`.
3. **Инъекция в промпты**:
   Переменная `{portrait}` встраивается во все три ветки промпта консилиума в ЛС:
   - Клинический разбор со снимком / архивом / поиском (`assistant.py:4766`)
   - Клинический вопрос без медиа (`assistant.py:4815`)
   - Свободный диалог с коллегой (`assistant.py:4838`)
4. **Обновление памяти после ответа**:
   Строки 4980-4989 через `runtime_guard.create_task` вызывают `update_clinician_memory_async`.

### 6.2. Групповой чат (Group Chat / Bot Mention)
В `check_bot_mention_trigger` (`assistant.py:5089-5090`):
```python
sender_ids = [r[0] for r in context_rows if r[0]]
users_chunk_context = await user_memory.format_users_chunk_context(sender_ids)
```
- Извлекаются `sender_ids` всех авторов недавних сообщений из окна контекста группы.
- `format_users_chunk_context` берет до 20 уникальных авторов, пакетом загружает их память через `get_users_memory_batch`, берет до 300 символов `group_summary` (или `clinical_summary`) и формирует компактный блок:
  `=== НАКОПЛЕННЫЕ ПРОФИЛИ УЧАСТНИКОВ ОБСУЖДЕНИЯ (ИЗ БЕСЕДЫ) ===`.
- Блок инжектируется в промпт ответа бота в группе (`assistant.py:5103`). Суммарный объем блока не превышает ~6 КБ.

### 6.3. Согласование имен функций
В запросе упоминались:
- `format_user_memory_context` -> фактически в `user_memory.py` функция называется `format_clinician_memory_prompt`.
- `format_users_chunk_context` -> совпадает полностью (`user_memory.py:150`).
- `get_user_memory` -> в `database.py:1089` называется `get_user_memory(user_id)`, а обертка в `user_memory.py:73` называется `get_clinician_memory(user_id)`.

---

## 7. Текущий статус проверок и план для симулятора (R1)

### 7.1. Результаты существующих тестов
- `python test_user_memory.py` -> **PASSED: 35, FAILED: 0** (время выполнения ~0.8 с)
- `python test_budget_nesting.py` -> **PASSED: 29, FAILED: 0** (время выполнения ~1.1 с)
- `python test_fix_pm.py` -> **PASSED: 29, FAILED: 0** (время выполнения ~0.9 с)
- `python test_startup_boot.py` -> **PASSED: 51, FAILED: 0** (время выполнения ~14 с, проверен полный цикл старта бота)

### 7.2. Аудит Ruff
- `python -m ruff check user_memory.py database.py` -> **0 ошибок** (All checks passed!).
- `python -m ruff check summarizer.py assistant.py` -> 25 ошибок (21 в `assistant.py`, 4 в `summarizer.py`). Это старые предупреждения (E402, E701, F841, E712), не затронутые текущей работой.

### 7.3. Архитектурный план для нового тестового файла `test_memory_e2e_integration.py` (R1)
Чтобы симуляция строго выполняла требования R1 и критерии приемки, тест должен быть построен следующим образом:
1. **Изоляция окружения**:
   - `tempfile.mkdtemp` для тестовой SQLite БД.
   - Подмена `config.DB_PATH` на временный файл.
   - Замоканные вызовы `generate_gemini_text_async` и `bot_client` (полный запрет отправки в сеть/Telegram).
2. **Сценарий 1: Симуляция диалога в ЛС из 8–12 реплик врача**:
   - Врач делится специализацией (ортопед/эндодонтист), арсеналом (микроскоп Carl Zeiss, ультразвук), протоколами (OptiBond FL, спиртовой протокол) и кейсом (зуб 3.6, деструкция костной ткани у апекса).
   - Вставка тривиальных реплик («спасибо», «ок»): проверка, что `pm_message_count` не увеличивается и мок LLM не вызывается.
   - Сброс `_LAST_PM_UPDATE_TS` перед каждым шагом симуляции, чтобы обойти 15-секундный кулдаун.
   - Проверка срабатывания уплотнения ровно каждые 4 сообщения: вызов LLM на 4-м и 8-м шаге.
   - Проверка итогового профиля в `user_memories`: наличие всех ключевых секций (специализация, арсенал, протоколы, кейс) и отсутствие дублирующихся предложений.
3. **Сценарий 2: Симуляция групповой беседы и такта демона**:
   - Генерация сообщений в таблицу `messages` от нескольких авторов (автор А с 4 сообщениями, автор Б с 1 сообщением, автор В с 0 сообщений).
   - Запуск `process_group_memory_daemon_batch`: подтверждение, что обработан только автор А (>=3 сообщения).
   - Запись большого текста (>10 КБ) в `group_summary` через мок и проверка жесткого лимита 8 000 символов.
   - Повторный запуск такта демона: проверка, что при отсутствии новых сообщений такт завершается с 0 вызовов LLM.
4. **Параллельный стресс-тест без взаимных блокировок (подготовка к R3)**:
   - Конкурентный запуск чтения профиля, записи сообщения и вызова демона через `asyncio.gather`.
   - Проверка отсутствия `sqlite3.OperationalError: database is locked`.

---
*Отчет подготовлен для передачи оркестратору и агентам реализации.*
