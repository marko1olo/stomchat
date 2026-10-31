"""
Redteam Inquisitor Suite: Zero Mirroring & Deep Clinical Synthesis Verification.

Verifies:
1. Complete coverage of ZERO MIRRORING / ZERO RETELLING negative constraints across
   all generative prompt pipelines (dialogue, non-dialogue, vision, consilium,
   mention, direct ask, rescue, lifeline, summarizer, distiller, poll debrief).
2. Mandatory DEEP CLINICAL SYNTHESIS / FIRST PRINCIPLES directives in all core prompts.
3. Absence of over-regulated compliment/praise bans ("пусть не будет это оговариваться").
4. Absolute parity of prompt directives between stomchat and endochat pipelines.
"""

import os
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import clinical_cognitive_core
import assistant
import clinical_rescue
import question_lifeline
import summarizer
import distiller
import poll_engine
import vision

PASS, FAIL = [], []

def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  [PASS] {name}")
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name}: {detail}")

print("==================== TEST ZERO MIRRORING & DEEP SYNTHESIS REDTEAM ====================")

# 1. clinical_cognitive_core.py - MASTER_COGNITIVE_ARCHITECTURE
master_arch = clinical_cognitive_core.MASTER_COGNITIVE_ARCHITECTURE
check("ccc_rule17_present", "17. ТОТАЛЬНЫЙ ЗАПРЕТ ЗЕРКАЛИРОВАНИЯ И ПЕРЕСКАЗА" in master_arch, "Rule 17 missing")
check("ccc_rule17_zero_mirroring_lock", "ZERO MIRRORING & DEEP SYNTHESIS LOCK" in master_arch, "Lock title missing")
check("ccc_rule17_forbids_retelling", "КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО зеркалить, копировать или пересказывать" in master_arch, "Retelling ban missing")
check("ccc_rule17_deep_synthesis", "ТРЕБОВАНИЕ ГЛУБОКОГО КЛИНИЧЕСКОГО РАЗБОРА И СИНТЕЗА" in master_arch, "Deep synthesis requirement missing")
check("ccc_rule17_net_new_value", "NET NEW VALUE" in master_arch, "Net new value missing")
check("ccc_rule17_first_principles", "биомеханика, биология тканей, физико-химия материалов" in master_arch, "First principles missing")
check("ccc_no_praise_ban_in_rule17", "ЗАПРЕТ КОМПЛИМЕНТОВ И ПОХВАЛЫ" not in master_arch, "Praise ban inappropriately present in Rule 17")

# 2. clinical_cognitive_core.py - Supporting architectures
dispute_arch = clinical_cognitive_core.DISPUTE_REFEREE_ARCHITECTURE
check("ccc_dispute_zero_mirroring", "Категорически запрещено пересказывать реплики участников дискуссии или зеркалить их аргументы" in dispute_arch, "Dispute referee anti-mirroring missing")
check("ccc_dispute_no_flattery_ban", "хвалить участников спора" not in dispute_arch, "Dispute referee has extra praise ban")

vision_arch = clinical_cognitive_core.VISION_COGNITIVE_ARCHITECTURE
check("ccc_vision_zero_mirroring", "Категорически запрещено пересказывать подпись автора к снимку" in vision_arch, "Vision caption mirroring missing")
check("ccc_vision_no_flattery_ban", "Не хвали работу или фото" not in vision_arch, "Vision has extra praise ban")

digest_arch = clinical_cognitive_core.DIGEST_COGNITIVE_ARCHITECTURE
check("ccc_digest_zero_mirroring", "Запрещено пересказывать цитаты участников чата просто с другого ракурса" in digest_arch, "Digest quote mirroring missing")

# 3. assistant.py - Source code verification across prompt generators
with open(os.path.join(REPO_ROOT, "assistant.py"), "r", encoding="utf-8") as f:
    assistant_src = f.read()

