#!/usr/bin/env python3
"""Index of Claude Code sessions: one row per transcript, joined with what is running now.

Shared by `claude-session-index` (table / JSON) and `claude-session-picker` (the tmux browser).

Source of truth is the transcripts, ~/.claude/projects/*/<sessionId>.jsonl. Only the first few
hundred lines and the last 1.5 MB of each are read (some are 35 MB); the result is cached by
(size, mtime) in ~/.claude/dash/sessions-index.json, so a refresh only re-reads what changed.
"Last used" is the timestamp of the last user/assistant record, not the file mtime: Claude Code
bumps the mtime of every open session, idle or not.

"Live" is joined from ~/.claude/sessions/<pid>.json (pid must be a running claude process) and,
for tmux sessions, the window the pane lives in.

"Context" is what every API request of the session re-sends: the snapshot claude-statusline saves in
~/.claude/dash/status/<sessionId>.json when it is newer, else the last API response in the transcript.
"""
import json, os, re, subprocess, time
from datetime import datetime
from pathlib import Path

HOME = Path.home()
PROJECTS = HOME / ".claude" / "projects"
REGISTRY = HOME / ".claude" / "sessions"
CACHE = HOME / ".claude" / "dash" / "sessions-index.json"
HEAD_LINES = 400
TAIL_BYTES = (1_500_000, 16_000_000)   # widen only when the small tail held no title
SKIP_PROMPT = ("<command-name>", "<local-command", "<system-reminder", "<task-notification", "<bash-")
CACHE_VERSION = 3
CONFIG = HOME / ".config" / "tmux-dashboard" / "config"
STATUS = HOME / ".claude" / "dash" / "status"          # claude-statusline's snapshot of each session
# /handoff notes, <sessionId>.md (digests made here are <sessionId>.digest.md), and {old sessionId: new sessionId} for threads
# continued from the browser. Tests point both elsewhere with the environment variables.
HANDOFFS = Path(os.environ.get("CLAUDE_HANDOFFS_DIR") or HOME / ".claude" / "handoffs")
LINKS = Path(os.environ.get("CLAUDE_DASH_LINKS") or HOME / ".claude" / "dash" / "continued.json")
SPEND = HOME / ".claude" / "dash" / "spend-index.json"   # per-transcript API responses read so far, for spend()
SPEND_VERSION = 1
# API list price, $/MTok (input, output, cache read), by model-id prefix - the longest prefix wins. A cache write costs
# 2x input with the 1-hour TTL, 1.25x with the 5-minute one.
PRICES = {"claude-fable-5-1": (10, 50, 0.25), "claude-mythos-5-1": (10, 50, 0.25), "claude-fable-5": (10, 50, 1.0),
          "claude-mythos-5": (10, 50, 1.0), "claude-opus-5-5": (4, 20, 0.20), "claude-opus-5": (5, 25, 0.50),
          "claude-opus-4": (5, 25, 0.50), "claude-sonnet-5": (2, 10, 0.20), "claude-sonnet-4": (3, 15, 0.30),
          "claude-haiku-5-5": (0.10, 0.50, 0.01), "claude-haiku-4": (1, 5, 0.10)}
# Experiment codes: 2-4 letters + 6 digits (Exp011530, Gen022830 ...). Spelling of the ones we know:
KNOWN_CODES = {"exp": "Exp", "gen": "Gen", "cyt": "Cyt", "seq": "Seq"}
DIR_CODE = re.compile(r"([A-Za-z]{2,4})(\d{6})(?!\d)")
TEXT_CODE = re.compile(r"(?<![A-Za-z])([A-Za-z]{2,4})(\d{6})(?!\d)")


