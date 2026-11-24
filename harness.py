import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request

# Cursor proxy: http://127.0.0.1:8765/v1/responses
DEFAULT_API = "http://127.0.0.1:11434/v1/responses"
DEFAULT_MODEL = "llama3.1:8b"
MAX_AUTO_CONTINUE = int(os.getenv("HARNESS_MAX_AUTO_CONTINUE", "16"))
CONTINUE_PHRASES = re.compile(
    r"\b(let me try|i'll try|i will try|try again|different command|try with|one more)\b",
    re.I,
)
FINAL_ANSWER_RE = re.compile(
    r"(?i)\b(?:"
    r"only in|only file|the search found|was found in|only (?:that |the )(?:file|document)|"
    r"apenas (?:em|no|o arquivo)|único arquivo|not found in any other|"
    r"no other files?(?: contain)?|somente (?:o |em )"
    r")\b"
)
NUDGE_MESSAGE = (
    "[harness] Continue now: the user is still waiting. "
    "Update your checklist (mark completed steps [x]), then run_bash for the "
    "next open step only. Do not repeat a command that already succeeded "
    "this turn. Do not reply with JSON in the message; use the tool."
)
PLAN_NUDGE_MESSAGE = (
    "[harness] Write ONE plan in plain text (Goal + numbered steps) for the "
    "user's last message only. No run_bash. No echo. Steps: discover, verify, "
    "execute, verify, report."
)
PLAN_EXECUTE_NUDGE = (
    "[harness] Plan locked. Run run_bash for the next open step only "
    "(real commands: pwd, ls, mkdir, loops, converters). "
    "Reply with at most 2 lines: Done: … Pending: … — then the tool."
)
PLAN_PROGRESS_NUDGE = (
    "[harness] No more plans or step lists. Format: Done: (what finished). "
    "Pending: (what remains). Next: one run_bash for the first pending item only."
)
PLAN_REEXEC_NUDGE = PLAN_PROGRESS_NUDGE
PLAN_STEP_RE = re.compile(
    r"(?im)(?:"
    r"^\s*\d+[\.\):]\s"
    r"|^\s*[-*]\s+\S"
    r"|^\s*\[[\sxX]\]\s"
    r"|^\s*(?:etapa|passo|step)\s*\d"
    r"|\bgoal:\s"
    r"|\bplano\s*:"
    r"|\bplan\s*:"
    r")"
)
SCOPE_VERIFY_MESSAGE = (
    "[harness] Plain-text reply only (no run_bash). "
    "Does your work fully match the user's last request? "
    "If anything is missing (e.g. batch job: not all files converted), "
    "your next step must be run_bash — do not say done. "
    "If complete, give a short summary with paths and counts."
)
MKDIR_RETRY_NUDGE = (
    "[harness] Output folder missing but user allowed creating it. "
    "Call run_bash now: mkdir -p <that folder>, then re-run the extract loop. "
    "Do not end the turn with Pending: mkdir in text only."
)
BATCH_TRANSFORM_NUDGE = (
    "[harness] Batch job still incomplete. User asked for one output per input. "
    "Use: mkdir -p OUT; for f in SRC/*; do [ -f \"$f\" ] || continue; "
    "run the converter once per file; write OUT/$(basename \"${f%.*}\").EXT; "
    "done; then ls -1 OUT and compare count to inputs. "
    "Do not copy originals unless asked. Do not use pdftotext with multiple PDFs "
    "in one call. Call run_bash now with the loop."
)
BATCH_INTENT_RE = re.compile(
    r"(?i)\b(?:"
    r"each|every|one by one|per file|for each|for every|for all|"
    r"witch one|which one|"
    r"cada(?:\s+um)?|todos os arquivos|um por um|"
    r"with the name of the original|same name|"
    r"salvar.*\.txt|save.*\.txt|"
    r"extract.*content|extracted_files|name of (?:the )?original"
    r")\b"
)
PREMATURE_DONE_RE = re.compile(
    r"(?i)\b(?:"
    r"task is done|therefore[,\s]+the task is done|"
    r"tarefa (?:está )?conclu"
    r")\b"
)
HARNESS_ECHO_RE = re.compile(
    r"(?i)(?:"
    r"\[harness\]|self-check before finishing|"
    r"restate the user'?s request"
    r")"
)

def load_system():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "harness.txt")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    return (
        "You are a CLI assistant with one tool: run_bash. "
        "Use run_bash for files and commands; read returncode, stdout, and stderr."
    )


