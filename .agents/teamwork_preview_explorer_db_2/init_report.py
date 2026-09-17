import json
import sys

sys.stdout.reconfigure(encoding='utf-8')

with open("c:/Users/danat/Desktop/stomchat/.agents/teamwork_preview_explorer_db_2/weekend_dump.json", "r", encoding="utf-8") as f:
    data = json.load(f)

messages = data["messages"]
memories = data["memories"]
profiles = data["profiles"]
msg_map = {m["msg_id"]: m for m in messages}

def get_sender_info(m):
    sid = m["sender_id"]
    mem = memories.get(str(sid), {})
    spec = mem.get("specialty", "N/A")
    return f"**{m['sender_name']}** (@{m['sender_username'] or 'N/A'}, ID: `{sid}`, {spec})"

print("Writing report_db.md...")

report_lines = []

def p(line=""):
    report_lines.append(line)

p("# ЭМПИРИЧЕСКИЙ АУДИТ ТЕЛЕМЕТРИИ STOMAT_BOT.DB (11–13 СЕНТЯБРЯ 2026)")
p("## Комплексный анализ взаимодействия бота StomChat, 9 клинических тредов, профилей врачей и динамики подавления")
p()
p("> **Рабочая директория:** `c:\\Users\\danat\\Desktop\\stomchat\\.agents\\teamwork_preview_explorer_db_2`  ")
p("> **База данных:** `stomat_bot.db` (read-only SQLite query)  ")
p("> **Аудируемый период:** 11 сентября 2026, 02:23:12 UTC — 13 сентября 2026, 11:20:41 UTC (выходные)  ")
p("> **Статус рантайма:** Zero-error health (0 критических ошибок в bot.log)  ")
p()
p("---")
p()
p("## 1. ИСПОЛНИТЕЛЬНОЕ РЕЗЮМЕ И СТАТИСТИКА ТЕЛЕМЕТРИИ")
p()
p("За период с 11 по 13 сентября 2026 года в рабочем чате ортопедов и терапевтов StomChat зафиксировано **200 сообщений**:")
p("- **180 сообщений врачей-клиницистов** (32 уникальных доктора).")
p("- **20 сообщений бота (`@docendobot`)**:")
p("  - **16 прямых диалоговых ответов** в рамках 9 структурированных клинических тредов.")
p("  - **4 сервисных дайджестных сообщения** (2 ежедневных вечерних дайджеста от 11 и 12 сентября, разбитых на 2 части каждый: `177300/177301` и `177415/177416`).")
p()
p("### Сводные показатели телеметрии")
p()
p("| Метрика | Значение | Примечание |")
p("| :--- | :--- | :--- |")
p("| **Всего сообщений в выборке** | 200 | Диапазон Msg ID: 177243 — 177445 |")
p("| **Сообщений врачей** | 180 | 32 практикующих специалиста |")
p("| **Всего реплик бота** | 20 | 16 клинических ответов + 4 части дайджестов |")
p("| **Клинических диалоговых тредов** | 9 | 100% детально деконструированы ниже |")
p("| **Уникальных врачей в памяти (`user_memories`)** | 30 из 32 (93.8%) | Глубокие клинические досье и стилистические профили |")
p("| **Ошибок рантайма (`ERROR`/`CRITICAL`)** | 0 | 100% аптайм без падений процесса |")
p("| **Срабатываний пассивного гейта (Suppression)** | 64 | 63 `passive cooldown`, 1 `retry backoff` |")
p("| **Каскадных переключений Gemini 503 (Fallback)** | 27–29 | Автоматический откат без срыва ответа |")
p("| **Выявленная уязвимость Race Condition** | 1 случай | Msg ID `177390` и `177392` (двойной ответ бота за 18 сек) |")
p()
p("---")
p()
p("## 2. ДЕТАЛЬНЫЙ РАЗБОР ВСЕХ 9 МНОГОШАГОВЫХ ДИАЛОГОВЫХ ТРЕДОВ")
p()

# We will append the text for each of the 9 threads
with open("c:/Users/danat/Desktop/stomchat/.agents/teamwork_preview_explorer_db_2/report_db.md", "w", encoding="utf-8") as f:
    f.write("\n".join(report_lines))

print("Initial report structure created.")
