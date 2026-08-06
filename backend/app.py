import os
import re
import glob
import time
import functools
import sqlite3
import subprocess
from typing import List, Tuple, Optional
from flask import Flask, request, jsonify, render_template, Response, abort
import requests
from werkzeug.utils import secure_filename
from llama_cpp import Llama
from huggingface_hub import hf_hub_download
from dotenv import load_dotenv

load_dotenv()

API_KEYS = os.getenv("API_KEY")
# Security:
RATE_LIMIT = 10  # 10 requests per minute
REQUEST_COUNTS = {}
# rate limiting decorator
def rate_limit(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        client_ip = request.remote_addr  # Get the client's IP address
        now = time.time()
        if client_ip not in REQUEST_COUNTS:
            REQUEST_COUNTS[client_ip] = {"count": 0, "timestamp": now}
        if now - REQUEST_COUNTS[client_ip]["timestamp"] > 60:  # Reset count after 1 minute
            REQUEST_COUNTS[client_ip] = {"count": 0, "timestamp": now}
        if REQUEST_COUNTS[client_ip]["count"] >= RATE_LIMIT:
            abort(429)  # Too Many Requests
        REQUEST_COUNTS[client_ip]["count"] += 1
        return func(*args, **kwargs)
    return wrapper
#api key decorator
def require_api_key(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        api_key = request.headers.get('X-API-Key')  # Assuming key is sent in header
        if api_key and api_key == API_KEYS:
            return func(*args, **kwargs)
        else:
            abort(401)  # Unauthorized
    return wrapper
# --- Cloud Config ---
# We use a 1.5B model so it doesn't crash Railway's free RAM (approx 2GB)
REPO_ID = "Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF"
FILENAME = "qwen2.5-coder-0.5b-instruct-q4_k_m.gguf"
MODEL = REPO_ID
# Ensure directories exist for Railway Volumes
os.makedirs("/app/data", exist_ok=True)
os.makedirs("/app/models", exist_ok=True)

# Auto-download model if missing
model_path = os.path.join("/app/models", FILENAME)
if not os.path.exists(model_path):
    print(f"Downloading model {FILENAME}...")
    hf_hub_download(repo_id=REPO_ID, filename=FILENAME, local_dir="/app/models")

# Initialize LLM
llm = Llama(model_path=model_path, n_ctx=8192, n_threads=4, n_batch=512, flash_attn=True)

DB_PATH = "/app/data/memory.db"
PROJECTS_DIR = os.getenv("PROJECTS_DIR", "./projects")

# LLM sampling/ctx controls
TEMPERATURE = float(os.getenv("TEMPERATURE", "0.2"))
TOP_P = float(os.getenv("TOP_P", "0.9"))
NUM_CTX = int(os.getenv("NUM_CTX", "2048")) 
SEED = int(os.getenv("SEED", "7"))                  # make outputs deterministic-ish

# Prompt/Context budgets
MAX_FILES_IN_CONTEXT = int(os.getenv("MAX_FILES_IN_CONTEXT", "10"))
MAX_FILE_BYTES = int(os.getenv("MAX_FILE_BYTES", str(16 * 1024))) 
MAX_PROMPT_CHARS = int(os.getenv("MAX_PROMPT_CHARS", str(46000)))

# Save/write guardrails
ALLOWED_EXTENSIONS = {
    "py", "ipynb", "js", "ts", "tsx", "jsx", "md", "txt", "json", "yml", "yaml",
    "html", "css", "toml", "ini", "cfg", "sh", "ps1"
}

# Optional local runner/linters (disabled by default)
RUNNER_ENABLED = os.getenv("RUNNER_ENABLED", "0") == "1"
LINTER_ENABLED = os.getenv("LINTER_ENABLED", "0") == "1"

# ---------------- Response contract/prompt ----------------
SYSTEM_PROMPT = """
You are a senior AI engineer. 
1) Always start your response with '#mode: write|review|explain|discuss|math'.
2) If code is requested, use the 'FILE: path/to/file.ext' format inside markdown blocks.
3) If a general question is asked, provide a clear and direct answer.
4) Be concise and professional.
"""
# -------------- Flask app ---------------
app = Flask(__name__, static_folder="../static", template_folder="../templates")

# -------------- SQLite memory -----------
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

# -------------- Helpers --------------
def project_base_dir(project: str) -> str:
    base = os.path.abspath(PROJECTS_DIR)
    path = os.path.abspath(os.path.join(base, project))
    if not path.startswith(base + os.sep) and path != base:
        abort(400, description="Invalid project path")
    os.makedirs(path, exist_ok=True)
    return path

def _is_allowed_file(path: str) -> bool:
    return "." in path and path.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def _sanitize_rel_path(rel_path: str) -> Optional[str]:
    rel_path = rel_path.strip().replace("\\", "/")
    rel_path = re.sub(r"^/+","", rel_path)  # strip leading slashes
    if not _is_allowed_file(rel_path):
        return None
    # Prevent directory traversal
    if ".." in rel_path.split("/"):
        return None
    return rel_path

def _rank_project_files(file_paths: List[str]) -> List[str]:
    # Rank by (1) recency, (2) path depth (shallower first), (3) name preference
    def key(p: str):
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            mtime = 0
        depth = p.count(os.sep)
        name_bonus = 0
        basename = os.path.basename(p).lower()
        if basename in {"readme.md","requirements.txt","pyproject.toml","setup.py"}:
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
        # enforce overall char budget
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
    # If no "Plan" heading, prepend a placeholder section to enforce habits.
    preface = (
        "Plan\n"
        "- Outline steps briefly.\n"
        "- Write complete code using the Multi-file format.\n"
        "- Add a tiny MWE when appropriate.\n"
        "- Include Self-Check at the end.\n\n"
    )
    return text if "Plan" in text[:400] else preface + text

def _wrap_lonely_fence_as_file(text: str) -> str:
    # If there are no FILE blocks but there is at least one code fence, wrap the first as scratch file.
    if FILE_BLOCK_RE.search(text):
        return text
    m = CODE_FENCE_RE.search(text)
    if not m:
        return text
    lang = (m.group("lang") or "text").lower()
    default_map = {
        "python": "scratch/main.py",
        "py": "scratch/main.py",
        "javascript": "scratch/index.js",
        "js": "scratch/index.js",
        "typescript": "scratch/index.ts",
        "ts": "scratch/index.ts",
        "json": "scratch/data.json",
        "html": "scratch/index.html",
        "css": "scratch/styles.css",
        "md": "scratch/README.md",
    }
    rel = default_map.get(lang, "scratch/snippet.txt")
    code = m.group("code")
    file_block = f"FILE: {rel}\n```{lang}\n{code}\n```"
    start, end = m.span()
    return text[:start] + file_block + text[end:]

def enforce_response_contract(text: str, default_mode: str = "write") -> str:
    text = _ensure_mode_header(text, default_mode=default_mode)
    text = _ensure_plan_section(text)
    text = _wrap_lonely_fence_as_file(text)
    # Normalize unlabeled fences to text to reduce parser misses
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
            # Skip dangerous or disallowed paths
            continue

        abs_path = os.path.abspath(os.path.join(base_dir, rel_path))
        if not abs_path.startswith(base_dir + os.sep) and abs_path != base_dir:
            continue

        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        # If fence had no language, try infer from extension
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
    
    # 1. Explicit coding triggers
    if re.search(r"write|code|create|build|script|function|generate|program|app", text, re.IGNORECASE):
        return "write"
    
    # 2. Review/Debug triggers
    if re.search(r"review|critique|improve|refactor|fix|bug|error", text, re.IGNORECASE):
        return "review"
    
    # 3. Explain triggers
    if re.search(r"explain|walk me through|how does|what does.*mean", text, re.IGNORECASE):
        return "explain"
        
    # 4. Default to casual chat if no coding intent is detected
    return "discuss"

def build_full_prompt(project: str, user_text: str, search_results: List[str] = None) -> Tuple[str, str]:
    hist = load_recent(project, limit=8)
    hist_lines = [f"{m['role'].upper()}: {m['content']}" for m in hist]
    files_context = load_project_files_context(project)
    mode = parse_user_mode(user_text)

    # 1. Define the search context block (The "Eyes" of the AI)
    search_context = ""
    if search_results:
        search_context = "\n--- Web Search Results ---\n" + "\n".join(search_results)

    # 2. Refined Instructions: Tell the AI it's okay to just answer questions
    if mode in ["write", "review"]:
        planning_instructions = f"""
IMPORTANT:
- Start with '#mode: {mode}'.
- ONLY if you are generating code: Write a 'Plan' section and use the Multi-file format.
- If this is a general question: Answer directly and ignore the 'Plan' requirement.
- End with a 'Self-Check' if code was written.
"""
    else:
        # Standard conversation mode
        planning_instructions = f"""
IMPORTANT:
- Start with '#mode: {mode}'.
- Use the provided search results to answer the user's question directly.
- Be concise and do not use coding formats.
"""

    # 3. Assemble the prompt (ensure search_context is in the list)
    sections = [
        SYSTEM_PROMPT.strip(),
        "\n--- Session Settings ---\n",
        f"Model: {MODEL}\nTemperature: {TEMPERATURE}\n",
        search_context,  # Correctly injected here
        "\n--- Project Context ---\n",
        files_context,
        "\n--- Conversation ---\n",
        "\n".join(hist_lines),
        "\n--- New Request ---\n",
        f"USER: {user_text}\n{planning_instructions}\nASSISTANT:"
    ]

    # 4. Join and enforce character budget
    prompt = "\n".join(s for s in sections if s and s.strip())
    if len(prompt) > MAX_PROMPT_CHARS:
        prompt = prompt[-MAX_PROMPT_CHARS:]
    
    return prompt, mode

# -------------- Routes --------------
@app.route("/")
@rate_limit
def index():
    return render_template("index.html", api_key=API_KEYS)

@app.route("/protected")
@require_api_key
def protected_resource():
    return "This is a protected resource!"

@app.route("/history/<project>", methods=["GET"])
def get_history(project):
    try:
        # Open a fresh connection to avoid "Thread" issues
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        
        # Fetch role and content, ordered by ID to keep the conversation chronological
        cur.execute("SELECT role, content FROM messages WHERE project=? ORDER BY id ASC", (project,))
        rows = cur.fetchall()
        conn.close()
        
        # Wrap the list in a dictionary with the key 'history'
        history_list = [{"role": r[0], "content": r[1]} for r in rows]
        return jsonify({"history": history_list})
        
    except Exception as e:
        print(f"Database Error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/projects")
def get_projects():
    cur.execute("SELECT DISTINCT project FROM messages ORDER BY project ASC")
    rows = cur.fetchall()
    return jsonify([r[0] for r in rows])

@app.route("/settings", methods=["GET", "POST"])
@require_api_key
def settings():
    """Get or update runtime settings without redeploy."""
    global MODEL, TEMPERATURE, TOP_P, NUM_CTX, SEED
    if request.method == "POST":
        data = request.json or {}
        MODEL = data.get("model", MODEL)
        TEMPERATURE = float(data.get("temperature", TEMPERATURE))
        TOP_P = float(data.get("top_p", TOP_P))
        NUM_CTX = int(data.get("num_ctx", NUM_CTX))
        SEED = int(data.get("seed", SEED))
    return jsonify({
        "model": MODEL,
        "temperature": TEMPERATURE,
        "top_p": TOP_P,
        "num_ctx": NUM_CTX,
        "seed": SEED
    })

@app.route("/add_project", methods=["POST"])
def add_project():
    data = request.json or {}
    project = (data.get("project") or "").strip()
    if not project:
        return jsonify({"error": "empty project name"}), 400
    project_base_dir(project)
    save_message(project, "system", f"Project {project} created.")
    return jsonify({"status": "ok", "project": project})

@app.route("/chat", methods=["POST"])
@require_api_key
def chat():
    data = request.json or {}
    project = data.get("project", "default")
    user_text = (data.get("message") or "").strip()
    if not user_text:
        return jsonify({"error": "empty message"}), 400

    save_message(project, "user", user_text)
    full_prompt, mode = build_full_prompt(project, user_text)

    try:
        # Use your initialized 'llm' object instead of ollama
        resp = llm(
            full_prompt,
            max_tokens=512,
            temperature=TEMPERATURE,
            top_p=TOP_P,
            stop=["\nUSER:", "\nSYSTEM:"],
            echo=False
        )
        assistant_text_raw = resp["choices"][0]["text"]
        assistant_text = enforce_response_contract(assistant_text_raw, default_mode=mode)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    save_message(project, "assistant", assistant_text)
    saved_files = save_generated_files(project, assistant_text)
    return jsonify({"response": assistant_text, "saved_files": saved_files})

@app.route("/stream", methods=["POST"])
@require_api_key
@rate_limit
def stream():
    data = request.json or {}
    project = data.get("project", "default")
    user_text = (data.get("message") or "").strip()
    
    save_message(project, "user", user_text)
    # Using a tiny prompt for testing to ensure speed
    full_prompt, mode = build_full_prompt(project, user_text)

    def generate():
        # 1. IMMEDIATE Keep-Alive: Tell Railway the server is alive
        yield "data:  \n\n" 
        
        try:
            # 2. Run inference with smaller max_tokens for speed
            stream_res = llm(
                full_prompt,
                max_tokens=1024, 
                temperature=0.7,
                stream=True,
                stop=["USER:", "ASSISTANT:"]
            )
            
            for chunk in stream_res:
                token = chunk.get("choices", [{}])[0].get("text", "")
                if token:
                    # 3. Clean token: replace actual newlines with escaped string
                    # This is what your script.js expects
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
@require_api_key
@rate_limit
def search_web():
    data = request.json or {}
    query = (data.get("query") or "").strip()
    
    # Register at tavily.com for a free API key
    TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

    payload = {
        "api_key": TAVILY_API_KEY,
        "query": query,
        "search_depth": "basic",
        "max_results": 3
    }
    
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
@require_api_key
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
@require_api_key
def delete_project():
    data = request.json or {}
    project = (data.get("project") or "").strip()
    if not project:
        return jsonify({"error": "empty project name"}), 400

    # Delete from DB
    cur.execute("DELETE FROM messages WHERE project=?", (project,))
    conn.commit()

    # Delete project folder from disk
    project_dir = os.path.join(PROJECTS_DIR, project)
    if os.path.exists(project_dir):
        import shutil
        shutil.rmtree(project_dir)

    return jsonify({"status": "ok", "project": project})

@app.route("/cancel", methods=["POST"])
def cancel():
    return jsonify({"status": "Session reset requested", "note": "Inference is self-contained."})

# ---------- Optional: local lint & run (guarded) ----------
def _run_cmd(cmd: List[str], cwd: Optional[str] = None, timeout: int = 20) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        return 1, "", str(e)

@app.route("/run/<project>", methods=["POST"])
@require_api_key
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
@require_api_key
@rate_limit
def lint(project):
    if not LINTER_ENABLED:
        return jsonify({"error": "linter disabled; set LINTER_ENABLED=1"}), 400
    base = project_base_dir(project)
    # Try ruff, fallback to pyflakes
    try:
        import shutil as _shutil  # local import
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
