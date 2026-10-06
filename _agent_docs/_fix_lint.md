# Лана lint — ruff до нуля по классам, которые могут скрывать дефект

Файлы во владении: `test_fix_db.py`, `test_llm_failover.py`, `test_media_selection.py`,
`test_round3.py`, `test_tg_delivery.py`, `test_wiki_pagination.py`, `videosi.py`, `visionproc.py`.

Обход всегда с исключением вложенной копии:
`ruff check --exclude stomchat`, `rg -g '!stomchat' -g '!uploaded_media' -g '!.git' -g '!*.log'`.

---

## 1. Замер ДО правок (ruff 0.15.16, живое дерево)

```
python -m ruff check --exclude stomchat --select F401,F841,F811,F821,E711,E712,E714,F632 .
Found 33 errors.
```

### 1.1 Опасные классы — замер лида ПОДТВЕРЖДЁН частично, с одним расхождением

Лид: «опасные классы (F821, F811, E711/E712/E714, F632) по живому дереву: ровно ОДИН».
Замер: по этим пяти кодам действительно **ровно один** — `assistant.py:1729:55 E712`
(сравнение с `True` по `parent_rows[0][0]`, файл не мой, разобран лидом).

Но **F841 лид посчитал не полностью**. Лид: «F841: test_fix_db.py дважды переменная path».
Замер по живому дереву — F841 **четыре**, не два:

| файл:строка | переменная | мой файл |
|---|---|---|
| `test_fix_db.py:242` | `path` | да |
| `test_fix_db.py:476` | `path` | да |
| `assistant.py:1784` | `last_text_lower` | **нет** |
| `assistant.py:4023` | `address` | **нет** |

Два F841 в `assistant.py` в задании не названы. Мёртвая переменная в боевом
`assistant.py` — ровно тот класс «потерянного значения», из-за которого лана и
существует. Разбор — в разделе «для лида» ниже; править не могу, файл не мой.

### 1.2 F401/F841 в МОИХ файлах — замер лида подтверждён полностью

| файл:строка | имя |
|---|---|
| `test_fix_db.py:242`, `:476` | `path` (F841) |
| `test_llm_failover.py:20` | `time` |
| `test_media_selection.py:41` | `assistant` (as `A`) |
| `test_round3.py:6`, `:8` | `asyncio`, `os` |
| `test_tg_delivery.py:36`, `:43` | `io`, `timedelta` |
| `test_wiki_pagination.py:30` | `config` |
| `videosi.py:15` | `time` |
| `visionproc.py:6`, `:7` | `httpx`, `base64` |

Итого 11 замечаний в 8 файлах. Ни одного F821/F811/E7xx/F632 в моих файлах.

### 1.3 Прогоны ДО (полные, ноль провалов)

| набор | PASSED | FAILED |
|---|---|---|
| `test_fix_db.py` | 72 | 0 |
| `test_llm_failover.py` | 61 | 0 |
| `test_media_selection.py` | 31 | 0 |
| `test_round3.py` | 34 | 0 |
| `test_tg_delivery.py` | 71 | 0 |
| `test_wiki_pagination.py` | 40 | 0 |
| `test_import_safety.py` (сосед: сканирует `videosi.py`/`visionproc.py`) | 461 | 0 |

Расхождение с заданием: лид назвал `test_import_safety.py` «436 проверок»,
замер — **461** (разобрано файлов: 238 из 238). Число в задании устарело.

### 1.4 md5-префиксы ДО

```
test_fix_db.py           7ae9f94b53
test_llm_failover.py     39157b6166
test_media_selection.py  59ca93404a
test_round3.py           8c0d23ab68
test_tg_delivery.py      434c8f26cb
test_wiki_pagination.py  51bf03a30d
videosi.py               e414198aee
visionproc.py            e420ecc61e
```

---

## 2. Разбор каждого замечания ДО удаления

### 2.1 `test_fix_db.py:242`, `:476` — `path` — НЕ потерянное значение

`fresh_db(name)` (строка 136) делает главное побочным эффектом:

```python
config.DB_PATH = path        # ради этого её и вызывают
return path
```

Путь нужен девяти секциям, которые лезут в базу напрямую (`sqlite3.connect(path)`,
`plan_of(path, ...)`). Секции `[4]` (окно последних сообщений) и `[10]`
(неразобранные снимки) ходят только через `database.*`, и путь им не нужен.
Значение не потеряно — оно и не требуется.

Правка: убран только `path = `, вызов оставлен, над ним комментарий с ценой
неверного «исправления». Это не украшение: снести строку целиком — ровно то,
что сделает следующий читатель, увидев «мёртвая переменная». Тогда секция `[4]`
пойдёт по базе секции `[3]`, где уже лежит 1000 строк ветки, а секция `[10]` —
по базе закладок. Проверка «окно не теряет реплику врача» перестанет проверять
окно, оставшись зелёной. Диверсии A и B ниже это измеряют.

### 2.2 `test_media_selection.py:41` — `import assistant as A` — побочного эффекта НЕТ

Задание отметило этот импорт как особенно подозрительный. Замер отпечатками
интерпретатора (`_probe_lint_sideeffect.py`, удалён после замера — иначе он сам
попадает в `os.listdir` сканера `test_import_safety`), два прогона, с импортом и
без:

| отпечаток | с `import assistant` | без |
|---|---|---|
| `assistant` в `sys.modules` | True | True |
| модулей верхнего уровня загружено | 177 | 177 |
| порядок проектных модулей | config, runtime_guard, vision, database, assistant, main, media_tools, gemini_client, html_safe | тот же |
| публичных имён в `assistant` | 139 | 139 |
| `main.assistant is sys.modules['assistant']` | True | True |

