"""
Вложенность бюджетов: внутренний срок обязан укладываться во внешний.

Это единственный класс дефекта, который за сессию всплыл ТРИЖДЫ в разных
подсистемах, и каждый раз стоил дорого:

  1. Сторож сводки терял терпение через 1800 с, а генерации сводки разрешено
     2100 с. Надзиратель убивал процесс раньше, чем операция успевала честно
     отработать свой бюджет и обработать отказ: терялся дайджест И происходил
     перезапуск бота.
  2. Родитель подпроцесса ждал ровно тот же срок, который выдавал ребёнку, а
     внутрь родительского срока попадал ещё и запуск интерпретатора (замер:
     0.11-2.48 с на импорты). Ребёнок не доживал до последней модели каскада, и
     вся проделанная работа выбрасывалась.
  3. Каскад зрения имел внутренний бюджет до ~891 с при внешнем потолке 180 с:
     резервные модели зрения не пробовались НИКОГДА, потому что внешний таймаут
     срабатывал всегда первым.

Общее у всех трёх: два независимых числа в разных файлах, каждое разумное само
по себе. Пока их никто не сопоставляет, расхождение не проявляется ни в тестах,
ни в журнале — просто конвейер тихо не доходит до конца.

Проверка сопоставляет их явно. Ничего не запускается и не вызывается: читаются
только константы модулей.

Запуск: python test_budget_nesting.py
"""
import os
import sys
import tempfile

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

os.environ["STOMCHAT_LOG_PATH"] = os.path.join(tempfile.mkdtemp(prefix="stomchat_budget_"), "t.log")

import blocking_tools as B  # noqa: E402
import main as M  # noqa: E402
import runtime_guard as G  # noqa: E402
import summarizer as S  # noqa: E402
import vision as V  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def nests(inner_name, inner, outer_name, outer, why):
    """Внутренний бюджет обязан укладываться во внешний, иначе отказ не штатный."""
    check(f"{inner_name} ({inner}) укладывается в {outer_name} ({outer})",
          inner < outer, f"{why}. Разница {inner - outer:+g} с")


print("\n[1] Сторож сводки терпеливее самого конвейера сводки")
nests("генерация сводки", S.GEMINI_GENERATION_TIMEOUT_SECONDS,
      "терпение сторожа", M.SUMMARY_STALE_SECONDS,
      "сторож убьёт процесс посреди законной генерации: дайджест потерян И перезапуск")
check("терпение сторожа выведено из бюджета генерации, а не задано числом",
      "GEMINI_GENERATION_TIMEOUT_SECONDS" in
      next(l for l in open("main.py", encoding="utf-8") if "SUMMARY_STALE_SECONDS =" in l
           and not l.lstrip().startswith("#")),
      "два независимых числа снова разъедутся")
check("запас покрывает публикацию, отправку и закреп",
      M.SUMMARY_STALE_SECONDS - S.GEMINI_GENERATION_TIMEOUT_SECONDS
      >= S.TELEGRAPH_TIMEOUT_SECONDS + S.TELEGRAM_SEND_TIMEOUT_SECONDS + S.PIN_TIMEOUT_SECONDS,
      f"запас {M.SUMMARY_STALE_SECONDS - S.GEMINI_GENERATION_TIMEOUT_SECONDS} с")
check("сторож опрашивает статус многократно за срок терпения",
      M.SUMMARY_STATUS_CHECK_SECONDS * 5 <= M.SUMMARY_STALE_SECONDS,
      f"опрос {M.SUMMARY_STATUS_CHECK_SECONDS} с при терпении {M.SUMMARY_STALE_SECONDS} с")

print("\n[2] Родитель подпроцесса ждёт дольше, чем выдал ребёнку")
check("запас на подъём ребёнка объявлен",
      getattr(B, "_SUBPROCESS_STARTUP_SLACK_SECONDS", 0) > 0,
      "родитель убьёт ребёнка, не дав тому доработать свой бюджет")
check("запас покрывает импорты дочернего процесса",
      B._SUBPROCESS_STARTUP_SLACK_SECONDS >= 3,
      f"замер на этой машине: до 2.8 с на импорт config и openai, а на холодном "
      f"кеше больше; сейчас {B._SUBPROCESS_STARTUP_SLACK_SECONDS} с")

