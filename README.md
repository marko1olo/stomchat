# StomChat — Dentistry Chat Knowledge Base & AI Telegram Bot

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg?style=flat&logo=python)](https://www.python.org/)
[![Telethon](https://img.shields.io/badge/Telethon-MTProto-blue.svg?style=flat)](https://github.com/LonamiWebs/Telethon)
[![Google Gemini](https://img.shields.io/badge/Google_Gemini-GenAI-orange.svg?style=flat&logo=google-gemini)](https://deepmind.google/technologies/gemini/)
[![Groq](https://img.shields.io/badge/Groq-LPU-orange.svg?style=flat)](https://groq.com/)

**StomChat** is a hybrid Telegram Userbot + Bot system powered by Google Gemini and Groq. It automatically archives dental community chat messages, analyzes media files (images, clinical photos, X-rays, videos), uses agentic search APIs to verify facts, and publishes daily/weekly AI-synthesized digests and knowledge articles to Telegraph and Telegram channels.

---

## 🚀 Key Features

### 📡 1. Hybrid Telegram Client Architecture
- **Userbot Listener**: Listens to targeted dental chats, groups, and channels to archive messages and media into SQLite.
- **Bot Publisher**: A dedicated bot client that formats, drafts, and posts summarized digests and Telegraph pages without cluttering your personal userbot session.

### 🧠 2. AI Summaries & Digests
- **Daily Digest**: Auto-compiles daily summaries from dental chat discussions (focusing on cases, queries, clinic issues).
- **Weekly Newspaper**: Formulates a structured weekly report on dentistry developments and discussed clinical cases.
- **Powered by Gemini & Groq**: Dynamic multi-key API rotation with built-in cooldowns for 429/timeouts.

### 🔍 3. Agentic Fact-Checking & Search
- Integrates **Tavily Search API** and **DuckDuckGo API** to perform web searches, verify claims made in dental chats, and add authoritative medical references.

### 🖼️ 4. Multimodal Media Analysis (AI Vision)
- **Automatic Frame Extraction**: Leverages `OpenCV` and `FFmpeg` to extract keyframes from uploaded clinical videos.
- **X-Ray & Photo Inspection**: Inspects dental X-rays, clinic setups, and dental photographs using multimodal Gemini Vision models.

### 📑 5. Telegraph Integration
- Automatically compiles long discussions into beautifully formatted Telegraph articles using `html-telegraph-poster` and publishes them to channels.

---

## 🛠️ Tech Stack
- **Core**: Python 3.10+
- **Telegram Protocol**: [Telethon](https://github.com/LonamiWebs/Telethon) (MTProto Client)
- **LLM / Vision**: Google GenAI SDK (Gemini 1.5 Pro/Flash), Groq, OpenAI API
- **Web Search Tools**: Tavily API, DuckDuckGo Search API
- **Video & Image Processing**: OpenCV, Pillow, FFmpeg
- **Database**: SQLite (via `aiosqlite`)
- **Telemetry & Process Guard**: `psutil`, custom heartbeat watchdog

---

## 📦 Getting Started

### 📋 Prerequisites
- Python 3.10+
- API ID & API Hash from [my.telegram.org](https://my.telegram.org/)
- Google Gemini API Key
- Groq API Key (optional, for fast transcripts/LLM tasks)
- Tavily API Key (optional, for web search verification)
- FFmpeg installed in system path

### ⚙️ Installation

1. **Clone the repository**:
   ```bash
   git clone https://github.com/marko1olo/stomchat.git
   cd stomchat
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure Environment Variables**:
   Create a `.env` file in the root directory:
   ```env
   API_ID=your-telegram-api-id
   API_HASH=your-telegram-api-hash
   BOT_TOKEN=your-telegram-bot-token
   SESSION_NAME=your-userbot-session
   GEMINI_API_KEY=your-gemini-key
   TAVILY_API_KEY=your-tavily-key
   GROQ_API_KEY=your-groq-key
   REPORT_CHAT_ID=-100xxxxxxxxxx
   REPORT_HOUR=20
   ```

4. **Run the Bot**:
   ```bash
   python main.py
   ```

---

## 📄 License
This project is licensed under the MIT License.
