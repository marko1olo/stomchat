# Разведка: диагностируемость (bot.log и молчаливые отказы)

Разведчик. Правок в .py нет — только чтение, AST-разбор и пробы.
Область: что оператор может понять по bot.log, когда бот повёл себя не так.

---

## ЗАМЕР 3 (главный результат): что реально лежит в bot.log

bot.log = 12372 строки, 2026-05-18 .. 2026-07-28 09:59.
INFO 9013 / WARNING 1196 / ERROR 197 / без уровня (строки трейсбеков) 1966.
Различных шаблонов: ERROR 23, WARNING 30. Логгеры: `__main__` 5037, `main` 3918,
`summarizer` 358, `httpx` 353, `gemini_client` 265, `database` 230, `runtime_guard` 145.

### Первое, что показал замер: 84% записей ERROR/WARNING — не работа бота, а тесты

Дни 2026-07-27 (2562 строки) и 2026-07-28 (1740 строк) — выдача тестового набора.
Доказательство прямое:
- `main - ERROR - Unexpected error in CallbackQuery handler: падение до event.answer()` (50 раз)
  порождается `test_routing_behaviour.py:248` — `raise RuntimeError("падение до event.answer()")`.
- `main - WARNING - Heartbeat write failed, continuing: файл занят другим процессом` (648 раз)
  порождается `test_routing_behaviour.py:262` — `raise PermissionError("файл занят другим процессом")`.
- `ERROR - Failed to initialize assistant or set commands: object MagicMock can't be used in 'await'
  expression` (5 раз) — мок.

Из 1393 записей ERROR+WARNING: **1175 (84%) тестовые, 218 — работа бота.**
Все крупные кластеры оказались тестовыми: `MESSAGE LOST` 46, `save_message attempt failed` 138,
`background task cancelled` 144, `voice transcription failed` 50, `voice download timed out` 50,
`Error saving bot outgoing message ID: нет такой таблицы` 40.

Это **не находка по коду** — течь уже заткнута: `runtime_guard.py:12-39` (`_default_log_path`
уводит журнал в `bot_test.log` по имени точки входа), `run_all_tests.py:31` ставит
`STOMCHAT_LOG_PATH=bot_test.log`, а `run_all_tests.py:49` держит `bot.log` в списке файлов,
чей md5 сверяется до и после прогона. Дыр в этом не нашёл: тесты запускаются либо напрямую
`python test_*.py`, либо через `run_all_tests.py`; pytest в проекте не используется.

Осталось следствие, которое кодом не лечится: **сам файл никто не почистил.** Оператор,
открывающий bot.log сегодня, читает 4302 строки выдумки — несуществующие чаты, выдуманных
врачей, 648 warning про антивирус, которого не было. Разделительной метки в файле нет.
Минимальная правка: один раз ротировать/усечь bot.log (или вырезать блок 07-27..07-28),
это не правка кода, а гигиена данных.

Второе следствие, важное для доверия к остальным цифрам: **последняя строка работы бота в
bot.log — 2026-06-22.** Подпроцессной архитектуры (`blocking_tools.py`, `media_tools.py`,
`_relay_child_log`) в этом журнале нет ни одной строки: `[gemini-text]`, `prepare-image`,
`extract-frame` не встречаются ни разу. То есть код, который работает сейчас, ещё ни разу
не оставил в bot.log записи, которую оператор мог бы прочитать.

### Таблица: строка журнала -> сколько раз -> можно ли установить причину

**ERROR, работа бота (98 записей):**

| строка | раз | причина устанавливается? |
|---|---|---|
| Groq fallback exhausted. | 8 | нет — но строка мертва, см. «опровергнуто» |
| Ошибка обработки медиа N: `<пусто>` | 8 | нет, поле причины пустое; строка тоже мертва |
| fatal bot crash | 5 | да, есть трейсбек |
| ❌ Все попытки исчерпаны. Gemini недоступен (N/N). | 4 | нет — строка мертва |
| message handler failed | 4 | да, есть трейсбек |
| health_check failed N/3: `<пусто>` | 4 | **НЕТ — находка 4, код живой** |
| Daily/Weekly was not delivered to any target | 3+3 | частично: цель есть, причина отказа нет |
| summary failed / weekly summary failed | 2+2 | да, есть трейсбек |
| Gemini attempts exhausted. Fallback is disabled | 2 | нет — строка мертва |
| Daily/Weekly not delivered to all; missing=[chat:topic] | 1+1+1 | да, цели перечислены |