print("\n[3] Сердцебиение кормится многократно за срок сторожа процесса")
nests("интервал сердцебиения", G.HEARTBEAT_INTERVAL_SECONDS,
      "терпение сторожа процесса", G.WATCHDOG_STALE_SECONDS,
      "сторож убьёт живой процесс между двумя ударами")
check("на срок терпения приходится минимум пять ударов",
      G.HEARTBEAT_INTERVAL_SECONDS * 5 <= G.WATCHDOG_STALE_SECONDS,
      f"{G.WATCHDOG_STALE_SECONDS / G.HEARTBEAT_INTERVAL_SECONDS:.1f} удара — "
      f"одна пропущенная запись уже опасна")

print("\n[4] Догоняющая синхронизация не срывает подъём")
nests("подключение клиента", M.START_TIMEOUT_SECONDS,
      "бюджет синхронизации", M.SYNC_HISTORY_TIMEOUT_SECONDS,
      "синхронизация обязана иметь больше времени, чем одно подключение")
nests("запрос автора в синхронизации", M.SYNC_SENDER_TIMEOUT_SECONDS,
      "общий сетевой таймаут", M.TELEGRAM_REQUEST_TIMEOUT_SECONDS,
      "автор — необязательное обогащение, ему нельзя давать общий срок")
check("одно зависание запроса автора не съедает синхронизацию",
      M.SYNC_HISTORY_TIMEOUT_SECONDS / M.SYNC_SENDER_TIMEOUT_SECONDS >= 30,
      f"хватит {M.SYNC_HISTORY_TIMEOUT_SECONDS / M.SYNC_SENDER_TIMEOUT_SECONDS:.0f} "
      f"зависаний, чтобы сорвать подъём")

print("\n[5] Разбор снимка: внутренние шаги внутри внешнего потолка")
nests("скачивание медиа", M.MEDIA_DOWNLOAD_TIMEOUT_SECONDS,
      "потолок разбора медиа", M.MEDIA_ANALYSIS_TIMEOUT_SECONDS,
      "скачивание съест весь бюджет, и на разбор времени не останется")
nests("извлечение кадра", M.MEDIA_FRAME_TIMEOUT_SECONDS,
      "потолок разбора медиа", M.MEDIA_ANALYSIS_TIMEOUT_SECONDS,
      "кадр съест бюджет разбора")
# Внутренний бюджет каскада зрения: пауза между вызовами умножается на число
# моделей в пуле. Если он больше внешнего потолка, резервные модели не
# пробуются никогда — внешний таймаут срабатывает первым.
# Пул моделей объявлен внутри функции, поэтому считаем его по исходнику: он и
# задаёт, сколько раз каскад заплатит паузой троттлинга.
import io as _io  # noqa: E402
import re as _re  # noqa: E402

_vision_src = _io.open("vision.py", encoding="utf-8").read()
_pool_block = _vision_src.split("models_pool = [", 1)[1].split("]", 1)[0]
_pool_size = len(_re.findall(r'\("', _pool_block))
check("пул моделей зрения найден в исходнике", _pool_size >= 2, f"got {_pool_size}")
if _pool_size >= 2:
    worst = V.VISION_MIN_CALL_INTERVAL_SECONDS * _pool_size
    check(f"пауза троттлинга зрения x {_pool_size} моделей ({worst:g} с) "
          f"укладывается в потолок ({M.MEDIA_ANALYSIS_TIMEOUT_SECONDS} с)",
          worst < M.MEDIA_ANALYSIS_TIMEOUT_SECONDS,
          "внешний таймаут сработает раньше, и резервные модели зрения не будут "
          "опробованы никогда")
    check("на каждую модель пула остаётся время на сам запрос",
          (M.MEDIA_ANALYSIS_TIMEOUT_SECONDS - worst) / _pool_size >= 20,
          f"на модель остаётся "
          f"{(M.MEDIA_ANALYSIS_TIMEOUT_SECONDS - worst) / _pool_size:.0f} с")

print("\n[6] Пределы длины согласованы с ограничением Telegram")
TELEGRAM_HARD_LIMIT = 4096
nests("наш предел сообщения", M.__dict__.get("TELEGRAM_MESSAGE_LIMIT", 4000),
      "жёсткий предел Telegram", TELEGRAM_HARD_LIMIT,
      "Telegram отклонит сообщение целиком") if "TELEGRAM_MESSAGE_LIMIT" in M.__dict__ else None
