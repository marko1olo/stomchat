# Original User Request

## 2026-09-04T13:39:40Z

Комплексный сквозной аудит, стресс-тестирование и расширение применения долговременной клинической памяти врача (user_memory.py) во всех ключевых модулях бота StomChat: проверка качества заполнения досье, интеграция профилей в summarizer.py (выбор эксперта дня) и верификация параллельной работы без блокировок БД.

Working directory: c:\Users\danat\Desktop\stomchat
Integrity mode: development

СТРОЖАЙШИЙ ЗАПРЕТ: НЕ ОТПРАВЛЯТЬ ТЕСТОВЫЕ СООБЩЕНИЯ В ПРОД, В ТЕЛЕГРАМ-ГРУППУ ИЛИ РЕАЛЬНЫМ ПОЛЬЗОВАТЕЛЯМ! Все тесты и симуляции проводить строго на временных изолированных БД и с моками сетевой отправки!

## Requirements

### R1. Сквозная E2E-симуляция наполнения памяти врача в ЛС и беседе
Провести программную E2E-симуляцию реального многошагового клинического взаимодействия:
- Симуляция диалога в ЛС из 8-12 последовательных реплик врача (специализация, оснащение микроскопом, любимые протоколы адгезии, разбор зуба 3.6). Проверить, что каждые 4 сообщения LLM обновляет и уплотняет досье в user_memories, не допуская дублей и сохраняя структуру.
- Симуляция беседы: проверка выборки активных врачей из лога/дампа сообщений, запуск такта демона и подтверждение, что group_summary формируется строго для тех, кто реально писал сообщения, с соблюдением лимита 8 КБ.

### R2. Интеграция клинических профилей в summarizer.py (Дайджесты и Газета)
Подключить память активных участников обсуждения к генерации дайджестов в summarizer.py:
- При формировании дайджеста дня/недели подгружать профили самых активных авторов через format_users_chunk_context.
- Использовать эти профили в промптах для точного и обоснованного выбора в рубрике «ЭКСПЕРТ ДНЯ» (учитывая подтвержденную специализацию и опыт доктора, а не случайные фразы).
- Строго контролировать бюджет символов/токенов в промпте дайджеста, исключая обрезку и переполнение контекста.

### R3. Изоляция, безопасность прода и стресс-тестирование параллельного доступа к SQLite
КРИТИЧЕСКИЙ ЗАПРЕТ: ЗАПРЕЩЕНО ОТПРАВЛЯТЬ ТЕСТОВЫЕ СООБЩЕНИЯ В ПРОД, В ТЕЛЕГРАМ-ГРУППУ ИЛИ РЕАЛЬНЫМ ПОЛЬЗОВАТЕЛЯМ!
- Все тесты, симуляции и E2E сценарии должны выполняться СТРОГО на временных изолированных БД (в tempfile / копии базы) с замоканной отправкой сообщений в Telegram (через mock/stub клиента без сетевых вызовов в боевой чат).
- Запустить параллельные асинхронные задачи: одновременная запись реплики в ЛС, чтение профиля, обновление памяти в фоне и запуск демона группы на изолированной тестовой БД.
- Подтвердить отсутствие ошибок sqlite3.OperationalError: database is locked и корректность транзакций _run_db.
- Соблюдать бережное отношение к API-ключам (cooldown 2.5-3 сек между запросами, никаких параллельных спам-пачек).

### R4. Регрессионная безопасность и автоматический сьют проверок
- Обеспечить 100% прохождение существующих тестов (test_user_memory.py, test_budget_nesting.py, test_fix_pm.py, test_startup_boot.py).
- Написать новый автоматический тестовый файл test_memory_e2e_integration.py, подтверждающий все вышеуказанные требования.
- Пройти проверку линтером ruff check по всем затронутым модулям с 0 ошибок.

## Acceptance Criteria

### Качество и динамика памяти (Memory Quality)
- [ ] После симуляции диалога из 8 реплик профиль врача в user_memories содержит корректно заполненные разделы (специализация, арсенал, протоколы, кейс) без повторов одних и тех же предложений.
- [ ] Односложные реплики («спасибо», «ок») не вызывают холостых вызовов LLM и не увеличивают счетчик цикла актуализации.
- [ ] Демон группы обрабатывает только авторов новых сообщений и завершает такт без запросов к LLM, если новых сообщений не было.

