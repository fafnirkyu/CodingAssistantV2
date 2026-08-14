# Coding Assistant v2 — OAuth2/OIDC-Secured Cloud Coding Companion

A project-aware coding assistant powered by **Qwen2.5-Coder-0.5B-Instruct** (GGUF, 4-bit quantized), running entirely on CPU via `llama-cpp-python` — no external LLM API calls, no Ollama dependency.

This is the **v2** iteration of the project. The original API-key-authenticated version is preserved here: https://github.com/fafnirkyu/CodingAssistant — this version replaces static API keys with **Google OAuth 2.0 / OIDC**, aligned with how production APIs actually handle authentication.

---

## 🔒 Authentication Architecture

Earlier versions of this project used a static `X-API-Key` header — functional, but not representative of how real systems authenticate. This version implements proper **OAuth 2.0 / OpenID Connect**:

1. **Frontend**: Google Identity Services (`google.accounts.id`) handles the sign-in UI and returns a signed **ID token (JWT)** issued by Google.
2. **Backend verification** (no shared secret involved):
   - The Flask backend fetches Google's public signing keys from `https://www.googleapis.com/oauth2/v3/certs` (Google's JWKS endpoint).
   - Each incoming token's signature is verified against the matching public key (`joserfc`).
   - Claims are validated: `iss` must be Google's issuer, `aud` must match this app's `GOOGLE_CLIENT_ID`, and `exp` must not have passed.
3. Only requests with a **valid, unexpired, correctly-audienced** token reach protected routes (`/chat`, `/stream`, `/upload_file`, `/settings`, `/delete_project`).

This means the backend never stores or checks a shared secret — it only needs to know how to verify a signature against a public key, the same pattern used by real identity providers (Auth0, Okta, Cognito) in production systems.

---

## ✨ Features

- **Local LLM inference** — Qwen2.5-Coder-0.5B, quantized to fit CPU-only, low-RAM environments.
- **OAuth2/OIDC authentication** — Google Sign-In, verified server-side via JWKS.
- **Rate limiting** — IP-based request throttling, returns `429` when exceeded.
- **Project-based context** — automatically injects relevant uploaded project files into the model's context window.
- **Streaming responses** — token-by-token output via Server-Sent Events.
- **Web search integration** — Tavily API for grounding answers in current information.
- **File uploads** — attach project files directly through the web UI.
- **Persistent chat memory** — per-project conversation history via SQLite.

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
At runtime inside the container: model weights are stored at `/app/models`, and the SQLite chat history database at `/app/data/memory.db`.

---

## 🚀 Deployment

### Railway
1. Create a Volume, mount it to `/app/data` and `/app/models` (persists the model download and chat history across restarts).
2. Environment variables:
   - `GOOGLE_CLIENT_ID` — OAuth Client ID from Google Cloud Console.
   - `TAVILY_API_KEY` — API key for web search.
   - `PORT` — `8080`
   - `PYTHONUNBUFFERED` — `1`
3. Start command:
```bash
   gunicorn --workers 1 --timeout 300 --bind 0.0.0.0:8080 backend.app:app
```

### Hugging Face Spaces
Set these under **Settings → Variables and secrets**:
- `GOOGLE_CLIENT_ID`
- `TAVILY_API_KEY`

No secrets are ever committed to the repository — both platforms inject them as environment variables at runtime.

---

## 🧩 Technology Stack

**Backend**
- Python 3.10
- Flask — routing and API
- `llama-cpp-python` — local GGUF model inference
- `joserfc` — JWT/JWKS verification for OAuth2/OIDC
- SQLite — per-project chat history
- `requests` — Tavily search + Google JWKS fetching

**Frontend**
- HTML5 / CSS3
- Vanilla JavaScript (ES6+), Fetch API
- Google Identity Services (`accounts.google.com/gsi/client`)
- Marked.js — Markdown rendering
- Highlight.js — code syntax highlighting

---

## 🔐 Privacy

The language model runs entirely inside the deployed container — no prompts or code are sent to OpenAI, Anthropic, or any other third-party LLM provider. The only external network calls are: Tavily (optional web search) and Google's JWKS endpoint (public key retrieval for auth — no user data is sent, only a request for public keys).