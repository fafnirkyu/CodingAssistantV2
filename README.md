# Qwen Coder — Cloud-Native Personal Coding Assistant

A project-aware coding companion powered by Qwen2.5-Coder-1.5B optimized for cloud deployment on Railway.

This assistant provides high-quality coding intelligence within a constrained 2GB RAM environment using llama-cpp-python and GGUF quantization.

---

## ✨ Features

- **Cloud-Ready LLM** — Optimized Qwen 1.5B model running on CPU-only instances.
- **Persistent Storage** — Integrated with Railway Volumes for project files and model storage.
- **Project-based Context** — Automatically injects your uploaded project files into the AI's context.
- **Streaming Output** — Real-time, token-by-token response streaming.
- **Web Search** — Integrated DuckDuckGo API for quick information lookup.
- **File Uploads** — Upload project files directly via the web UI.
- **Persistent Chat Memory** — Remembers previous interactions per project using SQLite.
- **Memory Efficient** — Uses Flash Attention and 4-bit quantization to fit coding intelligence into 2GB RAM.

---

## 📂 Project Structure

.
├── backend/
│ └── app.py # Flask backend with Ollama integration
├── static/
│ ├── styles.css # Dark mode styling
│ └── script.js # Frontend interactivity & streaming
├── templates/
│ └── index.html # Web interface
├── projects/ # Your saved coding projects
├── memory.db # SQLite chat history database
└── README.md

---
## 🔒 Security & Authentication

This project incorporates robust security controls tailored for public cloud deployments:

1. Authentication: Secure Google OAuth 2.0 / OIDC login supporting public multi-user access via JSON Web Token (JWT) verification.

2. Rate Limiting: Custom IP-based rate tracking returns HTTP 429 (Too Many Requests) status codes if thresholds are exceeded.

---

## Deployment (Railway)

1. Mount Volumes: Create a volume and mount it to /app/data and /app/models.

2. Environment Variables:
   - `API_KEY`: Your secret key for API verification.
   - `PORT`: 8080
   - `PYTHONUNBUFFERED`: 1

3. Start command:

    ```bash
    gunicorn --workers 1 --timeout 300 --bind 0.0.0.0:8080 backend.app:app
    ```

## Deployment (Hugging Face Spaces)

1. Environment Variables / Secrets: Add the following secret keys in your Space settings:
    - `GOOGLE_CLIENT_ID`: Your Google OAuth Client ID.
    - `TAVILY_API_KEY`: Your Tavily search API key.

### Privacy

This assistant runs on your private Railway instance. No data is sent to OpenAI, Anthropic, or any other third-party LLM providers.

🧩 Technology Stack

Backend

Python 3.9+ — Core backend language.

Flask — Lightweight web framework for handling API routes and serving the web UI.

SQLite — Embedded database for persistent per-project chat history.

Ollama — Local LLM runner for Qwen v3 30B.

Qwen v3 30B — Large Language Model specialized for coding and reasoning.

Requests — HTTP client for DuckDuckGo search API.

Frontend

HTML5 — Web interface structure.

CSS3 — Custom dark mode styling.

JavaScript (ES6+) — Client-side logic for chat interaction & streaming.

Fetch API — Asynchronous communication with the backend.

Marked.js — Client-side Markdown rendering for assistant messages.

Highlight.js — Syntax highlighting for code blocks.

🔒 Privacy

Everything runs entirely locally.
No data is sent to external services except optional DuckDuckGo search queries.