**WARNING, работа бота (120 записей):**

| строка | раз | причина устанавливается? |
|---|---|---|
| recent delivery scan failed chat=-N topic=N: API access for bot users is restricted | 44 | да, полный текст ошибки |
| Gemini failed attempt=N/12 key=...XXXXX: 400 FAILED_PRECONDITION User location is not supported | 45 | да, полный текст |
| Groq fallback failed attempt=N key=...XXXXX: 413 Request too large for llama-3.3-70b | 16 | да, полный текст |
| health_check нашёл пропущенные сообщения: remote=N local=N | 34 | да |
| Groq vision key failed: Connection error. | 2 | частично |

**Ни одной записи в bot.log про 14 убийств процесса сторожем — находка 1.**

---

## Находка 1 (главная). Сторож убивает процесс, не оставляя в журнале ни одной строки

`runtime_guard.py:194-209`, `_watchdog_loop`. При зависании цикла событий пишет дамп в
`bot_watchdog_dump.txt` и вызывает `os._exit(78)`. **Вызова `logger` на этом пути нет вообще.**

```python
if age > WATCHDOG_STALE_SECONDS:
    try:
        with open(WATCHDOG_DUMP_PATH, "a", ...) as dump_file:
            ...
    except Exception:
        pass
    os._exit(78)
```

**Замер.** В `bot_watchdog_dump.txt` — 14 записей `WATCHDOG EXIT reason=event_loop_heartbeat_stale_*`
(300.0 .. 330.0 с) плюс 6 `RUNTIME DUMP`. В bot.log про них **0 строк**: единственные строки со
словами watchdog/stale — `temp_media cleanup: removed 70 stale files` и отмены задач в тестовые дни.
Сопутствующий замер: **57 из 59 запусков процесса** в bot.log не имеют перед собой ни одной
строки о причине предыдущего завершения (причина есть только у двух: `forcing restart` и
`KeyboardInterrupt`).

**Сценарий отказа с данными.** 2026-05-19 13:45:46 журнал обрывается на
`🎞️ Извлечение первого кадра из видео 161192...`. Сторож убил pid 49776 в 13:51:03
(`event_loop_heartbeat_stale_326.9s`). Следующая строка bot.log — **2026-05-20 19:03:30**
`🚀 Инициализация базы данных...`. Бот лежал 29 часов. По журналу отличить это от отключения
света, ручной остановки, падения интерпретатора или убийства сторожем **невозможно**, и
`start.bat` его, очевидно, не поднял — но и об этом в журнале нет ничего.
Такой же рисунок у всех 14 случаев: обрыв журнала, ~5 минут тишины, `🚀 Инициализация`.

**Усугубление.** Оба писателя дампа гасят ошибку молча: `runtime_guard.py:207-208`
(`except Exception: pass`) и `runtime_guard.py:190-191` в `dump_runtime_state`. Если файл дампа
занят — а занятость файла и есть та причина, по которой на этой машине ломается запись
heartbeat (`main.py:409-414`), — записи об убийстве не останется **нигде**.

**Минимальная правка.** Перед `os._exit(78)` в `runtime_guard.py:209` записать
`logging.critical("watchdog exit: heartbeat stale %.1fs pid=%s dump=%s", age, os.getpid(), WATCHDOG_DUMP_PATH)`
и вызвать `logging.shutdown()` — `os._exit` не флашит обработчики.

---

## Находка 2. `media_tools` выбрасывает stderr дочернего процесса, в том числе при таймауте

`media_tools.py:96-109`, `_run_tool`:

```python
except asyncio.TimeoutError:
    proc.kill()
    await proc.communicate()          # <- прочитанный stderr не используется
    return None, f"{action} timeout"
...
if proc.returncode != 0 and not stdout:
    err_text = stderr.decode(...)     # <- stderr читается ТОЛЬКО здесь
```

Релея журнала ребёнка в `media_tools` нет ни одного (аналога `_relay_child_log` нет).