def _epoch(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _text(msg):
    c = (msg or {}).get("content")
    if isinstance(c, list):
        c = " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
    return c if isinstance(c, str) else ""


def _clean(s, n):
    return re.sub(r"\s+", " ", s).strip()[:n]


def _scan_head(f):
    out = {"first_ts": None, "cwd": None, "first_prompt": "", "entrypoint": None}
    with open(f, "rb") as fh:
        for i, raw in enumerate(fh):
            if i >= HEAD_LINES:
                break
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            out["first_ts"] = out["first_ts"] or d.get("timestamp")
            out["cwd"] = out["cwd"] or d.get("cwd")
            out["entrypoint"] = out["entrypoint"] or d.get("entrypoint")
            if not out["first_prompt"] and d.get("type") == "user" and not d.get("isMeta"):
                t = _text(d.get("message")).strip()
                if t and not t.startswith(SKIP_PROMPT):
                    out["first_prompt"] = _clean(t, 200)
            if out["first_ts"] and out["cwd"] and out["first_prompt"]:
                break
    return out


def _scan_tail(f, size, nbytes):
    start = max(0, size - nbytes)
    with open(f, "rb") as fh:
        fh.seek(start)
        lines = fh.read().split(b"\n")
    if start:
        lines = lines[1:]          # first line is cut mid-record
    r = {}
    for raw in reversed(lines):    # newest first: the first hit of each kind is the current value
        if not raw:
            continue
        if b'"type":"ai-title"' in raw:
            key = "ai"
        elif b'"type":"custom-title"' in raw:
            key = "custom"
        elif b'"type":"agent-name"' in raw:
            key = "agent"
        elif b'"type":"last-prompt"' in raw:
            key = "last_prompt"
        elif b'"type":"cost-state"' in raw:
            key = "cost"
        elif b'"type":"continued-in"' in raw:
            key = "continued_in"
        elif b'"type":"assistant"' in raw and b'"usage"' in raw and "usage" not in r:
            key = "usage"
        elif b'"type":"user"' in raw or b'"type":"assistant"' in raw:
            key = "last_ts"
        else:
            continue
        if key in r:
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        t = d.get("type")
        if key == "ai" and t == "ai-title":
            r[key] = d.get("aiTitle")
        elif key == "custom" and t == "custom-title":
            r[key] = d.get("customTitle")
        elif key == "agent" and t == "agent-name":
            r[key] = d.get("agentName")
        elif key == "last_prompt" and t == "last-prompt" and d.get("lastPrompt"):
            r[key] = _clean(d["lastPrompt"], 300)
        elif key == "cost" and t == "cost-state":
            r[key] = float(d.get("totalCostUSD") or 0)
        elif key == "continued_in" and t == "continued-in":
            r[key] = d.get("continuedInSessionId")
        elif key == "usage" and t == "assistant":
            m = d.get("message") or {}
            u = m.get("usage")
            if isinstance(u, dict) and m.get("model") not in (None, "<synthetic>") and not d.get("isSidechain"):
                r[key] = {"tokens": sum(u.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")),
                          "model": m["model"], "ts": d.get("timestamp")}
            if d.get("timestamp"):
                r.setdefault("last_ts", d["timestamp"])     # this line was not offered to last_ts, and may be the newest record
        elif key == "last_ts" and t in ("user", "assistant") and d.get("timestamp"):
            r[key] = d["timestamp"]
    return r


def _scan(f, size):
    info = _scan_head(f)
    for nbytes in TAIL_BYTES:
        tail = _scan_tail(f, size, nbytes)
        if tail.get("custom") or tail.get("ai") or tail.get("agent") or nbytes >= size:
            break
    info.update(tail)
    return info


def _load_cache():
    try:
        d = json.loads(CACHE.read_text())
        return d["files"] if d.get("v") == CACHE_VERSION else {}
    except Exception:
        return {}


def _save_cache(files):
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE.with_suffix(f".{os.getpid()}.tmp")      # the browser and the dash panel both write it
        tmp.write_text(json.dumps({"v": CACHE_VERSION, "files": files}))
        os.replace(tmp, CACHE)
    except OSError:
        pass


def _run(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return ""


def live_map():
    """sessionId -> {pid, status, kind, tmux_session, window_index} for running claude processes."""
    running = set()
    for line in _run("ps", "-axo", "pid=,command=").splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if pid.isdigit() and "claude" in cmd:
            running.add(int(pid))
    windows = {}
    for line in _run("tmux", "list-windows", "-a", "-F", "#{window_id} #{session_name} #{window_index}").splitlines():
        parts = line.split(" ", 2)
        if len(parts) == 3:
            windows[parts[0]] = (parts[1], int(parts[2]))
    live = {}
    for f in REGISTRY.glob("*.json"):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        sid, pid = d.get("sessionId"), d.get("pid")
        if not sid or pid not in running:
            continue
        entry = {"pid": pid, "status": d.get("status"), "kind": d.get("kind"), "tmux_session": None, "window_index": None, "window_id": None, "pane_id": None}
        m = re.match(r"(.+):(@\d+)\.(%\d+)$", d.get("tmux") or "")
        if m and m.group(2) in windows:
            entry["tmux_session"], entry["window_index"] = windows[m.group(2)]
            entry["window_id"], entry["pane_id"] = m.group(2), m.group(3)
        # two registry files can name one session (a stale resume); prefer the one that has a tmux window
        if sid not in live or (entry["window_index"] is not None and live[sid]["window_index"] is None):
            live[sid] = entry
    return live


def setting(name, default):
    """KEY from the environment or ~/.config/tmux-dashboard/config (KEY=VALUE), else the default."""
    if os.environ.get(name):
        return os.environ[name]
    try:
        for line in CONFIG.read_text().splitlines():
            if line.strip().startswith(name + "="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return default


# Every step of a session (each prompt and each tool call is one API request) re-reads its whole context from the cache,
# and the first step after the cache expired rewrites all of it, so a long session gets dearer per step. Thresholds in
# thousands of tokens, from ~/.config/tmux-dashboard/config: CTX_WARN_K (yellow, /compact soon) and CTX_CLEAR_K (red).
CTX_WARN = int(setting("CTX_WARN_K", "200")) * 1000
CTX_CLEAR = int(setting("CTX_CLEAR_K", "400")) * 1000


def _price(model):
    m = re.sub(r"\[.*\]$", "", model or "")                  # claude-opus-5-5[1m] -> claude-opus-5-5
    for k in sorted(PRICES, key=len, reverse=True):
        if m.startswith(k):
            return PRICES[k]
    return None


def context(r, now=None):
    """{tokens, model, warm, left, step, wake, level} for a row, or None before its first API response. `step` is what each
    request pays to re-read the context while the cache is warm; `wake` is what the first one pays once it went cold."""
    now = now or time.time()
    u, ttl, expires = r.get("usage"), "1h", None
    last_req = _epoch(u["ts"] or "") if u else None            # the session's last API response, from the transcript
    try:
        s = json.loads((STATUS / f"{r['id']}.json").read_text())
        cur = (s.get("context_window") or {}).get("current_usage") or {}
        tokens = sum(cur.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        if tokens and s.get("_saved_at", 0) >= (last_req or 0) - 60:    # the statusline saw it last: it knows the cache too (all zeros = resumed, no request yet)
            u = {"tokens": tokens, "model": (s.get("model") or {}).get("id")}
            pc = s.get("prompt_cache") or {}
            ttl, expires = pc.get("ttl") or ttl, pc.get("expires_at")    # None until this process has sent a request (just resumed)
    except (OSError, ValueError, TypeError):
        pass
    if not u or not u["tokens"]:
        return None
    if not expires and last_req:                               # never from when the snapshot was saved: a resume sends nothing
        expires = last_req + (3600 if ttl == "1h" else 300)
    price = _price(u["model"]) or PRICES["claude-opus-5-5"]
    tokens = u["tokens"]
    return {"tokens": tokens, "model": u["model"], "warm": bool(expires and expires > now), "left": (expires or now) - now,
            "step": tokens * price[2] / 1e6, "wake": tokens * price[0] * (2 if ttl == "1h" else 1.25) / 1e6,
            "level": 2 if tokens >= CTX_CLEAR else 1 if tokens >= CTX_WARN else 0}


def money(usd):
    return f"{usd * 100:.0f}¢" if usd < 1 else f"${usd:.1f}" if usd < 10 else f"${usd:.0f}"


def ctx_cells(r):
    """(size, price, warm minutes left, total so far) strings for a row's context columns; blanks when unknown."""
    c = r.get("ctx")
    size, price = ctx_label(c).split() if c else ("", "")
    return size, price, ago(c["left"]) if c and c["warm"] else "", money(r["spent"]) if r.get("spent") else ""


def ctx_label(c):
    """'312K 6¢' while the cache is warm (cost of each step), '312K ↻$2.5' once it is cold (cost of waking it up)."""
    if not c:
        return ""
    t = c["tokens"]
    size = f"{t / 1e3:.0f}K" if t < 999_500 else f"{t / 1e6:.1f}M"
    return f"{size:>5} {money(c['step']) if c['warm'] else '↻' + money(c['wake'])}"


def usd(model, t):
    """API list price of one response; t = [input, output, cache write 5m, cache write 1h, cache read] tokens."""
    p = _price(model) or PRICES["claude-opus-5-5"]
    return (t[0] * p[0] + t[1] * p[1] + t[2] * p[0] * 1.25 + t[3] * p[0] * 2 + t[4] * p[2]) / 1e6


def _response(raw):
    """(id, model, local month, tokens) of one API response line of a transcript, or None."""
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    m = d.get("message") if d.get("type") == "assistant" else None
    if not isinstance(m, dict) or not isinstance(m.get("usage"), dict) or not m.get("id") or m.get("model") in (None, "<synthetic>"):
        return None
    u = m["usage"]
    cc = u.get("cache_creation") or {}
    w5, w1 = (cc.get("ephemeral_5m_input_tokens") or 0, cc.get("ephemeral_1h_input_tokens") or 0) if cc else (u.get("cache_creation_input_tokens") or 0, 0)
    ts = _epoch(d.get("timestamp") or "")
    return (m["id"], m["model"], time.strftime("%Y-%m", time.localtime(ts)) if ts else "?",
            [u.get("input_tokens") or 0, u.get("output_tokens") or 0, w5, w1, u.get("cache_read_input_tokens") or 0])


def spend():
    """{"2026-10": usd, ...}: every API response in ~/.claude/projects (subagents included) at API list price, by local month.
    Incremental: each transcript is read on from where the last call stopped, and files Claude Code has since deleted keep
    counting. A response written over several lines (streaming) or copied into another file (a fork, a resume) counts
    once, at its largest token counts. A response whose final line was never written keeps its placeholder output count,
    so this runs a few percent low."""
    best = {}
    for c in _spend_files().values():
        for rid, (model, month, *t) in c["ids"].items():
            old = best.get(rid)
            best[rid] = (model, month, list(map(max, old[2], t)) if old else t)
    by = {}
    for model, month, t in best.values():
        by[month] = by.get(month, 0) + usd(model, t)
    return by


def spend_by_session():
    """{sessionId: usd} at API list price, each session with its subagents (project/<id>.jsonl and everything under
    project/<id>/). Same counting as spend(), except that a response copied into a fork counts in both sessions."""
    per = {}
    for key, c in _spend_files().items():
        parts = Path(key).relative_to(PROJECTS).parts
        sid = Path(parts[1]).stem if len(parts) == 2 else parts[1]
        d = per.setdefault(sid, {})
        for rid, (model, month, *t) in c["ids"].items():
            old = d.get(rid)
            d[rid] = (model, list(map(max, old[1], t)) if old else t)
    return {sid: sum(usd(m, t) for m, t in d.values()) for sid, d in per.items()}


def _spend_files():
    """Bring ~/.claude/dash/spend-index.json up to date with the transcripts and return its files: {path: {ino, off, ids}}."""
    try:
        d = json.loads(SPEND.read_text())
        files = d["files"] if d.get("v") == SPEND_VERSION else {}
    except Exception:
        files = {}
    changed = False
    for f in PROJECTS.glob("**/*.jsonl"):
        try:
            st = f.stat()
        except OSError:
            continue
        c = files.get(str(f))
        if not c or c["ino"] != st.st_ino or c["off"] > st.st_size:          # new, replaced or truncated: from the start
            c = files[str(f)] = {"ino": st.st_ino, "off": 0, "ids": {}}
        if c["off"] == st.st_size:
            continue
        with open(f, "rb") as fh:
            fh.seek(c["off"])
            chunk = fh.read()
        end = chunk.rfind(b"\n") + 1                                          # a line still being written waits for next time
        for raw in chunk[:end].split(b"\n"):
            if b'"usage"' in raw and b'"type":"assistant"' in raw:
                got = _response(raw)
                if got:
                    rid, model, month, t = got
                    old = c["ids"].get(rid)
                    c["ids"][rid] = [model, month, *(map(max, old[2:], t) if old else t)]
        c["off"] += end
        changed = changed or end > 0
    if changed:
        try:
            tmp = SPEND.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps({"v": SPEND_VERSION, "files": files}))
            os.replace(tmp, SPEND)
        except OSError:
            pass
    return files


# ---- threads: /handoff notes, and sessions continued in a fresh one ----
def handoffs():
    """sessionId -> {status, next, title, continues, path} from the front matter of each ~/.claude/handoffs/<sessionId>.md."""
    out = {}
    for f in HANDOFFS.glob("*.md"):
        if f.name.endswith(".digest.md"):
            continue
        try:
            text = f.read_text()
        except OSError:
            continue
        meta = {}
        if text.startswith("---"):
            for line in text.split("\n---", 1)[0].splitlines()[1:]:
                k, sep, v = line.partition(":")
                if sep:
                    meta[k.strip()] = v.strip()
        out[f.stem] = {"status": meta.get("status") or "open", "next": meta.get("next") or "", "title": meta.get("title") or "",
                       "continues": meta.get("continues") or None, "parked": meta.get("parked") == "yes", "path": str(f)}
    return out


def park(r):
    """A stub note for a session not worth waking to write one (Ctrl-T in the browser): open, `next:` = your last prompt
    there, a digest of the transcript beside it - all made here, no API call. An existing note is only reopened, never
    overwritten. Enter then n in the browser later turns the stub into a real note. Returns the path."""
    path = HANDOFFS / f"{r['id']}.md"
    if path.exists():
        set_handoff_status(path, "open")
        return path
    digest = write_digest(r)
    last = _clean(r["last_prompt"] or r["first_prompt"] or "", 200) or "(no prompt found)"
    made, used = time.strftime("%Y-%m-%d %H:%M"), time.strftime("%Y-%m-%d %H:%M", time.localtime(r["last"]))
    c = r.get("ctx")
    path.write_text(f"""---
session: {r['id']}
title: {_clean(r['title'], 60)}
folder: {r['cwd']}
status: open
next: {last}
parked: yes
updated: {made}
---

## Goal
Parked from the session browser on {made} without waking the session - this stub was not written by Claude.
First prompt: {_clean(r['first_prompt'], 600) or '(none)'}

## Where it stands
Last used {used}{f", context {ctx_label(c).split()[0]} tokens" if c else ""}. Its last exchanges are in the digest:
{digest}

## Next steps
1. Your last prompt there: {last}
2. Enter then n in the session browser starts a fresh session that reads the digest and writes a proper note.
""")
    return path


def set_handoff_status(path, status):
    """Rewrite the `status:` line of a handoff's front matter (open / done); nothing else in the note changes."""
    p = Path(path)
    head, sep, body = p.read_text().partition("\n---")
    if re.search(r"^status:", head, flags=re.M):
        head = re.sub(r"^status:.*$", f"status: {status}", head, count=1, flags=re.M)
    else:
        head += f"\nstatus: {status}"
    p.write_text(head + sep + body)


def links():
    try:
        return json.loads(LINKS.read_text())
    except (OSError, ValueError):
        return {}


def add_link(old, new):
    d = links()
    d[old] = new
    try:
        LINKS.parent.mkdir(parents=True, exist_ok=True)
        tmp = LINKS.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(d, indent=1))
        os.replace(tmp, LINKS)
    except OSError:
        pass


def write_digest(r, budget=24_000):
    """~/.claude/handoffs/<id>.digest.md: what a fresh session needs to pick up an old one, read from the transcript with no
    API call - title, folder, first prompt, then the last exchanges (your prompts and Claude's answers, tool traffic left
    out), oldest first, within `budget` characters. Returns the path."""
    f = PROJECTS / r["proj_dir"] / f"{r['id']}.jsonl"
    size = f.stat().st_size
    cap = {"Claude": 2500, "You": 1200}                       # characters kept of one answer / one prompt
    for nbytes in (4_000_000, 16_000_000, 64_000_000, size):  # tool output fills transcripts: read back until the budget is met
        turns = _turns(f, size, nbytes)
        if nbytes >= size or sum(min(len(t), cap[who]) for who, _, t in turns) >= budget:
            break
    picked, used = [], 0
    for who, ts, t in reversed(turns):
        t = t if len(t) <= cap[who] else t[:cap[who]] + " […]"
        if used + len(t) > budget and picked:
            break
        picked.append(f"### {who} · {ts} UTC\n{t}\n")
        used += len(t)
    made, last = (time.strftime("%Y-%m-%d %H:%M", time.localtime(x)) for x in (r["created"], r["last"]))
    c = r.get("ctx")
    out = [f"# Digest of session {r['short']} - {r['title']}", "",
           f"Folder: {r['cwd']} · created {made} · last used {last}" + (f" · context {ctx_label(c).split()[0]} tokens" if c else ""),
           f"Made by the session browser from the transcript, without waking the session: {f}", "",
           "## First prompt", r["first_prompt"] or "(none)", "",
           f"## Last exchanges, oldest first ({len(picked)} of the {len(turns)} found in the last {min(nbytes, size) / 1e6:.0f} MB)", ""]
    out += reversed(picked)
    path = HANDOFFS / f"{r['id']}.digest.md"
    HANDOFFS.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out))
    return path


def _turns(f, size, nbytes):
    """[who, timestamp, text] of the prompts and answers in the last `nbytes` of a transcript, consecutive lines of one
    answer joined; tool calls, tool results, subagents and meta records left out."""
    with open(f, "rb") as fh:
        fh.seek(max(0, size - nbytes))
        lines = fh.read().split(b"\n")[1 if size > nbytes else 0:]
    turns = []
    for raw in lines:
        if b'"type":"user"' not in raw and b'"type":"assistant"' not in raw:
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        t = _text(d.get("message")).strip()
        if not t or d.get("isSidechain") or d.get("isMeta") or t.startswith(SKIP_PROMPT):
            continue
        who = "You" if d["type"] == "user" else "Claude"
        if turns and turns[-1][0] == who:                     # one answer is written as several lines: join them
            turns[-1][2] += "\n\n" + t
        else:
            turns.append([who, (d.get("timestamp") or "")[:16].replace("T", " "), t])
    return turns


# Topics for the sessions that were started in the home folder and name no experiment: "Name: keyword, keyword".
# The first line whose keyword starts a word in the title (else in the first prompt) wins. Override with
# ~/.config/tmux-dashboard/topics (same format; start from `claude-session-index --print-topics`).
TOPICS_FILE = HOME / ".config" / "tmux-dashboard" / "topics"
DEFAULT_TOPICS = """\
Lab work: cloning, plasmid, primer, oligo, genotyping, pcr, protocol
Data & HPC: 10x, sequencing, hpc, cluster, slurm, cellranger, rnaseq, dataset, pipeline
Claude tooling: claude, permission, agent, model selection, extension, doctor
Mac & tmux: tmux, dashboard, mac, ssh, safari, firewall, sleep
Notes & writing: mail, email, notes, abstract, template, manuscript
Lookups: repo, location, locate, session, folder, directory
"""
AUTO_STOP = set("""session sessions repo repos with that this from what have help find check into about working directory files
latest claude code your will would could should need want make some more other when where which there their they them""".split())


def load_topics():
    try:
        text = TOPICS_FILE.read_text()
    except OSError:
        text = DEFAULT_TOPICS
    topics = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and ":" in line:
            name, _, kws = line.partition(":")
            words = [k.strip().lower() for k in kws.split(",") if k.strip()]
            if name.strip() and words:
                topics.append((name.strip(), [re.compile(r"\b" + re.escape(w)) for w in words]))
    return topics


def topic_of(row, topics):
    for text in (row["title"], row["first_prompt"]):          # the title decides; the first prompt only breaks a tie of silence
        low = (text or "").lower()
        for name, pats in topics:
            if any(p.search(low) for p in pats):
                return name
    return None


def _code_name(letters):
    return KNOWN_CODES.get(letters.lower()) or (letters if letters.isupper() else letters.capitalize())


AUTO_DIRS = (str(HOME / ".claude" / "tests") + "/",)     # benchmark and test folders under ~/.claude


def is_automated(r):
    """Not started by hand in a terminal: a headless / SDK run (`claude -p`, entrypoint sdk-*) or a test folder."""
    return (r.get("entrypoint") or "").startswith("sdk") or any((r["cwd"] or "").startswith(d) for d in AUTO_DIRS)


def assign_blocks(rows):
    """Put a `block` on every row: `Auto` for runs that were not started by hand (scripts, benchmarks, the SDK), else the experiment code of its folder (.../Exp011530/... -> Exp), else a code named in
    its title or first prompt ("exp011530 qPCR primers" -> Exp), else ~ (started in the home folder) or Other."""
    from_dir = {}
    for r in rows:
        if is_automated(r):
            continue
        for part in reversed([x for x in (r["cwd"] or "").split("/") if x]):   # deepest folder first
            m = DIR_CODE.match(part)
            if m:
                from_dir[r["id"]] = _code_name(m.group(1))
                break
    trusted = set(KNOWN_CODES.values()) | set(from_dir.values())   # a prefix may only be believed in free text if we know it
    for r in rows:
        if is_automated(r):
            r["block"] = "Auto"
            continue
        block = from_dir.get(r["id"])
        for text in (r["title"], r["first_prompt"]) if not block else ():
            for m in TEXT_CODE.finditer(text or ""):
                if _code_name(m.group(1)) in trusted:
                    block = _code_name(m.group(1))
                    break
            if block:
                break
        r["block"] = block or ("~" if not r["cwd"] or Path(r["cwd"]) == HOME else "Other")
    # sessions started in ~ that name no experiment are split by topic: "~ Lab work", "~ Lookups" ...
    topics, left = load_topics(), []
    for r in rows:
        if r["block"] != "~":
            continue
        t = topic_of(r, topics)
        if t:
            r["block"] = f"~ {t}"
        else:
            left.append(r)
    df = {}                                                   # a word shared by 4+ of the rest becomes a topic of its own
    for r in left:
        for w in set(re.findall(r"[a-z][a-z0-9]{3,}", r["title"].lower())) - AUTO_STOP:
            df.setdefault(w, []).append(r)
    for w, rs in sorted(df.items(), key=lambda kv: -len(kv[1])):
        rs = [r for r in rs if r["block"] == "~"]
        if len(rs) >= 4:
            for r in rs:
                r["block"] = f"~ {w.capitalize()}"


def block_order(rows):
    """Blocks present in `rows`: Open (threads with an open /handoff note) first, then the configured ones (SESSION_BLOCK_ORDER, default Gen,Cyt,Seq,Exp), then any other
    experiment code by most recent use, then Other, then the `~ topic` blocks A-Z, then the rest of ~, and last Auto (runs not started by hand)."""
    wanted = [b.strip() for b in setting("SESSION_BLOCK_ORDER", "Gen,Cyt,Seq,Exp").split(",") if b.strip()]
    latest = {}
    for r in rows:
        latest[r["block"]] = max(latest.get(r["block"], 0), r["last"])
    topics = sorted(b for b in latest if b.startswith("~ "))          # alphabetical: positions stay put as sessions come and go
    rest = sorted((b for b in latest if b not in wanted and b not in ("Other", "~", "Auto") and b not in topics), key=lambda b: -latest[b])
    rest = [b for b in rest if b != "Open"]
    return (["Open"] * ("Open" in latest) + [b for b in wanted if b in latest] + rest + [b for b in ("Other",) if b in latest]
            + topics + [b for b in ("~", "Auto") if b in latest])


def folder_label(cwd):
    if not cwd:
        return "?"
    p = Path(cwd)
    return "~" if p == HOME else (p.name or "/")


def build(use_cache=True):
    """Rows for every top-level transcript, newest use first. Stubs that were continued elsewhere are flagged."""
    cache = _load_cache() if use_cache else {}
    fresh, rows = {}, []
    for f in PROJECTS.glob("*/*.jsonl"):
        try:
            st = f.stat()
        except OSError:
            continue
        key = str(f)
        c = cache.get(key)
        if c and c["size"] == st.st_size and c["mtime"] == st.st_mtime_ns:
            info = c["info"]
        else:
            try:
                info = _scan(f, st.st_size)
            except OSError:
                continue
        fresh[key] = {"size": st.st_size, "mtime": st.st_mtime_ns, "info": info}
        sid = f.stem
        created = _epoch(info.get("first_ts") or "") or st.st_ctime
        last = _epoch(info.get("last_ts") or "") or st.st_mtime
        title = info.get("custom") or info.get("agent") or info.get("ai") or _clean(info.get("first_prompt") or "", 70) or sid[:8]
        rows.append({
            "id": sid, "short": sid[:8], "title": title,
            "titled": bool(info.get("custom") or info.get("agent") or info.get("ai")),
            "created": created, "last": last, "cwd": info.get("cwd") or "", "folder": folder_label(info.get("cwd")),
            "size": st.st_size, "cost": info.get("cost"), "first_prompt": info.get("first_prompt") or "",
            "last_prompt": info.get("last_prompt") or "", "continued_in": info.get("continued_in"),
            "proj_dir": f.parent.name, "live": None, "folded": False, "entrypoint": info.get("entrypoint") or "", "usage": info.get("usage"),
            "empty": not (info.get("custom") or info.get("agent") or info.get("ai") or info.get("first_prompt")),
        })
    _save_cache(fresh)
    ids = {r["id"] for r in rows}
    live = live_map()
    hand, link = handoffs(), links()
    try:
        spent = spend_by_session()
    except Exception:                                         # a damaged spend index must not take the browser down
        spent = {}
    for sid, h in hand.items():                               # a note that says where it came from links the two as well
        if h["continues"]:
            link.setdefault(h["continues"], sid)
    back = {new: old for old, new in link.items()}
    for r in rows:
        r["live"] = live.get(r["id"])
        r["continued_by"] = link.get(r["id"]) if link.get(r["id"]) in ids else None
        r["folded"] = bool(r["continued_in"] and r["continued_in"] in ids and r["continued_in"] != r["id"]) or bool(r["continued_by"])
        r["ctx"] = context(r)
        r["spent"] = spent.get(r["id"])
        sid, h = r["id"], hand.get(r["id"])                   # its own note, else the note of the session it continues
        for _ in range(5):
            if h or sid not in back:
                break
            sid = back[sid]
            h = hand.get(sid)
        r["handoff"] = h
    assign_blocks(rows)
    for r in rows:                                            # open threads gather at the top, whatever their folder
        if r["handoff"] and r["handoff"]["status"] == "open" and not r["folded"]:
            r["block"] = "Open"
    rows.sort(key=lambda r: r["last"], reverse=True)
    return rows


def haystack(r):
    """One lowercase string per row: everything the browser can show, so a match is never invisible."""
    from time import localtime, strftime
    return " ".join([r["title"], r["block"], r["folder"], r["cwd"], r["id"], r["first_prompt"], r["last_prompt"],
                     strftime("%Y-%m-%d", localtime(r["created"]))]).lower()


def matches(r, words):
    h = haystack(r)
    return all(w in h for w in words)


def ago(seconds):
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit}"
    return "now"


def live_label(r, current_session=None):
    """'w7' for a window of the current tmux session, '1:w3' for another, 'bg' / 'term' outside tmux."""
    lv = r["live"]
    if not lv:
        return ""
    if lv["window_index"] is not None:     # short enough for a 6-column label: w18 here, 0:w18 in another tmux session
        short = lv["tmux_session"].split("-")[0][:3]
        return f"w{lv['window_index']}" if lv["tmux_session"] == current_session else f"{short}:w{lv['window_index']}"
    return "bg" if "back" in (lv["kind"] or "") or lv["kind"] == "bg" else "term"