TOOLS = [{
    "type": "function",
    "name": "run_bash",
    "description": (
        "Run a bash command on the user's computer and return stdout, stderr, and returncode. "
        "Only after a step-by-step plan for the current user request; one plan step per call when possible."
    ),
    "parameters": {
        "type": "object",
        "properties": {"command": {"type": "string", "description": "The exact bash command"}},
        "required": ["command"],
        "additionalProperties": False,
    },
    "strict": True,
}]


def load_env(path=".env"):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def streaming_enabled():
    return os.getenv("LLM_STREAM", "1").lower() not in ("0", "false", "no")


def _request_headers():
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("OPENAI_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def api_url():
    return os.getenv("LLM_API", DEFAULT_API)


def ollama_base_url():
    url = api_url().rstrip("/")
    for suffix in ("/v1/responses", "/v1/chat/completions"):
        if url.endswith(suffix):
            return url[: -len(suffix)]
    return None


def ollama_models_with_tools():
    base = ollama_base_url()
    if not base:
        return []
    try:
        with urllib.request.urlopen(f"{base}/api/tags", timeout=10) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return []
    out = []
    for entry in data.get("models", []):
        caps = entry.get("capabilities") or []
        if "tools" in caps:
            out.append(entry.get("name") or entry.get("model"))
    return out


def ensure_model_support(model):
    base = ollama_base_url()
    if not base:
        return
    with_tools = ollama_models_with_tools()
    if not with_tools:
        return
    if model in with_tools:
        return
    prefix = model if ":" in model else f"{model}:"
    if any(name == model or name.startswith(prefix) for name in with_tools):
        return
    lines = [
        f"Modelo '{model}' não declara suporte a tools no Ollama.",
        "Este harness precisa de tool calling (run_bash).",
        "",
        "Modelos instalados com 'tools':",
        *[f"  - {name}" for name in with_tools],
        "",
        f"Sugestão: no .env use LLM_MODEL={with_tools[0]}",
        "(llama3:8b é só completion; use llama3.1:8b ou outro com tools).",
    ]
    raise SystemExit("\n".join(lines))


def _error_message(body):
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body.decode(errors="replace")
    err = payload.get("error")
    if isinstance(err, dict):
        return err.get("message") or json.dumps(err, ensure_ascii=False)
    return str(err or payload)


def explain_http_error(status, body, model):
    message = _error_message(body)
    if "does not support tools" in message.lower():
        hint = ollama_models_with_tools()
        extra = (
            f"\n\nCausa: o Ollama só aceita 'tools' em modelos com capacidade tools.\n"
            f"Você pediu: {model!r}\n"
        )
        if hint:
            extra += "No seu PC, instale/ use um destes:\n" + "\n".join(f"  LLM_MODEL={m}" for m in hint)
        else:
            extra += (
                "Exemplos para ~16 GB RAM (CPU): ollama pull llama3.1:8b ou llama3.2:3b\n"
                "Depois: LLM_MODEL=llama3.1:8b"
            )
        return message + extra
    return f"HTTP {status}: {message}"


def _open_request(payload):
    req = urllib.request.Request(
        api_url(),
        data=json.dumps(payload).encode(),
        headers=_request_headers(),
    )
    return urllib.request.urlopen(req)


def consume_sse(resp):
    import harness_status

    final = None
    pending_event = None
    assistant_open = False
    tool_announced = False
    signaled = False

    while True:
        raw = resp.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            pending_event = None
            continue
        if line.startswith("event:"):
            pending_event = line[6:].strip()
            continue
        if not line.startswith("data:"):
            continue
        raw_data = line[5:].strip()
        if not raw_data or raw_data == "[DONE]":
            continue
        try:
            data = json.loads(raw_data)
        except json.JSONDecodeError:
            continue
        evt = data.get("type") or pending_event or ""

        if evt == "response.created" and not signaled:
            harness_status.llm_event("created")
            signaled = True
        elif evt == "response.output_text.delta":
            delta = data.get("delta", "")
            if delta:
                if not assistant_open:
                    if not signaled:
                        harness_status.llm_event("first_token")
                        signaled = True
                    sys.stdout.write("\nassistant> ")
                    assistant_open = True
                sys.stdout.write(delta)
                sys.stdout.flush()
        elif evt == "response.output_item.added":
            item = data.get("item") or {}
            if item.get("type") == "function_call" and not tool_announced:
                if not signaled:
                    harness_status.llm_event("first_token")
                    signaled = True
                if not assistant_open:
                    sys.stdout.write("\n")
                name = item.get("name") or "run_bash"
                sys.stdout.write(f"▸ {name} …\n")
                sys.stdout.flush()
                tool_announced = True
        elif evt == "response.function_call_arguments.done":
            try:
                args = json.loads(data.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            cmd = args.get("command")
            if cmd:
                sys.stdout.write(f"$ {cmd}\n")
                sys.stdout.flush()
        elif evt == "response.completed":
            final = data.get("response")
        elif evt == "response.error":
            raise SystemExit(data.get("error") or data)

    if assistant_open or tool_announced:
        sys.stdout.write("\n")
        sys.stdout.flush()

    if final is None:
        raise SystemExit("stream ended without response.completed")
    return final


def api(payload, *, stream=None, status_reason: str = "chat"):
    import harness_status

    use_stream = streaming_enabled() if stream is None else stream
    try:
        harness_status.llm_wait(stream=use_stream, reason=status_reason)
        if not use_stream:
            with _open_request(payload) as resp:
                out = json.load(resp)
                harness_status.llm_event("done")
                return out
        payload = {**payload, "stream": True}
        with _open_request(payload) as resp:
            out = consume_sse(resp)
            harness_status.llm_event("done")
            return out
    except urllib.error.HTTPError as e:
        body = e.read()
        model = payload.get("model", DEFAULT_MODEL)
        raise SystemExit(explain_http_error(e.code, body, model))


def shell_executable():
    if os.name != "nt":
        return "/bin/bash"
    for candidate in (
        os.environ.get("BASH_PATH"),
        r"C:\Program Files\Git\bin\bash.exe",
        shutil.which("bash"),
    ):
        if not candidate or not os.path.isfile(candidate):
            continue
        low = candidate.lower()
        if low.endswith(r"\system32\bash.exe") or "windowsapps" in low:
            continue
        return candidate
    return None


def _popen_args(command):
    kwargs = {"text": True, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    if os.name == "nt":
        bash = shell_executable()
        if bash:
            return [bash, "-lc", command], kwargs
        kwargs["shell"] = True
        return command, kwargs
    kwargs["shell"] = True
    kwargs["executable"] = "/bin/bash"
    return command, kwargs


def _pump(pipe, parts, out):
    try:
        for chunk in iter(lambda: pipe.read(4096), ""):
            parts.append(chunk)
            out.write(chunk)
            out.flush()
    finally:
        pipe.close()


def run(command):
    cmd, kwargs = _popen_args(command)
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except OSError as e:
        return json.dumps({
            "command": command,
            "returncode": -1,
            "stdout": "",
            "stderr": str(e),
        })

    out_parts, err_parts = [], []
    t_out = threading.Thread(target=_pump, args=(proc.stdout, out_parts, sys.stdout))
    t_err = threading.Thread(target=_pump, args=(proc.stderr, err_parts, sys.stderr))
    t_out.daemon = True
    t_err.daemon = True
    t_out.start()
    t_err.start()

    try:
        rc = proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return json.dumps({
            "command": command,
            "returncode": -1,
            "stdout": "".join(out_parts)[-8000:],
            "stderr": f"Timed out after 60s\n{''.join(err_parts)[-8000:]}",
        })

    t_out.join()
    t_err.join()
    stdout = "".join(out_parts)
    stderr = "".join(err_parts)
    return json.dumps({
        "command": command,
        "returncode": rc,
        "stdout": stdout[-8000:],
        "stderr": stderr[-8000:],
    })


def text_from_response(response):
    text = []
    for item in response.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                text.append(content.get("text", ""))
    return "".join(text).strip()


def extract_commands_from_text(text):
    if not text:
        return []
    commands = []
    for match in re.finditer(r'"command"\s*:\s*"((?:\\.|[^"\\])*)"', text):
        raw = match.group(1)
        try:
            command = json.loads(f'"{raw}"')
        except json.JSONDecodeError:
            command = raw.replace('\\"', '"').replace("\\n", "\n")
        if command and command not in commands:
            commands.append(command)
    return commands


def plan_required_enabled():
    return os.getenv("HARNESS_REQUIRE_PLAN", "1").lower() not in (
        "0",
        "false",
        "no",
    )


def assistant_has_plan(text):
    if not text or len(text.strip()) < 50:
        return False
    if not re.search(r"(?im)^\s*(?:plan|plano)\s*:", text):
        return False
    steps = len(re.findall(r"(?m)^\s*\d+[\.\):]\s", text))
    return steps >= 2


_ALLOWED_CMD_START = frozenset({
    "pwd", "ls", "find", "grep", "cat", "head", "tail", "mkdir", "cp", "mv", "rm",
    "touch", "cd", "echo", "printf", "for", "while", "if", "test", "[", "python",
    "python3", "pdftotext", "bash", "sh", "xargs", "wc", "sort", "awk", "sed", "tee",
    "chmod", "file", "basename", "dirname", "realpath", "du", "df", "stat", "which",
    "type", "true", "false", "export", "declare", "local", "read", "exec", "set", "env",
    "unzip", "tar", "curl", "wget", "cpio", "rsync", "diff", "comm", "cut", "tr",
})


def _first_shell_word(command):
    c = command.strip()
    if not c:
        return ""
    first_line = c.split("\n", 1)[0].strip()
    first = first_line.split(None, 1)[0] if first_line else ""
    return first.strip(";|&").split("/")[-1].lower()


def command_looks_like_invalid_shell(command):
    c = command.strip()
    if not c:
        return True
    first = _first_shell_word(command)
    if not first:
        return True
    if first in _ALLOWED_CMD_START:
        return False
    if first.startswith("./") or first.startswith("../"):
        return False
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", first):
        return False
    if first in ("{", "(", ":"):
        return False
    return True


def command_is_plan_theater(command):
    c = command.strip()
    if re.search(r"(?is)^\s*echo\b", c) and re.search(
        r"(?i)\b(?:goal|plan|plano)\s*:",
        c,
    ):
        return True
    if re.search(r"(?i)\bgoal\s*:", c) and re.search(r"(?i)\bplan\s*:", c):
        return True
    if re.search(r"(?is)^\s*echo\b", c) and re.search(r"\[\s*\]", c):
        return True
    if re.match(r"(?is)^(the\s+)?goal\b", c):
        return True
    if re.search(r"(?m)^\s*\d+\.\s+", c) and re.search(
        r"(?i)\b(?:discover|verify|execute|report)\b",
        c,
    ):
        return True
    if re.search(
        r"(?is)echo\b.*\b(?:discover|verify|execute|the goal)\b",
        c,
    ):
        return True
    if command_looks_like_invalid_shell(command):
        return True
    return False


def command_is_shell_plan(command):
    return command_is_plan_theater(command)


def index_last_user_message(history):
    for i in range(len(history) - 1, -1, -1):
        item = history[i]
        if item.get("role") != "user":
            continue
        content = (item.get("content") or "").strip()
        if content.startswith("[harness]"):
            continue
        return i
    return -1


def command_succeeded_this_turn(history, command):
    start = index_last_user_message(history)
    if start < 0:
        return False
    want = command.strip()
    for item in history[start + 1 :]:
        if item.get("type") != "function_call_output":
            continue
        try:
            out = json.loads(item.get("output", "{}"))
        except json.JSONDecodeError:
            continue
        if out.get("returncode") == 0 and (out.get("command") or "").strip() == want:
            return True
    return False


def block_tool_missing_plan(call_id, command, history):
    msg = (
        "HARNESS: publish a numbered step-by-step plan before the first "
        "run_bash for this request (include collect/verify steps for unknowns)."
    )
    output = json.dumps({
        "command": command,
        "returncode": 1,
        "stdout": "",
        "stderr": msg,
    })
    history.append({
        "type": "function_call_output",
        "call_id": call_id,
        "output": output,
    })


def last_user_message(history):
    for item in reversed(history):
        if item.get("role") != "user":
            continue
        content = (item.get("content") or "").strip()
        if content.startswith("[harness]"):
            continue
        return content
    return ""


def last_tool_result(history):
    for item in reversed(history):
        if item.get("type") != "function_call_output":
            continue
        try:
            return json.loads(item.get("output", "{}"))
        except json.JSONDecodeError:
            return {}
    return None


def assistant_delivered_answer(text):
    return bool(FINAL_ANSWER_RE.search(text or ""))


def known_listing_names(history):
    names = set()
    for item in history:
        if item.get("type") != "function_call_output":
            continue
        try:
            out = json.loads(item.get("output", "{}"))
        except json.JSONDecodeError:
            continue
        for line in (out.get("stdout") or "").splitlines():
            line = line.strip()
            if not line or line.lower().startswith("total"):
                continue
            parts = line.split()
            if not parts:
                continue
            name = parts[-1].split("/")[-1]
            if name in (".", ".."):
                continue
            names.add(name)
            names.add(name.lower())
    return names


_SKIP_PATH_PREFIXES = (".harness_tmp/", ".harness_tmp\\")


def paths_referenced_in_command(command):
    bases = []
    seen = set()
    for match in re.finditer(r"""(['"])([^'"]+)\1""", command):
        path = match.group(2).replace("\\", "/")
        if "/" in path or re.search(r"\.[A-Za-z0-9]{1,12}$", path):
            base = path.split("/")[-1]
            if base not in seen:
                seen.add(base)
                bases.append((path, base))
    for match in re.finditer(
        r"(?<![\w./\\-])([^\s'\"\\|;&$><]+\.[A-Za-z0-9]{1,12})",
        command,
    ):
        token = match.group(1).replace("\\", "/")
        if "*" in token or "?" in token:
            continue
        base = token.split("/")[-1]
        if base not in seen:
            seen.add(base)
            bases.append((token, base))
    return bases


PLACEHOLDER_PATH_RE = re.compile(
    r"(/path/to|path/to/|/actual/|"
    r"<[^>]{1,40}>|\bDIR/|\bPATH\b)",
    re.I,
)


def command_uses_placeholder_paths(command):
    return bool(PLACEHOLDER_PATH_RE.search(command))


def _path_allowed_without_listing(path):
    norm = path.replace("\\", "/").lower()
    return any(norm.startswith(p) for p in _SKIP_PATH_PREFIXES)


def _parent_dir_name(path):
    norm = path.replace("\\", "/")
    if "/" not in norm:
        return ""
    return norm.rsplit("/", 1)[0].split("/")[-1]


def known_directory_names(history):
    dirs = set()
    for item in history:
        if item.get("type") != "function_call_output":
            continue
        try:
            out = json.loads(item.get("output", "{}"))
        except json.JSONDecodeError:
            continue
        for line in (out.get("stdout") or "").splitlines():
            line = line.strip()
            if not line or line.lower().startswith("total"):
                continue
            parts = line.split()
            if not parts:
                continue
            if parts[0].startswith("d"):
                name = parts[-1].split("/")[-1]
                if name not in (".", ".."):
                    dirs.add(name)
                    dirs.add(name.lower())
    for name in known_listing_names(history):
        if "." not in name:
            dirs.add(name)
            dirs.add(name.lower())
    return dirs


def command_path_validation_error(command, history):
    known = known_listing_names(history)
    if not known:
        return None
    known_dirs = known_directory_names(history)
    is_pdftotext = bool(re.search(r"(?i)\bpdftotext\b", command))
    for path, base in paths_referenced_in_command(command):
        if _path_allowed_without_listing(path):
            continue
        if is_pdftotext and base.lower().endswith(".pdf"):
            if base not in known and base.lower() not in known:
                return (
                    f"HARNESS: input `{base}` was not in a recent directory listing — "
                    "run `ls` on the source folder and use an exact PDF name."
                )
            continue
        if is_pdftotext and not base.lower().endswith(".pdf"):
            parent = _parent_dir_name(path)
            if parent and (
                parent in known_dirs or parent.lower() in known_dirs
            ):
                continue
            if parent and re.search(
                rf"(?i)mkdir\s+(?:-p\s+)?{re.escape(parent)}\b",
                command,
            ):
                continue
            continue
        parent = _parent_dir_name(path)
        if parent and (parent in known_dirs or parent.lower() in known_dirs):
            continue
        if base not in known and base.lower() not in known:
            return (
                f"HARNESS: `{base}` was not in a recent directory listing — run "
                "`ls` / `find` on the relevant folder, use an exact name, or "
                "search similar names (`find . -iname '*fragment*'`). "
                "Stay aligned with the user's request."
            )
    return None


def tool_outcome_failed(result):
    if not result:
        return False
    if result.get("returncode", 0) != 0:
        return True
    blob = f"{result.get('stderr', '')}\n{result.get('stdout', '')}".lower()
    if "usage:" in blob and "pdftotext" in blob:
        return True
    if "pdftotext version" in blob and "usage:" in blob:
        return True
    if "no such file or directory" in blob:
        return True
    if "harness:" in blob and "placeholder path" in blob:
        return False
    if "harness:" in blob and "not in a recent directory listing" in blob:
        return False
    if "i/o error" in blob:
        return True
    if "binary operator expected" in blob:
        return True
    return False


def scope_verify_enabled():
    return os.getenv("HARNESS_SCOPE_VERIFY", "1").lower() not in (
        "0",
        "false",
        "no",
    )


def premature_batch_completion(assistant_text, history):
    if not PREMATURE_DONE_RE.search(assistant_text or ""):
        return False
    user = last_user_message(history)
    if not BATCH_INTENT_RE.search(user):
        return False
    last = last_tool_result(history)
    if last and tool_outcome_failed(last):
        return True
    cmd = (last or {}).get("command", "") or ""
    if "for " not in cmd and "while " not in cmd:
        return True
    if re.search(r"pdftotext\s+[^\s|]+\s+-\s*\|\s*tee", cmd):
        return True
    return False


def scope_theater_only(assistant_text):
    text = assistant_text or ""
    if not text.strip():
        return False
    hits = len(re.findall(r"(?i)scope, period, folder", text))
    return hits >= 2 or (
        "match the request" in text.lower()
        and "next command" in text.lower()
        and 'run_bash' not in text
    )


def continue_nudge_message(assistant_text, history):
    if batch_job_still_incomplete(history, assistant_text):
        return MKDIR_RETRY_NUDGE
    last = last_tool_result(history) or {}
    err = (last.get("stderr") or "") or ""
    if err.startswith("HARNESS: plans belong") or err.startswith(
        "HARNESS: not a valid shell"
    ) or err.startswith("HARNESS: do not put plans"):
        return PLAN_PROGRESS_NUDGE
    if premature_batch_completion(assistant_text, history):
        return BATCH_TRANSFORM_NUDGE
    if assistant_has_plan(assistant_text) and plan_execute_active_in_history(history):
        return PLAN_REEXEC_NUDGE
    return NUDGE_MESSAGE


def plan_execute_active_in_history(history):
    for item in reversed(history):
        if item.get("role") == "user":
            content = (item.get("content") or "").strip()
            if content.startswith("[harness]"):
                if "Plan locked" in content or PLAN_EXECUTE_NUDGE[:40] in content:
                    return True
                if "Write ONE plan" in content or PLAN_NUDGE_MESSAGE[:40] in content:
                    continue
            else:
                break
    return False


def user_allowed_create_output_dir(history):
    return bool(
        re.search(
            r"(?i)(?:"
            r"create if (?:not )?exist|"
            r"cria(?:r)?\s+.*(?:se n[aã]o exist|caso n[aã]o exist)|"
            r"\(create if not exist\)"
            r")",
            last_user_message(history),
        )
    )


def assistant_announced_pending_action(assistant_text):
    t = assistant_text or ""
    if re.search(
        r"(?i)\b(?:pending|next)\s*:.*\b(?:mkdir|pdftotext|\bfor\b)",
        t,
    ):
        return True
    if re.search(r"(?i)pending\s*:.*run_bash", t):
        return True
    return False


def batch_write_failed_this_turn(history):
    start = index_last_user_message(history)
    if start < 0:
        return False
    failed = False
    for item in history[start + 1 :]:
        if item.get("type") != "function_call_output":
            continue
        try:
            out = json.loads(item.get("output", "{}"))
        except json.JSONDecodeError:
            continue
        cmd = out.get("command") or ""
        if "pdftotext" not in cmd and ">" not in cmd:
            continue
        blob = f"{out.get('stderr', '')}\n{out.get('stdout', '')}".lower()
        if out.get("returncode", 0) == 0 and "no such file or directory" not in blob:
            return False
        if "no such file or directory" in blob or out.get("returncode", 0) != 0:
            failed = True
    return failed


def batch_job_still_incomplete(history, assistant_text):
    if not BATCH_INTENT_RE.search(last_user_message(history)):
        return False
    if not batch_write_failed_this_turn(history):
        return False
    if assistant_announced_pending_action(assistant_text):
        return True
    if user_allowed_create_output_dir(history):
        return True
    return False


def mkdir_hint_for_result(result, history):
    if not user_allowed_create_output_dir(history):
        return result
    cmd = result.get("command") or ""
    if "pdftotext" not in cmd and ">" not in cmd:
        return result
    blob = f"{result.get('stderr', '')}\n{result.get('stdout', '')}"
    if "no such file or directory" not in blob.lower():
        return result
    m = re.search(r"ls: cannot access '([^']+)'", blob)
    if not m:
        m = re.search(
            r"([\w.-]+)/[\w.-]+\.(?:txt|out|json|csv)\s*:",
            blob,
        )
    if m:
        folder = m.group(1).split("/")[0]
        extra = (
            f"\nHARNESS: output directory `{folder}` is missing — "
            f"user allowed create: run `mkdir -p {folder}` then retry."
        )
        result = dict(result)
        result["stderr"] = (result.get("stderr") or "") + extra
    return result


def should_auto_continue(assistant_text, history):
    if assistant_delivered_answer(assistant_text):
        return False
    if extract_commands_from_text(assistant_text):
        return False
    if assistant_announced_pending_action(assistant_text):
        return True
    if batch_job_still_incomplete(history, assistant_text):
        return True
    last = last_tool_result(history)
    err = (last.get("stderr") or "") if last else ""
    if err.startswith(
        ("HARNESS: plans belong", "HARNESS: not a valid shell", "HARNESS: do not put plans")
    ):
        return True
    if premature_batch_completion(assistant_text, history):
        return True
    if scope_theater_only(assistant_text):
        return True
    if CONTINUE_PHRASES.search(assistant_text or ""):
        return True
    if re.search(r'\{\s*"name"\s*:\s*"run_bash"', assistant_text or ""):
        return True
    if assistant_has_plan(assistant_text) and re.search(
        r"(?im)^\s*goal\s*:",
        assistant_text or "",
    ):
        if plan_execute_active_in_history(history):
            return True
    last = last_tool_result(history)
    if last and "HARNESS: path not in last directory listing" in (last.get("stderr") or ""):
        return False
    if tool_outcome_failed(last):
        return True
    return False


def execute_command(history, command, call_id):
    import harness_status

    harness_status.tool_running(command)
    if command_is_plan_theater(command):
        if command_looks_like_invalid_shell(command):
            msg = (
                "HARNESS: not a valid shell command (looks like prose or a plan). "
                "Send a short Done/Pending note in text, then run_bash with "
                "one real command for the next pending step."
            )
        else:
            msg = (
                "HARNESS: do not put plans or step lists in bash. "
                "One real command only (ls, mkdir, for-loop, pdftotext, etc.)."
            )
        output = json.dumps({
            "command": command,
            "returncode": 1,
            "stdout": "",
            "stderr": msg,
        })
        if not streaming_enabled():
            print(f"\n$ {command}")
            print(msg, file=sys.stderr)
        history.append({
            "type": "function_call_output",
            "call_id": call_id,
            "output": output,
        })
        return
    if HARNESS_ECHO_RE.search(command):
        msg = (
            "HARNESS: do not run shell commands that quote [harness] instructions — "
            "follow them in your reply or use run_bash for real work."
        )
        output = json.dumps({
            "command": command,
            "returncode": 1,
            "stdout": "",
            "stderr": msg,
        })
        if not streaming_enabled():
            print(f"\n$ {command}")
            print(msg, file=sys.stderr)
        history.append({
            "type": "function_call_output",
            "call_id": call_id,
            "output": output,
        })
        return
    if command_succeeded_this_turn(history, command):
        msg = (
            "HARNESS: this exact command already succeeded this turn — "
            "use the prior stdout and execute the next open plan step."
        )
        output = json.dumps({
            "command": command,
            "returncode": 1,
            "stdout": "",
            "stderr": msg,
        })
        if not streaming_enabled():
            print(f"\n$ {command}")
            print(msg, file=sys.stderr)
        history.append({
            "type": "function_call_output",
            "call_id": call_id,
            "output": output,
        })
        return
    if command_uses_placeholder_paths(command):
        msg = (
            "HARNESS: command uses a placeholder path or generic filename — run "
            "`pwd` and `ls` first, then rebuild the command with real paths from that output."
        )
        output = json.dumps({
            "command": command,
            "returncode": 1,
            "stdout": "",
            "stderr": msg,
        })
        if not streaming_enabled():
            print(f"\n$ {command}")
            print(msg, file=sys.stderr)
        history.append({
            "type": "function_call_output",
            "call_id": call_id,
            "output": output,
        })
        return
    path_err = command_path_validation_error(command, history)
    if path_err:
        msg = path_err
        output = json.dumps({
            "command": command,
            "returncode": 1,
            "stdout": "",
            "stderr": msg,
        })
        if not streaming_enabled():
            print(f"\n$ {command}")
            print(msg, file=sys.stderr)
        history.append({
            "type": "function_call_output",
            "call_id": call_id,
            "output": output,
        })
        return
    if not streaming_enabled():
        print(f"\n$ {command}")
    output = run(command)
    result = json.loads(output)
    result = mkdir_hint_for_result(result, history)
    if result != json.loads(output):
        output = json.dumps(result)
    if not streaming_enabled():
        if result["stdout"]:
            print(result["stdout"], end="" if result["stdout"].endswith("\n") else "\n")
        if result["stderr"]:
            print(result["stderr"], end="" if result["stderr"].endswith("\n") else "\n")
    history.append({
        "type": "function_call_output",
        "call_id": call_id,
        "output": output,
    })


def chat(history):
    nudge_count = 0
    scope_verify_done = False
    plan_satisfied = not plan_required_enabled()
    plan_execute_nudged = False
    plan_nudge_sent = False
    plan_published = False
    theater_blocks = 0
    while True:
        r = api(
            {
                "model": os.getenv("LLM_MODEL", DEFAULT_MODEL),
                "instructions": load_system(),
                "tools": TOOLS,
                "input": history,
            },
            status_reason="agent",
        )
        history += r["output"]
        calls = [x for x in r["output"] if x["type"] == "function_call"]
        assistant_text = text_from_response(r)
        if assistant_has_plan(assistant_text):
            plan_satisfied = True
            plan_published = True

        if assistant_delivered_answer(assistant_text):
            if assistant_text:
                if not streaming_enabled():
                    print(f"\nassistant> {assistant_text}\n")
            return

        if not calls:
            scraped = extract_commands_from_text(assistant_text)
            if scraped:
                if plan_required_enabled() and not plan_satisfied:
                    if nudge_count < MAX_AUTO_CONTINUE:
                        nudge_count += 1
                        history.append(
                            {"role": "user", "content": PLAN_NUDGE_MESSAGE},
                        )
                    continue
                for i, command in enumerate(scraped):
                    execute_command(history, command, f"scraped_{nudge_count}_{i}")
                continue

            if (
                nudge_count < MAX_AUTO_CONTINUE
                and should_auto_continue(assistant_text, history)
            ):
                nudge_count += 1
                history.append({
                    "role": "user",
                    "content": continue_nudge_message(assistant_text, history),
                })
                continue

            if (
                plan_required_enabled()
                and plan_satisfied
                and not plan_execute_nudged
            ):
                plan_execute_nudged = True
                history.append({"role": "user", "content": PLAN_EXECUTE_NUDGE})
                continue

            if (
                plan_execute_nudged
                and not calls
                and assistant_has_plan(assistant_text)
                and nudge_count < MAX_AUTO_CONTINUE
            ):
                nudge_count += 1
                history.append({"role": "user", "content": PLAN_PROGRESS_NUDGE})
                continue

            if (
                scope_verify_enabled()
                and not scope_verify_done
                and (assistant_text or "").strip()
                and not scope_theater_only(assistant_text)
                and not BATCH_INTENT_RE.search(last_user_message(history))
            ):
                scope_verify_done = True
                history.append({"role": "user", "content": SCOPE_VERIFY_MESSAGE})
                continue

            if not streaming_enabled():
                print(f"\nassistant> {assistant_text}\n")
            elif not assistant_text:
                sys.stdout.write("\n")
                sys.stdout.flush()
            return

        if plan_required_enabled() and not plan_satisfied:
            if not streaming_enabled() and assistant_text:
                print(f"\nassistant> {assistant_text}\n")
            import harness_status

            for call in calls:
                command = json.loads(call["arguments"])["command"]
                harness_status.tool_running(command)
                block_tool_missing_plan(call["call_id"], command, history)
            if nudge_count < MAX_AUTO_CONTINUE:
                nudge_count += 1
                if not plan_nudge_sent:
                    plan_nudge_sent = True
                    history.append({"role": "user", "content": PLAN_NUDGE_MESSAGE})
                else:
                    plan_satisfied = True
                    plan_execute_nudged = True
                    history.append(
                        {"role": "user", "content": PLAN_PROGRESS_NUDGE},
                    )
            continue

        if plan_execute_nudged or plan_published:
            for call in calls:
                cmd = json.loads(call["arguments"])["command"]
                if command_is_plan_theater(cmd):
                    theater_blocks += 1

        for call in calls:
            command = json.loads(call["arguments"])["command"]
            execute_command(history, command, call["call_id"])

        if (
            theater_blocks >= 2
            and nudge_count < MAX_AUTO_CONTINUE
            and BATCH_INTENT_RE.search(last_user_message(history))
        ):
            nudge_count += 1
            history.append({"role": "user", "content": BATCH_TRANSFORM_NUDGE})
            continue


if __name__ == "__main__":
    import harness_history
    import harness_status

    load_env()
    model = os.getenv("LLM_MODEL", DEFAULT_MODEL)
    ensure_model_support(model)
    history, session = harness_history.bootstrap_history()
    if harness_history.enabled() and (history or session.get("summary")):
        harness_status.log(
            f"Sessão carregada ({len(history)} itens) — {harness_history.session_path()}"
        )
        harness_status.print_usage(harness_history.usage_metrics(history, session))
    while True:
        try:
            user_input = input("user> ")
        except EOFError:
            break
        if not user_input.strip():
            continue
        history.append({"role": "user", "content": user_input})
        chat(history)
        if harness_history.enabled():
            history, session = harness_history.after_user_turn(history, session)
            history, session = harness_history.maybe_compact(
                history,
                session,
                api_fn=api,
                text_from_response_fn=text_from_response,
                model=model,
            )
        harness_status.print_usage_after_turn(
            history, session, history_module=harness_history
        )