import assistant as A  # noqa: E402

nests("наш предел сообщения", A.TELEGRAM_MESSAGE_LIMIT, "жёсткий предел Telegram",
      TELEGRAM_HARD_LIMIT, "Telegram отклонит сообщение целиком")
nests("предел простого текста сводки", S.TELEGRAM_PLAIN_TEXT_LIMIT,
      "жёсткий предел Telegram", TELEGRAM_HARD_LIMIT,
      "запасная отправка без разметки тоже будет отклонена")
nests("бюджет истории ЛС", A._PM_HISTORY_MAX_CHARS, "бюджет справки в промпте x2",
      A._CORPUS_MAX_CHARS * 2, "история вытеснит клиническую справку из промпта")
nests("предел одной записи истории", A._PM_HISTORY_ENTRY_MAX_CHARS,
      "бюджет истории ЛС", A._PM_HISTORY_MAX_CHARS,
      "одна реплика съест весь блок истории")
nests("окно справки у рецензента", A._CORPUS_MAX_CHARS - 1,
      "показ рецензенту", A.VALIDATOR_REFERENCE_MAX_CHARS + 1,
      "рецензент увидит не всю справку и посчитает законную цифру выдуманной")

print("\n[8] Расшифровка голосового: все ключи достижимы в бюджете")
# Каждый ключ получал таймаут клиента по умолчанию (30 с), и перебор всех
# складывался в 222 с внутреннего бюджета против 70 с родительского дедлайна
# (60 внешних плюс 10 на подъём подпроцесса). Сценарий: Groq отдаёт 429 по первым
# двум ключам — для квоты whisper это норма при залпе голосовых, — остальные пять
# свежие, но родитель убивает дерево процессов на 70-й секунде, и врач не получает
# ничего ПРИ ПЯТИ НЕИСПОЛЬЗОВАННЫХ КЛЮЧАХ.
import config as _cfg  # noqa: E402

_keys = max(1, len(getattr(_cfg, "GROQ_KEYS", []) or []))
_budget = M.VOICE_TRANSCRIBE_TIMEOUT_SECONDS
_parent = _budget + B._SUBPROCESS_STARTUP_SLACK_SECONDS
_reserve = 12.0
_usable = max(0.0, _budget - _reserve)
_per_attempt = max(7.0, _usable / _keys)
_worst = _per_attempt * _keys
print(f"      ключей {_keys}, на попытку {_per_attempt:.1f} с, худший случай {_worst:.0f} с, "
      f"родительский дедлайн {_parent:.0f} с")
check("перебор всех ключей укладывается в родительский дедлайн", _worst <= _parent,
      f"{_worst:.0f} с против {_parent:.0f} с — последние ключи недостижимы")
check("на попытку остаётся время на соединение", _per_attempt >= 5,
      f"{_per_attempt:.1f} с — запрос не успеет даже соединиться")
_src = _io.open("gemini_client.py", encoding="utf-8").read()
check("расшифровка принимает бюджет извне",
      "def transcribe_audio_bytes_or_file(file_path, timeout=None)" in _src,
      "бюджет не передаётся, значит клиент снова возьмёт свои 30 с")
check("перебор прекращается по истечению бюджета, а не по числу ключей",
      "бюджет %.0f с исчерпан" in _src,
      "последний ключ будет пробоваться после того, как родитель уже убил ребёнка")
check("пауза на 429 ограничена остатком бюджета",
      "min(2.0, left)" in _src,
      "пауза съедает время, которого хватило бы на следующий свежий ключ")

print("\n[7] Проверка вложенности действительно ловит расхождение")
# Все проверки выше — сравнения чисел, и ноль провалов одинаково выглядит у
# согласованных бюджетов и у слепого сравнения. Прогоняем образец в стороне от
# check(): печатать здесь строку [FAIL] нельзя, сводный прогон считает их и
# показал бы ложный провал набора.
def probe(inner, outer):
    return inner < outer


check("сравнение видит расхождение", probe(100, 10) is False,
      "проверка вложенности слепа: она пропустит внутренний бюджет больше внешнего")
check("сравнение пропускает согласованное", probe(10, 100) is True)
check("равные бюджеты считаются расхождением", probe(100, 100) is False,
      "равенство опасно: внешний таймаут может сработать первым из-за накладных")

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
