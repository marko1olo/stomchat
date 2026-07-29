"""
Покрытие рецензентом: он стоял на 2 путях из 12, где ответ модели видит врач.

Разбор по AST всех функций assistant.py: двенадцать генерируют ответ модели и
отправляют его, рецензент check_response_quality вызывался в двух. Без проверки
уходили в том числе:

  * ответ в ЛИЧНЫХ СООБЩЕНИЯХ — главный клинический путь продукта: врач
    описывает свой случай и действует по ответу;
  * прямое обращение к боту в ОБЩЕМ чате — ответ читают все коллеги;
  * вопрос боту командой в общем чате.

Рецензент — единственное, что стоит между выдуманной дозировкой и врачом,
который по ней работает. Его правило 3.1 прямо отклоняет ответ с цифрами,
которых нет ни в справке, ни в общепризнанных стандартах.

Отдельно проверяется класс ошибки, на котором я споткнулся при этой же правке:
ссылка на переменную, которой в области видимости нет. Такая ошибка не видна ни
при импорте, ни в тестах, которые до этой ветки не доходят, — она падает у врача
в момент ответа.

Ничего не вызывается и не отправляется: проверка статическая, по AST и коду.

Запуск: python test_validator_coverage.py
"""
import ast
import io
import os
import sys
import tempfile

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

os.environ["STOMCHAT_LOG_PATH"] = os.path.join(tempfile.mkdtemp(prefix="stomchat_vc_"), "t.log")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


SOURCE = io.open("assistant.py", encoding="utf-8").read()
LINES = SOURCE.split("\n")
TREE = ast.parse(SOURCE)


def body_of(fn):
    return "\n".join(LINES[fn.lineno - 1:getattr(fn, "end_lineno", fn.lineno)])


FUNCS = {fn.name: fn for fn in ast.walk(TREE)
         if isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef))}

# Пути, которые генерируют ответ модели И отправляют его.
answering = {}
for name, fn in FUNCS.items():
    seg = body_of(fn)
    if "generate_gemini_text_async" in seg and ("send_message" in seg or "event.reply" in seg):
        answering[name] = "check_response_quality" in seg

print("\n[1] Клинические пути проверяются рецензентом")
# Здесь врач задаёт вопрос про свой случай и действует по ответу.
CLINICAL = [
    ("handle_private_message", "ответ в личных сообщениях"),
    ("check_bot_mention_trigger", "прямое обращение к боту в чате"),
    ("handle_group_direct_ask", "вопрос боту командой в чате"),
    ("check_and_trigger_assistant", "пассивный ответ в чате"),
    ("check_and_trigger_assistant_media", "разбор снимка в чате"),
]
for name, human in CLINICAL:
    check(f"{human} проверяется", answering.get(name) is True,
          f"{name}: ответ уходит врачу без проверки на клиническую обоснованность")

print("\n[2] Рецензент получает справку, на которой строился ответ")
# Без справки он не отличает число из базы знаний от выдуманного и валит
# верные ответы: до правки окна он видел 53% справки и считал каждую пятую
# законную цифру взятой с потолка.
for name, human in CLINICAL:
    fn = FUNCS.get(name)
    if not fn:
        continue
    seg = body_of(fn)
    call = seg.split("check_response_quality(", 1)[1][:400] if "check_response_quality(" in seg else ""
    check(f"{human}: справка передана", "reference=" in call,
          "рецензент судит о цифрах, не видя основания")

print("\n[3] Прямой вопрос врача не глохнет при недоступном рецензенте")
# Политика: явный отказ (ok:false) глушит всегда, а НЕДОСТУПНОСТЬ рецензента на
# приглашённом пути пропускает — молча проигнорировать вопрос хуже, чем отдать
# текст, прошедший EBM-правила основного промпта.
for name, human in CLINICAL[:3]:
    fn = FUNCS.get(name)
    seg = body_of(fn) if fn else ""
    call = seg.split("check_response_quality(", 1)[1][:400] if "check_response_quality(" in seg else ""
    check(f"{human}: помечен как приглашённый", "invited=True" in call,
          "при недоступном рецензенте вопрос врача пропадёт молча")

print("\n[4] Отказ рецензента виден врачу, а не проглочен")
for name, human in (("handle_private_message", "в личных сообщениях"),
                    ("handle_group_direct_ask", "на вопрос в чате")):
    fn = FUNCS.get(name)
    seg = body_of(fn) if fn else ""
    tail = seg.split("check_response_quality(", 1)[1] if "check_response_quality(" in seg else ""
    tail = tail[:1200]
    check(f"отказ {human} сопровождается сообщением",
          "send_message" in tail and "не прошёл" in tail,
          "врач не поймёт, дошёл ли его вопрос вообще")

print("\n[5] Ссылки на переменные в новых вызовах существуют")
# Класс ошибки, на котором я споткнулся при этой правке: context_msgs не
# существует в check_bot_mention_trigger и handle_group_direct_ask. Такая ошибка
# не видна ни при импорте, ни в тестах, которые до ветки не доходят — она падает
# у врача в момент ответа.
missing_total = 0
for name in [c[0] for c in CLINICAL]:
    fn = FUNCS.get(name)
    if not fn or "check_response_quality(" not in body_of(fn):
        continue
    defined = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    defined |= {a.arg for a in ast.walk(fn) if isinstance(a, ast.arg)}
    defined |= set(dir(__builtins__)) if isinstance(__builtins__, type(sys)) else set()
    call_src = body_of(fn).split("check_response_quality(", 1)[1].split(")", 1)[0]
    used = {n.id for n in ast.walk(ast.parse(f"f({call_src})", mode="eval"))
            if isinstance(n, ast.Name)} - {"f"}
    missing = sorted(v for v in used if v not in defined and not v.isupper())
    missing_total += len(missing)
    check(f"{name}: все переменные вызова определены", not missing, f"нет: {missing}")
check("необъявленных ссылок нет ни в одном вызове", missing_total == 0)

print("\n[6] Проверки выше ловят поломку")
check("разбор находит пути с генерацией и отправкой", len(answering) >= 10,
      f"найдено {len(answering)} — разбор сломан, а не код чист")
check("непокрытые пути видны разбору",
      any(v is False for v in answering.values()),
      "все пути покрыты — тогда проверка [1] ничего не значит, обнови ожидания")
print(f"      покрыто рецензентом: {sum(1 for v in answering.values() if v)} из {len(answering)}")
uncovered = sorted(n for n, v in answering.items() if not v)
print(f"      осталось без проверки: {', '.join(uncovered)}")
# Осознанно не покрыты: викторина и разбор кейса — там модель не отвечает на
# клинический вопрос врача, а ведёт экзамен; пинги и итог чата не содержат
# клинических утверждений от себя.
check("непокрытыми остались только неклинические и экзаменационные пути",
      set(uncovered) <= {"handle_interactive_case_step", "handle_group_summary",
                         "handle_group_quiz", "check_and_trigger_referee",
                         "handle_term_explainer", "check_and_send_pm_pings",
                         "check_and_send_group_activity_pings"},
      f"без проверки остался клинический путь: {uncovered}")

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