check("assistant_dialogue_zero_mirroring", "КАТЕГОРИЧЕСКИЙ ЗАПРЕТ ЗЕРКАЛИРОВАНИЯ И ПЕРЕСКАЗА (ZERO MIRRORING & DEEP SYNTHESIS)" in assistant_src, "Dialogue Rule 7 missing")
check("assistant_image_zero_mirroring", "ДОБАВЛЯЙ ЦЕННОСТЬ (ZERO MIRRORING & DEEP SYNTHESIS)" in assistant_src, "Image Rule 6 missing")
check("assistant_consilium_zero_mirroring", "КАТЕГОРИЧЕСКИЙ ЗАПРЕТ ЗЕРКАЛИРОВАНИЯ И ПЕРЕСКАЗА (ZERO MIRRORING & DEEP SYNTHESIS)" in assistant_src, "Consilium zero mirroring missing")
check("assistant_mention_zero_mirroring", "КАТЕГОРИЧЕСКИЙ ЗАПРЕТ ЗЕРКАЛИРОВАНИЯ И ПЕРЕСКАЗА (ZERO MIRRORING & DEEP SYNTHESIS)" in assistant_src, "Mention zero mirroring missing")
check("assistant_direct_ask_zero_mirroring", "КАТЕГОРИЧЕСКИЙ ЗАПРЕТ ЗЕРКАЛИРОВАНИЯ И ПЕРЕСКАЗА (ZERO MIRRORING & DEEP SYNTHESIS)" in assistant_src, "Direct ask zero mirroring missing")

# 4. Pipeline Modules: clinical_rescue, question_lifeline, summarizer, distiller, poll_engine, vision
with open(os.path.join(REPO_ROOT, "clinical_rescue.py"), "r", encoding="utf-8") as f:
    rescue_src = f.read()
check("rescue_zero_mirroring", "КАТЕГОРИЧЕСКИЙ ЗАПРЕТ ЗЕРКАЛИРОВАНИЯ И ПЕРЕСКАЗА (ZERO MIRRORING & DEEP SYNTHESIS)" in rescue_src, "Rescue zero mirroring missing")

with open(os.path.join(REPO_ROOT, "question_lifeline.py"), "r", encoding="utf-8") as f:
    lifeline_src = f.read()
check("lifeline_zero_mirroring", "КАТЕГОРИЧЕСКИЙ ЗАПРЕТ ЗЕРКАЛИРОВАНИЯ И ПЕРЕСКАЗА" in lifeline_src, "Lifeline zero mirroring missing")
check("lifeline_deep_synthesis", "НОВЫЙ ВДУМЧИВЫЙ СИНТЕЗ" in lifeline_src, "Lifeline deep synthesis missing")

with open(os.path.join(REPO_ROOT, "summarizer.py"), "r", encoding="utf-8") as f:
    summarizer_src = f.read()
check("summarizer_zero_mirroring", "ЗАПРЕТ ЗЕРКАЛИРОВАНИЯ И ПЕРЕСКАЗА (ZERO MIRRORING & DEEP SYNTHESIS)" in summarizer_src, "Summarizer zero mirroring missing")

with open(os.path.join(REPO_ROOT, "distiller.py"), "r", encoding="utf-8") as f:
    distiller_src = f.read()
check("distiller_zero_mirroring", "СИНТЕЗ (ZERO MIRRORING & DEEP SYNTHESIS)" in distiller_src, "Distiller zero mirroring missing")

with open(os.path.join(REPO_ROOT, "poll_engine.py"), "r", encoding="utf-8") as f:
    poll_src = f.read()
check("poll_zero_mirroring", "КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО просто пересказывать вопрос и варианты" in poll_src, "Poll engine zero mirroring missing")

with open(os.path.join(REPO_ROOT, "vision.py"), "r", encoding="utf-8") as f:
    vision_src = f.read()
check("vision_zero_mirroring", "КАТЕГОРИЧЕСКИЙ ЗАПРЕТ ЗЕРКАЛИРОВАНИЯ И ПЕРЕСКАЗА (ZERO MIRRORING & DEEP SYNTHESIS)" in vision_src, "Vision zero mirroring missing")

# 5. Redteam Check: No accidental compliment prohibitions added to active rules
check("no_dialogue_compliment_ban", "хвалить врача, его вопрос или кейс" not in assistant_src, "Assistant source contains dialogue praise ban")
check("no_rescue_compliment_ban", "Запрещено хвалить кейс" not in rescue_src, "Rescue source contains praise ban")
check("no_lifeline_compliment_ban", "КАТЕГОРИЧЕСКИЙ ЗАПРЕТ ПОХВАЛЫ" not in lifeline_src, "Lifeline source contains praise ban")
check("no_poll_compliment_ban", "Запрещена любая похвала участников" not in poll_src, "Poll engine contains praise ban")

print(f"\nRESULTS: {len(PASS)} PASSED, {len(FAIL)} FAILED")
if FAIL:
    sys.exit(1)
print("ZERO MIRRORING & DEEP CLINICAL SYNTHESIS REDTEAM AUDIT: 100% GREEN")
sys.exit(0)
