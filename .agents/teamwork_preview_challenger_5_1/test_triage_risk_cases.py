#!/usr/bin/env python3
"""
Adversarial evaluation of Triage Prompt changes (Diff 4) and Quality Validator changes (Diff 5).
Simulates classification edge-cases and tests risk vectors.
"""
import json

# Edge case test matrix for Triage Prompt
TRIAGE_TEST_CASES = [
    {
        "id": "CASE_1_EQUIPMENT_PREFERENCE",
        "chat": [
            "Д-р Иванов: Коллеги, кто на какой скорости препарирует под циркон?",
            "Д-р Петров: 150к с водой",
            "Д-р Сидоров: 200к на повышайке"
        ],
        "category": "Peer routine exchange",
        "desired_bot_action": "SILENCE (Do not lecture practicing doctors on their handpiece speed)",
        "old_prompt_expected": "SILENCE (2+ colleagues discussing)",
        "diff4_prompt_risk": "OVER-TRIGGER ('мнения разделились, нужен EBM-протокол')",
        "risk_level": "HIGH"
    },
    {
        "id": "CASE_2_CLINICAL_SARCASM",
        "chat": [
            "Д-р Смирнов: Пациент просит гарантию 10 лет на билдап без коронки 😂",
            "Д-р Ковалев: Скажи что гарантия до дверей клиники))"
        ],
        "category": "Sarcasm with clinical terms",
        "desired_bot_action": "SILENCE (Humor / banter)",
        "old_prompt_expected": "SILENCE (Joke / humor)",
        "diff4_prompt_risk": "OVER-TRIGGER (Answers are 'односложные/сомнительные', mentions buildup without crown)",
        "risk_level": "CRITICAL"
    },
    {
        "id": "CASE_3_HARMFUL_PEER_CONSENSUS",
        "chat": [
            "Д-р Новичок: Перфорация дна пульпарной камеры в области фуркации 3.6, чем закрыть?",
            "Д-р Стаж: Закрой Fuji IX и не парься, всегда так делал",
            "Д-р Опыт: +1, фуджи отлично стоит"
        ],
        "category": "Harmful clinical consensus (Contraindicated protocol)",
        "desired_bot_action": "TRIGGER (MTA / bioceramic is mandatory, Fuji IX is obsolete and dangerous)",
        "old_prompt_expected": "SILENCE (2+ colleagues discussing)",
        "diff4_prompt_risk": "AMBIGUOUS / UNDER-TRIGGER ('Коллеги уже ответили и согласны' vs 'сомнительный ответ')",
        "risk_level": "HIGH"
    },
    {
        "id": "CASE_4_BRAND_VS_CLINICAL",
        "chat": [
            "Д-р Артем: Какой ультразвук лучше взять в кабинет: NSK или Woodpecker?",
            "Д-р Денис: NSK топ"
        ],
        "category": "Brand purchase advice",
        "desired_bot_action": "SILENCE (Equipment purchase preference, not clinical diagnosis)",
        "old_prompt_expected": "SILENCE (Non-clinical or peer opinion)",
        "diff4_prompt_risk": "OVER-TRIGGER ('ответ односложный топ, нужен EBM-протокол')",
        "risk_level": "MEDIUM"
    },
    {
        "id": "CASE_5_URGENT_SURGERY_COMPLICATION",
        "chat": [
            "Д-р Хирург: При удалении 3.8 верхушка дистального корня 2 мм ушла в нижнечелюстной канал. Кровотечение умеренное.",
            "Д-р Коллега: Ого 😱"
        ],
        "category": "Surgical emergency / complication",
        "desired_bot_action": "TRIGGER (Evidence-based protocol: CBCT, do not blindly scrape canal, neurosensory risk, steroid/referral protocol)",
        "old_prompt_expected": "SILENCE (Colleague replied 'Ого', or liability fear)",
        "diff4_prompt_risk": "POSITIVE TRIGGER (Prompt allows trigger when peer reply is single exclamation)",
        "risk_level": "BENEFICIAL"
    }
]

def analyze_triage_risks():
    print("=== ADVERSARIAL ANALYSIS OF TRIAGE PROMPT (DIFF 4) ===")
    for case in TRIAGE_TEST_CASES:
        print(f"\n[{case['id']}] ({case['category']})")
        print("Chat:")
        for line in case['chat']:
            print(f"  {line}")
        print(f"Desired: {case['desired_bot_action']}")
        print(f"Old prompt: {case['old_prompt_expected']}")
        print(f"Diff 4 Risk: {case['diff4_prompt_risk']} [Risk: {case['risk_level']}]")

if __name__ == "__main__":
    analyze_triage_risks()