**Это не «может быть», а известный, уже описанный дефект, оставленный в одном из двух мест.**
`blocking_tools.py:220-227` — комментарий ровно про него: «второй `communicate()` после kill
возвращает ПУСТОЙ stderr, то есть журнал убитого ребёнка терялся целиком. А убийство по таймауту —
как раз тот случай, который потом и надо разбирать». Там это исправили дренажем в списки снаружи
корутины и `_relay_child_log` (`blocking_tools.py:140-161`). В `media_tools` осталось как было.

**Сценарий отказа.** PIL упирается в `Image.MAX_IMAGE_PIXELS = 49_000_000` (`media_tools.py:35`)
на крупном снимке КТ, либо cv2 висит на битом mp4 (`_extract_frame_sync`, `media_tools.py:63-84`).
Родитель получает строку `prepare-image timeout` / `extract-frame timeout`, дальше `main.py`
пишет `Ошибка обработки медиа <id>`. Трейсбек ребёнка, имя файла, стадия (открытие / resize /
кодирование) — уничтожены. Замер подтверждает: в bot.log **нет ни одной строки** со словом
`prepare-image` или `extract-frame`, то есть от этих детей журнал не получает вообще ничего.

**Минимальная правка.** Скопировать `_relay_child_log` из `blocking_tools.py:140-161` и вызвать
его на дренированном stderr во всех трёх ветвях `_run_tool` (таймаут, отмена, нормальный выход).

---

## Находка 3. Верхний обработчик дочернего процесса стирает трейсбек и тип исключения

`blocking_tools.py:623-624`:

```python
    except Exception as exc:
        _json_exit({"ok": False, "error": str(exc)}, 1)
```

`_json_exit` (`blocking_tools.py:14-18`) пишет только в **stdout** и бросает `SystemExit`. Журнал
ребёнка идёт в **stderr** (`blocking_tools.py:563-567`), и именно stderr перекачивается в bot.log.
То есть трейсбек падения ребёнка не попадает никуда: ни в stdout (там JSON-протокол, куда его
нельзя), ни в stderr (туда его не пишут).

**Сценарий отказа с данными.** Проба (`python`): `str(ConnectionResetError()) == ''`,
`str(TimeoutError()) == ''`, `str(IndexError()) == ''`, `str(asyncio.CancelledError()) == ''`.
Такое исключение внутри `_generate_gemini_text_sync` даёт `{"ok": false, "error": ""}`; родитель
на `blocking_tools.py:315` — `return None, result.get("error") or f"{action} failed"` — подставляет
`"gemini-text failed"`. В bot.log оператор читает `Gemini subprocess failed: gemini-text failed`:
ни типа исключения, ни файла, ни строки, ни модели, ни ключа. При этом весь смысл релея
(`blocking_tools.py:144-148`: «вопрос "почему бот не ответил" остался без ответа») именно в том,
чтобы этого не было.

**Минимальная правка.** Перед `_json_exit` вызвать `logger.exception("child crashed action=%s", action)`
(уйдёт в stderr и будет перелито родителем) и положить в поле error `f"{type(exc).__name__}: {exc}"`.

---

## Находка 4. `health_check` печатает причину как `%s` от исключения и получает пустоту — а он же и перезапускает бота

`main.py:2121-2128`:

```python
except Exception as exc:
    failure_count += 1
    logger.error("health_check failed %s/%s: %s", failure_count, HEALTH_FAILURE_LIMIT, exc)
```

**Замер.** В bot.log 4 записи `health_check failed N/3:` и **все 4 — с пустым полем причины**
(из 1393 записей ERROR/WARNING пустое поле причины ровно у 12: эти 4 плюс 8 `Ошибка обработки
медиа N:`, второй сайт уже переведён на `logger.exception`). Внутри защищаемого блока стоит
`await asyncio.wait_for(sync_history(), timeout=SYNC_HISTORY_TIMEOUT_SECONDS)` (`main.py:2118`) —
`asyncio.TimeoutError` здесь типовой, а его `str()` пуст.

**Сценарий отказа.** 2026-06-04 22:07:09 / 22:13:09 / 22:19:09 — три пустые строки
`health_check failed 1/3:`, `2/3:`, `3/3:`; на третьей `main.py:2130-2133` отключает клиентов и
уходит в перезапуск; далее в журнале пять подряд `fatal bot crash` с интервалом ~2 минуты
(22:21, 22:23, 22:25, 22:27, 22:29) и полное молчание до 2026-06-06 00:03. Оператор видит цикл
перезапусков, у которого первопричина стёрта одним `%s`.

