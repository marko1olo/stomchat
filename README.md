# 📡 StomChat — Omnichannel Clinical Knowledge & Telephony Platform

[![Live Demo](https://img.shields.io/badge/Live_Showcase-GitHub_Pages-10b981?style=for-the-badge&logo=github)](https://marko1olo.github.io/stomchat/)
[![PWA Ready](https://img.shields.io/badge/PWA-Installable-22c55e?style=for-the-badge&logo=pwa)](https://marko1olo.github.io/stomchat/manifest.json)
[![AI Index](https://img.shields.io/badge/LLM_Search-llms.txt-38bdf8?style=for-the-badge)](https://marko1olo.github.io/stomchat/llms.txt)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python)](https://www.python.org/)
[![Telethon](https://img.shields.io/badge/Telethon-Async-2CA5E0?style=for-the-badge&logo=telegram)](https://github.com/LonamiWebs/Telethon)
[![SQLite FTS5](https://img.shields.io/badge/SQLite-FTS5-003B57?style=for-the-badge&logo=sqlite)](https://www.sqlite.org/)

High-throughput clinical communication platform combining asynchronous Telegram channel dispatch, WhatsApp webhooks, Asterisk/FreePBX SIP VoIP telemetry, and full-text search (FTS5) knowledge extraction for multi-clinic dental networks.

---

## 🏛️ System Architecture

```mermaid
graph LR
    User[Patient / Operator] -->|Telegram / WhatsApp| Gate[Telethon & Webhook Ingest]
    Gate -->|FTS5 Tokenizer| KB[(SQLite FTS5 Clinical Corpus)]
    KB -->|Context Grounding| AI[AI Clinical Classifier]
    AI -->|Deterministic Guardrails| Resp[Auto-Reply & Doctor Notification]
```

---

## 🔬 Core Modules

1. **Omnichannel Messaging:** Real-time bi-directional sync across Telegram, WhatsApp, and Web Chat with under 60-second response SLAs.
2. **VoIP SIP Telephony Engine:** In-browser G.711u call notifications, Caller ID lookup, and instant speech-to-text transcripts.
3. **AI Clinical Assistant:** Context-grounded auto-reply generator with empathetic and emergency triage modes.
4. **Digest Delivery Pipeline:** Automated patient reminders, pre-op prep instructions, and post-surgery care checklists.

---

## 🛠️ Quickstart

```bash
git clone https://github.com/marko1olo/stomchat.git
cd stomchat
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Run knowledge ingestion and worker
python main.py
```

---

### 👨‍💻 Lead Architect
**Адольф Петушков (Adolf Petushkov)** — High-Concurrency Systems & Clinical AI Architecture.  
GitHub: [@marko1olo](https://github.com/marko1olo)
