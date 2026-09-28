# Coding Assistant V2 — OIDC-Secured Local Coding Assistant

A project-aware coding assistant powered by **Qwen2.5-Coder-0.5B-Instruct** in GGUF format. It runs locally through `llama-cpp-python`, supports persistent project conversations, and can incorporate uploaded source files into its context.

Google OpenID Connect protects the application routes. Optional Tavily search can add current web context when explicitly enabled.

## Features

- **Local LLM inference** using a quantized Qwen2.5-Coder model.
- **Google OpenID Connect authentication** with server-side ID-token verification.
- **Project-aware context** built from uploaded source files.
- **Persistent conversation history** stored in SQLite.
- **Streaming responses** through Server-Sent Events.
- **Optional web search** through the Tavily API.
- **File uploads and project management** through the browser interface.
- **Rate limiting** for protected operations.
- **Optional code runner and linter**, disabled by default for safety.

## Authentication flow

1. Google Identity Services displays the sign-in interface and returns a signed OpenID Connect ID token.
2. The Flask backend downloads Google's public signing keys from its JWKS endpoint.
3. The backend verifies the token signature and validates its issuer, audience, and expiration.
4. Only requests with a valid token can access protected routes such as chat, history, settings, uploads, and project management.

The application does not use a shared API key for protected routes. Project paths are resolved and checked before use so a project name cannot escape the configured project directory.

## Project structure

```text
.
├── backend/
│   └── app.py                 # Flask API, authentication, storage, and inference
├── static/                    # Browser JavaScript and styling
├── templates/                 # Web interface
├── tests/                     # Backend access and security tests
├── data/                      # Runtime SQLite data; ignored by Git
├── models/                    # Downloaded GGUF models; ignored by Git
├── projects/                  # Uploaded project files; ignored by Git
├── .env.example               # Configuration template
├── Dockerfile
├── requirements.txt           # Runtime dependencies
└── requirements-ci.txt        # Lightweight CI test dependencies
```

Runtime directories can be changed with `DATA_DIR`, `MODELS_DIR`, and `PROJECTS_DIR`. A specific local model can be selected with `MODEL_PATH`.

## Local setup

The examples below use Windows PowerShell.

```powershell
git clone https://github.com/fafnirkyu/CodingAssistantV2.git
cd CodingAssistantV2

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

Copy-Item .env.example .env
```

Add your Google OAuth client ID to `.env`:

```env
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
```

To enable optional web search, also configure:

```env
TAVILY_API_KEY=your-tavily-api-key
```

Start the development server:

```powershell
python -m backend.app
```

Open `http://127.0.0.1:5000`. On first startup, the application downloads the configured GGUF model from Hugging Face unless `MODEL_PATH` points to an existing file.

## Docker

Build the image:

```powershell
docker build -t coding-assistant-v2 .
```

Run it with persistent volumes for conversations, models, and project files:

```powershell
docker run --rm -p 8080:8080 --env-file .env -v coding-assistant-data:/app/data -v coding-assistant-models:/app/models -v coding-assistant-projects:/app/projects coding-assistant-v2
```

Open `http://127.0.0.1:8080`.

## Tests

The CI dependency set avoids installing the full local inference stack because the access tests mock model download and loading.

```powershell
python -m pip install -r requirements-ci.txt
python -m pytest -q
```

GitHub Actions runs the backend access tests on every push and pull request.

## Deployment notes

The included Dockerfile starts one Gunicorn worker on port `8080`:

```text
gunicorn --bind 0.0.0.0:8080 backend.app:app
```

For a container platform such as Railway:

1. Build the repository with its Dockerfile.
2. Configure `GOOGLE_CLIENT_ID` and, optionally, `TAVILY_API_KEY` as environment variables.
3. Persist the data, model, and project directories. They may be separate volumes, or subdirectories of one mounted volume configured through `DATA_DIR`, `MODELS_DIR`, and `PROJECTS_DIR`.
4. Keep one application worker unless the SQLite connection and in-memory rate limiter are replaced with multi-process-safe alternatives.

Keep credentials and API keys out of version control.

## Technology stack

**Backend**

- Python 3.10
- Flask and Gunicorn
- `llama-cpp-python`
- `huggingface-hub`
- `joserfc` for JWT and JWKS verification
- SQLite
- Requests

**Frontend**

- HTML, CSS, and vanilla JavaScript
- Google Identity Services
- Marked.js
- Highlight.js

## Privacy and external services

LLM inference runs in the application process. Prompts and uploaded code are not sent to an external LLM provider.

Google Identity Services communicates with Google during sign-in, and the backend requests Google's public signing keys to verify ID tokens. When optional Tavily search is enabled, the submitted search query is sent to Tavily.

## Current limitations

- Authentication is implemented, but project files and conversation history are not isolated by user. Authenticated users share the configured storage, so this version is intended for personal or controlled demonstration environments.
- The rate limiter is stored in application memory and applies independently to each process.
- SQLite access and model loading are designed around a single application worker.
- The optional runner and linter are disabled by default with `RUNNER_ENABLED=0` and `LINTER_ENABLED=0`.
- Model startup and response speed depend on available CPU and memory.
- This is a portfolio project, not a production-ready multi-tenant service.