Причина: `main.py:32` — `import assistant`. Порядок тоже не меняется, потому что
сам `assistant` тянет config/runtime_guard/vision/database первым. Сам тест
читает `assistant.py` как ТЕКСТ (`io.open("assistant.py")`, строка 115), а не
через модуль, — имя `A` было не нужно ни для чего.

### 2.3 `test_wiki_pagination.py:30` — `import config` — порядок не значим

Стоял до `import assistant` (строка 35), но `assistant` импортирует `config` сам,
а `os.chdir(_TMPDIR)` происходит только на строке 140 — то есть `.env` в обоих
случаях читается из каталога репозитория. Прогон подтвердил: 40/0 до и после.

### 2.4 `visionproc.py:6,7` — `httpx`, `base64` — остатки удалённого пути

Строка 94 уже фиксирует: «Функция process_with_groq удалена в пользу
vision.describe_image». `httpx` (свой HTTP-запрос) и `base64` (кодировка
картинки) были инструментами именно этой функции. Сейчас и то и другое живёт в
`vision.py` — `vision.describe_image` кодирует картинку сама
(`base64.b64encode`) и держит свой `httpx.Timeout`. В `visionproc.py` ни одно из
двух имён не встречается больше нигде (замер `rg`, подтверждён AST ruff).

Проверено отдельно, что вызов на строке 175 корректен: `describe_image(file_paths, ...)`
принимает и одиночную строку — `if isinstance(file_paths, str): file_paths = [file_paths]`.
Дефекта «строку итерируют по символам» здесь нет.

Комментарий на месте удаления оставлен с последствием: завести httpx/base64
здесь снова — значит завести второй путь в Vision, минующий ротацию ключей и
кулдауны, и тогда снимок врача молча останется без описания на первом лимите.

### 2.5 `videosi.py:15` — `time` — остаток, паузы живут на `asyncio.sleep`

Пауза между протоколами на месте: `await asyncio.sleep(SLEEP_BETWEEN_VIDEOS)`
(строки 274 и 301) и `await asyncio.sleep(1)` (строка 202). Ни одного
`time.sleep`/`time.time` в файле нет. Троттлинг API не потерян.

### 2.6 `test_llm_failover.py:20`, `test_round3.py:6,8`, `test_tg_delivery.py:36,43`

Чистые остатки stdlib (`time`, `asyncio`, `os`, `io`, `timedelta`), побочных
эффектов у импорта stdlib нет; ни один не используется. Доказательство — прогоны
ниже, а не осмотр.

---

## 3. Что изменено

| файл | правка |
|---|---|
| `test_fix_db.py` | 2x снят `path = ` перед `fresh_db(...)`, вызов оставлен, 2 комментария с ценой неверного удаления |
| `test_llm_failover.py` | снят `import time` |
| `test_media_selection.py` | снят `import assistant as A` |
| `test_round3.py` | снят `import asyncio`, `import os` |
| `test_tg_delivery.py` | снят `import io`; `from datetime import datetime, timedelta` -> `... import datetime` |
| `test_wiki_pagination.py` | снят `import config` |
| `videosi.py` | снят `import time` |
| `visionproc.py` | снят `import httpx`, `import base64`; комментарий на строке 94 расширен последствием |

`ruff --exclude stomchat --select F401,F841,F811,F821,E711,E712,E714,F632` по всем
восьми файлам: **All checks passed!** Каждый файл компилируется (`py_compile`, 8/8 OK).

---

## 4. Прогоны ДО и ПОСЛЕ

| набор | ДО | ПОСЛЕ | дельта |
|---|---|---|---|
| `test_fix_db.py` | 72 / 0 | 72 / 0 | 0 |
| `test_llm_failover.py` | 61 / 0 | 61 / 0 | 0 |
| `test_media_selection.py` | 31 / 0 | 31 / 0 | 0 |
| `test_round3.py` | 34 / 0 | 34 / 0 | 0 |
| `test_tg_delivery.py` | 71 / 0 | 71 / 0 | 0 |
| `test_wiki_pagination.py` | 40 / 0 | 40 / 0 | 0 |
| `test_import_safety.py` | 472 / 0 | 472 / 0 | 0 (парный прогон, см. ниже) |

### 4.1 Почему `test_import_safety` пришлось мерить парно

Первый замер дал 461 при 238 разобранных файлах, повторный — 472 при 248. Прирост
**не мой**: сканер берёт `os.listdir(".")` по `*.py`, а соседние ланы в эту волну
добавляют файлы прямо сейчас. Один такой файл добавил и я сам (зонд), поэтому
зонд удалён.

Честный замер — парный прогон в одной и той же папке, где различаются только мои
восемь файлов (текущие против `.bak`):

```
AFTER  : 472 ok, 0 fail, разобрано файлов: 248 из 248
BEFORE : 472 ok, 0 fail, разобрано файлов: 248 из 248
diff по строкам, где упомянуты мои файлы — одна строка:
<       visionproc.py создаёт при импорте: [(38, 'os.makedirs'), (40, 'os.makedirs')]
>       visionproc.py создаёт при импорте: [(36, 'os.makedirs'), (38, 'os.makedirs')]
```

Это диагностическая ПЕЧАТЬ, не проверка (`test_import_safety.py:403-405`: результат
`module_level_mutations(tree, CREATORS)` только печатается). Номера сдвинулись на
два, потому что я снял две строки импорта. Мой дельта по проверкам — ровно 0.