**Класс, а не единичный случай.** AST-разбор 36 боевых модулей: **109 вызовов журнала печатают
только `str(исключения)` без типа и без `exc_info`** — assistant.py 63, main.py 24,
gemini_client.py 5, visionproc.py 4, summarizer.py 3, database.py 2, distiller.py 2,
search_engine.py 2, vision.py 2. Любой из них при исключении с пустым `str()` даёт запись
без причины.

**Минимальная правка.** `%r` вместо `%s` (или `type(exc).__name__`), а на пути, ведущем к
перезапуску, — `logger.exception`.

---

## Находка 5. 76 обработчиков в боевых модулях гасят ошибку насмерть — без записи и без переброса

AST-разбор 16 боевых модулей: 227 обработчиков `except`, из них **84 без единого вызова журнала**,
и **76 из этих 84 ещё и не перебрасывают исключение** — ошибка исчезает полностью.
По файлам (все 36 модулей, 94 немых обработчика): assistant.py 20, main.py 18,
blocking_tools.py 16, media_tools.py 8, runtime_guard.py 7, gemini_client.py 6, config.py 3,
database.py 1, vision.py 1, visionproc.py 1, prompter.py 1, search_engine.py 1.

Самые дорогие по последствиям:

1. **`main.py:1269-1270` и `main.py:1276-1277`** — `except Exception: pass` вокруг
   `await asyncio.wait_for(database.update_media_description(message.id, "-"), timeout=10)`.
   Это страховка «пометить медиа обработанным, чтобы не зациклиться» (её назначение прописано в
   самой строке журнала выше: `marking as processed to avoid loop`). Если страховка молча падает
   (БД занята, таймаут 10 с), сообщение остаётся необработанным и конвейер медиа берёт его снова
   на следующем круге — бесконечно. В журнале об этом **ни строки**: видно только повторяющееся
   `📸 Анализ медиа в сообщении <id>...` на один и тот же id.
   Правка: `logger.warning("не удалось пометить медиа обработанным msg_id=%s: %r", message.id, exc)`.

2. **`database.py:273-277`** — `except sqlite3.OperationalError: pass` на
   `ALTER TABLE messages ADD COLUMN media_remote_url`. Идиома рассчитана на «столбец уже есть», но
   тот же `OperationalError` покрывает `database is locked`, `disk I/O error`, `no such table:
   messages`. При заблокированной БД на старте миграция молча не проходит, `logger.info("database
   schema migrated...")` (строка 275) не печатается, и дальше каждое обращение к
   `media_remote_url` падает как ошибка записи, а не как непрошедшая миграция.
   Правка: пропускать молча только если `"duplicate column" in str(exc)`, иначе `logger.error`.

3. **`main.py:561-566`** — голый `except:` вокруг чтения `config.REPORT_TARGETS`, затем
   `targets = []`. При испорченном JSON в `.env` планировщик поднимается с нулём целей: чат врачей
   не получает дайджест никогда, а в журнале — одна строка INFO `📅 Планировщик активен. Целей: 0`
   и ноль ERROR. Правка: `except Exception as exc: logger.error("REPORT_TARGETS не разобран: %r", exc)`.

4. **`runtime_guard.py:190-191` и `:207-208`** — оба писателя дампа, см. находку 1.

5. **`assistant.py:1030`** (`check_and_apply_silence`), **`assistant.py:1780,1789,1834`**
   (`check_and_trigger_assistant_media`), **`assistant.py:4650`** (`check_and_trigger_referee`),
   **`assistant.py:5071,5133`** (`check_and_send_group_activity_pings`) — `except Exception: pass`
   в семи точках принятия решения «отвечать или молчать». Отказ любой из них выглядит для
   оператора идентично осознанному молчанию бота.

---

## Пункт 4 задания: следы отказов, которых никто не разбирал

Три постоянных, никем не закрытых отказа. Все три пишутся на уровне WARNING, повторяются
десятками раз, ни один не поднят до ERROR и ни один не имеет сводной записи «это конфигурация,
а не сбой»:

