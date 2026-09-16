import os
import re
import glob
import time
import functools
import sqlite3
import subprocess
from typing import List, Tuple, Optional
from pathlib import Path
from flask import Flask, request, jsonify, render_template, Response, abort
import requests
from werkzeug.utils import secure_filename
from llama_cpp import Llama
from huggingface_hub import hf_hub_download
from dotenv import load_dotenv
from joserfc import jwt, jws
from joserfc.jwk import RSAKey
from joserfc.jwt import JWTClaimsRegistry

load_dotenv()

# Security: rate limiting
RATE_LIMIT = 10  # 10 requests per minute
REQUEST_COUNTS = {}

def rate_limit(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        client_ip = request.remote_addr
        now = time.time()
        if client_ip not in REQUEST_COUNTS:
            REQUEST_COUNTS[client_ip] = {"count": 0, "timestamp": now}
        if now - REQUEST_COUNTS[client_ip]["timestamp"] > 60:
            REQUEST_COUNTS[client_ip] = {"count": 0, "timestamp": now}
        if REQUEST_COUNTS[client_ip]["count"] >= RATE_LIMIT:
            abort(429)
        REQUEST_COUNTS[client_ip]["count"] += 1
        return func(*args, **kwargs)
    return wrapper

# --- Google OAuth 2.0 / OIDC Config ---
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")

def require_oauth():
    """Google OIDC token verification decorator implementing OAuth 2.1 specs."""
    def oauth_decorator(f):
        @functools.wraps(f)
        def oauth_wrapper(*args, **kwargs):
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return jsonify({"error": "unsupported_token_type", "message": "Missing bearer token."}), 401
            
            token_string = auth_header.split(" ")[1]
            
            try:
                jwks_url = "https://www.googleapis.com/oauth2/v3/certs"
                jwks_data = requests.get(jwks_url, timeout=5).json()
                
                # Correctly access protected header for joserfc
                obj = jws.extract_compact(token_string.encode())
                kid = obj.protected.get("kid")
                
                raw_key = next((k for k in jwks_data.get("keys", []) if k.get("kid") == kid), None)
                if not raw_key:
                    return jsonify({"error": "invalid_key", "message": "Google public key not found."}), 401
                    
                public_key = RSAKey.import_key(raw_key)
                token = jwt.decode(token_string, public_key)
                
                claims_registry = JWTClaimsRegistry(
                    iss={"values": ["https://accounts.google.com", "accounts.google.com"]},
                    aud={"value": GOOGLE_CLIENT_ID}
                )
                claims_registry.validate(token.claims)
                
            except Exception as e:
                print(f"TOKEN VALIDATION ERROR: {str(e)}")
                return jsonify({"error": "invalid_token", "message": str(e)}), 401
                
            return f(*args, **kwargs)
        return oauth_wrapper
    return oauth_decorator

# --- Local and container paths ---
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def configured_path(name: str, default: Path) -> Path:
    """Resolve an optional path setting relative to the project root."""
    value = Path(os.getenv(name, str(default))).expanduser()
    return value if value.is_absolute() else PROJECT_ROOT / value


DATA_DIR = configured_path("DATA_DIR", PROJECT_ROOT / "data")
MODELS_DIR = configured_path("MODELS_DIR", PROJECT_ROOT / "models")
PROJECTS_DIR = configured_path("PROJECTS_DIR", PROJECT_ROOT / "projects")

for directory in (DATA_DIR, MODELS_DIR, PROJECTS_DIR):
    directory.mkdir(parents=True, exist_ok=True)

# --- Model configuration ---
REPO_ID = "Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF"
FILENAME = "qwen2.5-coder-0.5b-instruct-q4_k_m.gguf"
MODEL = REPO_ID

model_path = configured_path("MODEL_PATH", MODELS_DIR / FILENAME)
if not model_path.exists():
    print(f"Downloading model {FILENAME}...")
    model_path = Path(
        hf_hub_download(
            repo_id=REPO_ID, filename=FILENAME, local_dir=str(model_path.parent)
        )
    )

llm = Llama(model_path=str(model_path), n_ctx=8192, n_threads=4, n_batch=512, flash_attn=True)

DB_PATH = str(DATA_DIR / "memory.db")
PROJECTS_DIR = str(PROJECTS_DIR)

TEMPERATURE = float(os.getenv("TEMPERATURE", "0.2"))
TOP_P = float(os.getenv("TOP_P", "0.9"))
NUM_CTX = int(os.getenv("NUM_CTX", "2048"))
SEED = int(os.getenv("SEED", "7"))

MAX_FILES_IN_CONTEXT = int(os.getenv("MAX_FILES_IN_CONTEXT", "10"))
MAX_FILE_BYTES = int(os.getenv("MAX_FILE_BYTES", str(16 * 1024)))
MAX_PROMPT_CHARS = int(os.getenv("MAX_PROMPT_CHARS", str(18000)))

ALLOWED_EXTENSIONS = {
    "py", "ipynb", "js", "ts", "tsx", "jsx", "md", "txt", "json", "yml", "yaml",
    "html", "css", "toml", "ini", "cfg", "sh", "ps1"
}

RUNNER_ENABLED = os.getenv("RUNNER_ENABLED", "0") == "1"
LINTER_ENABLED = os.getenv("LINTER_ENABLED", "0") == "1"

SYSTEM_PROMPT = """
You are a senior AI engineer. 
1) Always start your response with '#mode: write|review|explain|discuss|math'.
2) If code is requested, use the 'FILE: path/to/file.ext' format inside markdown blocks.
3) If a general question is asked, provide a clear and direct answer.
4) Be concise and professional.
"""

app = Flask(__name__, static_folder="../static", template_folder="../templates")

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cur = conn.cursor()
cur.execute("""
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT,
    role TEXT,
    content TEXT,
    ts REAL
)
""")
conn.commit()

def save_message(project: str, role: str, content: str):
    cur.execute(
        "INSERT INTO messages (project, role, content, ts) VALUES (?, ?, ?, ?)",
        (project, role, content, time.time())
    )
    conn.commit()

def load_recent(project: str, limit: int = 12):
    cur.execute(
        "SELECT role, content FROM messages WHERE project=? ORDER BY id DESC LIMIT ?",
        (project, limit)
    )
    rows = cur.fetchall()[::-1]
    return [{"role": r[0], "content": r[1]} for r in rows]

def resolve_project_path(project: str) -> str:
    """Resolve a project path without creating it or leaving the project root."""
    base = Path(PROJECTS_DIR).resolve()
    path = (base / project).resolve()
    if path == base or base not in path.parents:
        abort(400, description="Invalid project path")
    return str(path)


def project_base_dir(project: str) -> str:
    path = resolve_project_path(project)
    os.makedirs(path, exist_ok=True)
    return path

def _is_allowed_file(path: str) -> bool:
    return "." in path and path.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def _sanitize_rel_path(rel_path: str) -> Optional[str]:
    rel_path = rel_path.strip().replace("\\", "/")
    rel_path = re.sub(r"^/+", "", rel_path)
    if not _is_allowed_file(rel_path):
        return None
    if ".." in rel_path.split("/"):
        return None
    return rel_path

def _rank_project_files(file_paths: List[str]) -> List[str]:
    def key(p: str):
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            mtime = 0
        depth = p.count(os.sep)
        name_bonus = 0
        basename = os.path.basename(p).lower()
        if basename in {"readme.md", "requirements.txt", "pyproject.toml", "setup.py"}:
            name_bonus = 10
        return (-mtime, depth, -name_bonus)
    return sorted(file_paths, key=key)

def load_project_files_context(project: str) -> str:
    base_dir = project_base_dir(project)
    files_data = []
    byte_budget = MAX_PROMPT_CHARS

    preferred_globs = [
        "**/*.py", "**/*.ipynb", "**/*.md",
        "**/*.js", "**/*.ts", "**/*.tsx", "**/*.jsx",
        "**/*.json", "**/*.yml", "**/*.yaml",
        "**/*.toml", "**/*.ini",
        "**/*.html", "**/*.css",
        "README.md", "requirements.txt", "pyproject.toml", "setup.py"
    ]

    candidate_paths = set()
    for pattern in preferred_globs:
        candidate_paths.update(glob.glob(os.path.join(base_dir, pattern), recursive=True))

    ranked = _rank_project_files([p for p in candidate_paths if os.path.isfile(p)])

    count = 0
    for path in ranked:
        if count >= MAX_FILES_IN_CONTEXT:
            break
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size > MAX_FILE_BYTES:
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except Exception:
            continue

        rel_path = os.path.relpath(path, base_dir)
        block = f"FILE: {rel_path}\n```\n{content}\n```"
        if len(block) > byte_budget:
            break
        files_data.append(block)
        byte_budget -= len(block)
        count += 1

    if not files_data:
        return "No existing project files found."
    return "\n\n".join(files_data)

FILE_BLOCK_RE = re.compile(
    r"FILE:\s*(?P<path>[^\n\r]+)\s*```(?P<lang>[\w.+-]+)?\s*\n(?P<code>.*?)```",
    re.DOTALL
)
CODE_FENCE_RE = re.compile(
    r"```(?P<lang>[\w.+-]*)\s*\n(?P<code>.*?)```",
    re.DOTALL
)

def _ensure_mode_header(text: str, default_mode: str = "write") -> str:
    first = text.strip().splitlines()[0].strip() if text.strip().splitlines() else ""
    if not re.search(r"^#mode:\s*(write|review|explain)\s*$", first, re.IGNORECASE):
        text = f"#mode: {default_mode}\n\n" + text
    return text

def _ensure_plan_section(text: str) -> str:
    if re.search(r"(?im)^\s*plan\s*$", text):
        return text
    preface = (
        "Plan\n"
        "- Outline steps briefly.\n"
        "- Write complete code using the Multi-file format.\n"
        "- Add a tiny MWE when appropriate.\n"
        "- Include Self-Check at the end.\n\n"
    )
    return text if "Plan" in text[:400] else preface + text

def _wrap_lonely_fence_as_file(text: str) -> str:
    if FILE_BLOCK_RE.search(text):
        return text
    m = CODE_FENCE_RE.search(text)
    if not m:
        return text
    lang = (m.group("lang") or "text").lower()
    default_map = {
        "python": "scratch/main.py", "py": "scratch/main.py",
        "javascript": "scratch/index.js", "js": "scratch/index.js",
        "typescript": "scratch/index.ts", "ts": "scratch/index.ts",
        "json": "scratch/data.json", "html": "scratch/index.html",
        "css": "scratch/styles.css", "md": "scratch/README.md",
    }
    rel = default_map.get(lang, "scratch/snippet.txt")
    code = m.group("code")
    file_block = f"FILE: {rel}\n```{lang}\n{code}\n```"
    start, end = m.span()
    return text[:start] + file_block + text[end:]

def enforce_response_contract(text: str, default_mode: str = "write") -> str:
    text = _ensure_mode_header(text, default_mode=default_mode)
    if default_mode == "write":
        text = _ensure_plan_section(text)
        text = _wrap_lonely_fence_as_file(text)
    text = re.sub(r"```(\s*\n)", "```text\1", text)
    return text

def save_generated_files(project: str, assistant_text: str) -> List[str]:
    base_dir = project_base_dir(project)
    matches = list(FILE_BLOCK_RE.finditer(assistant_text))
    saved: List[str] = []
    for m in matches:
        rel_path_raw = m.group("path")
        lang = (m.group("lang") or "").strip().lower()
        code = m.group("code")
        rel_path = _sanitize_rel_path(rel_path_raw)
        if not rel_path:
            continue
        abs_path = os.path.abspath(os.path.join(base_dir, rel_path))
        if not abs_path.startswith(base_dir + os.sep) and abs_path != base_dir:
            continue
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        if not lang:
            ext = rel_path.rsplit(".", 1)[-1].lower()
            lang = ext
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(code.strip())
        saved.append(rel_path)
    return saved

def parse_user_mode(text: str) -> str:
    m = re.search(r"#mode:\s*(write|review|explain|discuss|math)", text, re.IGNORECASE)
    if m:
        return m.group(1).lower()
    if re.search(r"review|critique|improve|refactor|fix|bug|error", text, re.IGNORECASE):
        return "review"
    if re.search(r"explain|walk me through|how does|what does.*mean", text, re.IGNORECASE):
        return "explain"
    if re.search(r"\b(write|code|create|build|script|function|generate|program)\b", text, re.IGNORECASE):
        return "write"
    return "discuss"

def build_chat_messages(project: str, user_text: str, search_results: List[str] = None) -> Tuple[List[dict], str]:
    hist = load_recent(project, limit=8)
    files_context = load_project_files_context(project)
    mode = parse_user_mode(user_text)

    context_sections = []
    if search_results:
        context_sections.append("Web search results:\n" + "\n".join(search_results))
    if files_context != "No existing project files found.":
        context_sections.append("Selected project files:\n" + files_context)

    instructions = [
        SYSTEM_PROMPT.strip(),
        f"Start your response with '#mode: {mode}'.",
        "Answer the user directly. Do not repeat system instructions, session settings, or project context.",
        "Do not claim to have inspected files that were not supplied as project context.",
    ]
    if mode == "write":
        instructions.append(
            "For code generation, include a concise Plan and use FILE: path blocks for files to create or change."
        )
    if context_sections:
        instructions.append("\n\n".join(context_sections))
    else:
        instructions.append("No project files are currently selected. Answer as a standalone coding assistant.")

    messages = [{"role": "system", "content": "\n\n".join(instructions)}]
    messages.extend(
        {"role": message["role"], "content": message["content"]}
        for message in hist
        if message["role"] in {"user", "assistant"}
    )
    messages.append({"role": "user", "content": user_text})
    return messages, mode

# -------------- Routes --------------
@app.route("/")
@rate_limit
def index():
    return render_template(
        "index.html",
        google_client_id=GOOGLE_CLIENT_ID
    )

@app.route("/protected")
@require_oauth()
def protected_resource():
    return "This is a protected resource!"

@app.route("/history/<project>", methods=["GET"])
@require_oauth()
def get_history(project):
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT role, content FROM messages WHERE project=? ORDER BY id ASC", (project,))
        rows = cur.fetchall()
        conn.close()
        history_list = [{"role": r[0], "content": r[1]} for r in rows]
        return jsonify({"history": history_list})
    except Exception as e:
        print(f"Database Error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/projects")
@require_oauth()
def get_projects():
    cur.execute("SELECT DISTINCT project FROM messages ORDER BY project ASC")
    rows = cur.fetchall()
    return jsonify([r[0] for r in rows])

@app.route("/settings", methods=["GET", "POST"])
@require_oauth()
def settings():
    global MODEL, TEMPERATURE, TOP_P, NUM_CTX, SEED
    if request.method == "POST":
        data = request.json or {}
        MODEL = data.get("model", MODEL)
        TEMPERATURE = float(data.get("temperature", TEMPERATURE))
        TOP_P = float(data.get("top_p", TOP_P))
        NUM_CTX = int(data.get("num_ctx", NUM_CTX))
        SEED = int(data.get("seed", SEED))
    return jsonify({
        "model": MODEL, "temperature": TEMPERATURE, "top_p": TOP_P,
        "num_ctx": NUM_CTX, "seed": SEED
    })

@app.route("/add_project", methods=["POST"])
@require_oauth()
def add_project():
    data = request.json or {}
    project = (data.get("project") or "").strip()
    if not project:
        return jsonify({"error": "empty project name"}), 400
    project_base_dir(project)
    save_message(project, "system", f"Project {project} created.")
    return jsonify({"status": "ok", "project": project})

@app.route("/chat", methods=["POST"])
@require_oauth()
def chat():
    data = request.json or {}
    project = data.get("project", "default")
    user_text = (data.get("message") or "").strip()
    if not user_text:
        return jsonify({"error": "empty message"}), 400

    save_message(project, "user", user_text)
    messages, mode = build_chat_messages(project, user_text)

    try:
        resp = llm.create_chat_completion(
            messages=messages, max_tokens=512, temperature=TEMPERATURE, top_p=TOP_P
        )
        assistant_text_raw = resp["choices"][0]["message"]["content"]
        assistant_text = enforce_response_contract(assistant_text_raw, default_mode=mode)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    save_message(project, "assistant", assistant_text)
    saved_files = save_generated_files(project, assistant_text)
    return jsonify({"response": assistant_text, "saved_files": saved_files})

@app.route("/stream", methods=["POST"])
@require_oauth()
@rate_limit
def stream():
    data = request.json or {}
    project = data.get("project", "default")
    user_text = (data.get("message") or "").strip()

    save_message(project, "user", user_text)
    messages, mode = build_chat_messages(project, user_text)

    def generate():
        yield "data:  \n\n"
        try:
            stream_res = llm.create_chat_completion(
                messages=messages, max_tokens=1024, temperature=0.7, stream=True
            )
            for chunk in stream_res:
                token = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                if token:
                    safe_token = token.replace("\n", "\\n").replace("\r", "")
                    yield f"data: {safe_token}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            print(f"STREAM ERROR: {e}")
            yield f"data: ERROR: {str(e)}\n\n"

    resp = Response(generate(), mimetype="text/event-stream")
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Cache-Control"] = "no-cache"
    return resp

@app.route("/search_web", methods=["POST"])
@require_oauth()
@rate_limit
def search_web():
    data = request.json or {}
    query = (data.get("query") or "").strip()
    TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
    payload = {"api_key": TAVILY_API_KEY, "query": query, "search_depth": "basic", "max_results": 3}
    try:
        response = requests.post("https://api.tavily.com/search", json=payload, timeout=10)
        tavily_data = response.json()
        results = [f"{r['title']}: {r['content']}" for r in tavily_data.get("results", [])]
        return jsonify({"results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route("/upload_file/<project>", methods=["POST"])
@require_oauth()
@rate_limit
def upload_file(project):
    if "file" not in request.files:
        return jsonify({"error": "no file part"}), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "no selected file"}), 400
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        save_path = os.path.join(project_base_dir(project), filename)
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        file.save(save_path)
        save_message(project, "system", f"File uploaded: {filename}")
        return jsonify({"status": "ok", "filename": filename})
    else:
        return jsonify({"error": "file type not allowed"}), 400

@app.route("/delete_project", methods=["POST"])
@require_oauth()
def delete_project():
    data = request.json or {}
    project = (data.get("project") or "").strip()
    if not project:
        return jsonify({"error": "empty project name"}), 400
    project_dir = resolve_project_path(project)
    cur.execute("DELETE FROM messages WHERE project=?", (project,))
    conn.commit()
    if os.path.exists(project_dir):
        import shutil
        shutil.rmtree(project_dir)
    return jsonify({"status": "ok", "project": project})

@app.route("/cancel", methods=["POST"])
@require_oauth()
def cancel():
    return jsonify({"status": "Session reset requested", "note": "Inference is self-contained."})

def _run_cmd(cmd: List[str], cwd: Optional[str] = None, timeout: int = 20) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        return 1, "", str(e)

@app.route("/run/<project>", methods=["POST"])
@require_oauth()
@rate_limit
def run_project(project):
    if not RUNNER_ENABLED:
        return jsonify({"error": "runner disabled; set RUNNER_ENABLED=1"}), 400
    data = request.json or {}
    entry = (data.get("entry") or "main.py").strip()
    base = project_base_dir(project)
    if not _is_allowed_file(entry):
        return jsonify({"error": "disallowed entry point"}), 400
    path = os.path.join(base, entry)
    if not os.path.exists(path):
        return jsonify({"error": f"missing entry: {entry}"}), 404
    code, out, err = _run_cmd(["python", entry], cwd=base, timeout=60)
    return jsonify({"code": code, "stdout": out, "stderr": err})

@app.route("/lint/<project>", methods=["POST"])
@require_oauth()
@rate_limit
def lint(project):
    if not LINTER_ENABLED:
        return jsonify({"error": "linter disabled; set LINTER_ENABLED=1"}), 400
    base = project_base_dir(project)
    try:
        import shutil as _shutil
        has_ruff = _shutil.which("ruff") is not None
    except Exception:
        has_ruff = False
    if has_ruff:
        code, out, err = _run_cmd(["ruff", "."], cwd=base, timeout=60)
    else:
        code, out, err = _run_cmd(["python", "-m", "pyflakes", "."], cwd=base, timeout=60)
    return jsonify({"code": code, "stdout": out, "stderr": err})



if __name__ == "__main__":
    os.makedirs(PROJECTS_DIR, exist_ok=True)
    app.run(host="0.0.0.0", port=5000, debug=True)