### Кросс-модульная интеграция (Summarizer Integration)
- [ ] В summarizer.py подключен контекст профилей участников дня, и рубрика «ЭКСПЕРТ ДНЯ» явно опирается на клинический статус врача.
- [ ] Объем инжектируемого контекста пользователей в дайджесте строго ограничен (не более 2000 символов суммарно), предотвращая сжатие других разделов сводки.

### Стабильность и тесты (Stability & Verification)
- [ ] Скрипт test_memory_e2e_integration.py завершается со статусом 0 и подтверждает корректность всех сценариев.
- [ ] Регрессионные тесты (test_user_memory.py, test_startup_boot.py, test_budget_nesting.py, test_fix_pm.py) завершаются с результатом 100% PASSED.
- [ ] ruff check user_memory.py summarizer.py database.py assistant.py завершается с 0 ошибок.

## 2026-09-08T07:44:06Z

Comprehensive audit of Telegram bot (StomChat) runtime logs, SQLite databases (42k+ active group messages, 117k+ archive messages, 351 PM messages), user sentiment, bot trigger/silence dynamics, and clinical dialogue quality, followed by actionable rebalancing proposals.

Working directory: c:\Users\danat\Desktop\stomchat
Integrity mode: development

## Requirements

### R1. Bot Activity vs Silence Audit (Logs & Runtime)
Perform an exhaustive analysis of `bot.log`, `bot_supervisor.log`, and `assistant_state.json`:
- Exact breakdown of when the bot triggers (Direct Reply, Mentions, Sequential Follow-ups, Passive Clinical Triggers, Media Triggers) vs when it stays silent.
- Quantitative distribution of all silence/suppression causes:
  - `passive_cooldown` (120-minute window)
  - `retry_backoff` (10-minute failed attempt backoff)
  - `dialogue_stale` (count_since > 5)
  - `dialogue_triage_rejected` (triage NO)
  - `negative_feedback_silenced` (4-hour user penalty)
  - `validator_rejected` (reviewer rejected draft)
  - `LLM errors / cascade exhaustion / provider bans` (503/504 cooldowns)
- Identify all instances where the bot SHOULD have answered a clinical query or assisted a doctor, but stayed silent due to overly strict gate conditions.

### R2. User Messages & Sentiment Analysis (Databases)
Analyze `stomat_bot.db` (`messages`, `pm_messages`, `user_memories`, `bot_sent_messages`) and `stomat_archive.db`:
- How do clinicians react to bot replies in the general chat and in private messages (PM)?
- Track user feedback, complaints, mockery, or confusion regarding bot silence, abrupt conversation termination, or repetitive answers.
- Clinical topic breakdown: what dental specialties (endodontics, implantology, surgery, prosthetics, orthotropics/aligners) generate the highest engagement vs what gets ignored?
- Multi-turn conversation depth: how many dialogue turns do doctors actually want to have with the bot in group discussions?

### R3. Rebalancing Proposals & Mathematical Justification
Formulate concrete, production-ready rebalancing recommendations:
- **Passive Cooldown:** Assess whether 120 minutes is optimal or if a dynamic cooldown based on chat message velocity (e.g. 45-60 min during peak clinical hours, longer during quiet nights) is superior.
- **Dialogue Freshness Window:** Evaluate the `count_since <= 5` and `10 minutes` constraints for sequential follow-up. Does 5 messages cut off legitimate follow-ups in busy chats?
- **Triage Sensitivity:** Review prompt instructions in `check_llm_triage` and `check_dialogue_continuation_triage`. Are clinical questions being falsely classified as "chitchat" or "opinion polls"?
- **Quality Validator Tuning:** Are good clinical drafts being rejected due to strict length or phrasing rules?

## Acceptance Criteria

### Quantitative Completeness
- [ ] Analysis covers 100% of recorded suppression events in available logs.
- [ ] SQLite queries analyze all 42k+ active messages and 351 PM records without truncating datasets.
- [ ] Clear statistical tables with percentages and frequency distributions for both reply triggers and silence causes.

### Qualitative & Clinical Depth
- [ ] Real message quotes and message IDs demonstrating exact false-negative silences and successful multi-turn discussions.
- [ ] Clinician feedback classification (positive, constructive, skeptical, negative/frustrated).

### Actionable Deliverables
- [ ] A dedicated report `REPORT_CHAT_BALANCE_AND_LOGS.md` created in `c:\Users\danat\Desktop\stomchat` with concrete configuration diffs and code patch recommendations.