1. **44 записи** `recent delivery scan failed chat=-N topic=N: The API access for bot users is
   restricted. The method you tried to invoke cannot be executed as a bot` —
   `summarizer.py:174-176`. Это не сбой сети, а **навсегда** недоступный боту метод
   `client.get_messages` (`summarizer.py:170-172`). Значит защита от повторной отправки дайджеста
   («не отправлять, если такой текст уже есть в чате») **не работает никогда** — функция всегда
   возвращает `None`. По журналу это выглядит как временная неудача, которую можно пропустить.

2. **45+20 записей** `Gemini failed attempt=N/12 key=...XXXXX: 400 FAILED_PRECONDITION ... 'User
   location is not supported for the API use.'` — по всем четырём ключам. Это постоянная
   гео-блокировка; каскад честно перебирает 12 попыток × 4 ключа на ошибке, которая не может
   пройти ни при какой попытке.

3. **16 записей** `Groq fallback failed attempt=N key=...XXXXX: Error code: 413 - 'Request too
   large for model llama-3.3-70b-versatile'` -> сразу за ними `Groq fallback exhausted.`
   Промпт дайджеста систематически не влезает в лимит модели; отказ детерминированный, ретраи
   бесполезны.

---

## Опровергнутые гипотезы (проверил — не находка)

- **Тесты пишут в боевой bot.log.** Было, заткнуто: `runtime_guard.py:12-39`, `run_all_tests.py:31`
  и `:49`. Дыр не нашёл (pytest в проекте нет, оба пути запуска закрыты). Остался только
  непочищенный файл.
- **Терминальная ошибка каскада LLM не содержит причины.** Строки `Groq fallback exhausted.`,
  `❌ Все попытки исчерпаны. Gemini недоступен (N/N).`, `Gemini attempts exhausted` в журнале есть,
  но **в исходниках их больше нет**: заменены на `gemini_client.py:619`
  `"All AI attempts exhausted: reason=%s detail=%s"`, и комментарий `gemini_client.py:610` описывает
  ровно эту проблему как закрытую. Записи в bot.log — от старой версии.
- **`Ошибка обработки медиа N:` без причины.** 8 записей с пустым полем в журнале, но
  `main.py:1272` уже переведён на `logger.exception`. Исправлено.
- **`MESSAGE LOST` без причины / ретраи `save_message` бесполезны.** Обе гипотезы неверны.
  `database.py:458-463` использует `logger.exception`, трейсбек есть. Ретраи работают:
  46 записей `save_message recovered on attempt N` против 46 `MESSAGE LOST`; арифметика сходится
  (`SAVE_RETRY_ATTEMPTS = 3`, `database.py:14`): 46×2 + 46×1 = 138 warning. Всё это, впрочем,
  тестовые данные.
- **Дочерний процесс `blocking_tools` пишет в bot.log своим `RotatingFileHandler` и рвёт ротацию.**
  Нет: `blocking_tools.py:563-567` пишет в stderr, родитель перекачивает через `_relay_child_log`.
  Сделано осознанно, комментарий `:559-562`.
- **`background task cancelled` (145 записей) — потерянная работа врачей.** 144 из 145 — тестовые.
  В работе бота такая запись одна, и она рядом с `health_check forcing restart`, то есть объяснена.
  Само сообщение (`runtime_guard.py:242`) причины отмены не содержит, но повода для правки нет.

---

## Приоритет правок

1. Находка 1 — строка в журнал перед `os._exit(78)`. Без неё 14 из 14 самых тяжёлых отказов
   (зависание цикла событий) невидимы в журнале полностью.
2. Находка 4 — `%r`/`exc_info` на пути health_check -> перезапуск. Дешевле всего, срабатывает
   на уже наблюдавшемся цикле перезапусков.
3. Находка 2 — релей stderr в `media_tools`. Медиа — самый частый путь отказа в журнале.
4. Находка 3 — тип и трейсбек в верхнем обработчике `blocking_tools`.
5. Находка 5, пункты 1-3 — три конкретных `pass`, за которыми стоят зацикливание медиа,
   непрошедшая миграция и молчащий планировщик.
6. Один раз усечь bot.log, чтобы 4302 строки тестовой выдумки не читались как телеметрия.
