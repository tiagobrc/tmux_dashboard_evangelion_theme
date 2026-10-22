#!/usr/bin/env python3
"""Index of Claude Code sessions: one row per transcript, joined with what is running now.

Shared by `claude-session-index` (table / JSON) and `claude-session-picker` (the tmux browser).

Source of truth is the transcripts, ~/.claude/projects/*/<sessionId>.jsonl. Only the first few
hundred lines and the last 1.5 MB of each are read (some are 35 MB); the result is cached by
(size, mtime) in ~/.claude/dash/sessions-index.json, so a refresh only re-reads what changed.
"Last used" is the timestamp of the last user/assistant record, not the file mtime: Claude Code
bumps the mtime of every open session, idle or not.

"Live" is joined from ~/.claude/sessions/<pid>.json (running(): the pid is alive and started before the entry was
written, so a reused pid is not mistaken for it) and, for tmux sessions, the window the pane lives in.

"Context" is what every API request of the session re-sends: the snapshot claude-statusline saves in
~/.claude/dash/status/<sessionId>.json when it is newer, else the last API response in the transcript.

An open /handoff note whose transcript Claude Code has purged (cleanupPeriodDays) still gets a row, made from the note
alone: r["orphan"] = True, nothing to resume - only a fresh session from the note (continue_prompt()).
"""
import ctypes, fcntl, json, os, re, shutil, struct, subprocess, sys, threading, time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))      # dashlib sits beside this file
from dashlib import dur

HOME = Path.home()
PROJECTS = HOME / ".claude" / "projects"
REGISTRY = HOME / ".claude" / "sessions"
CACHE = HOME / ".claude" / "dash" / "sessions-index.json"
HEAD_LINES = 400
TAIL_BYTES = (1_500_000, 16_000_000)   # widen only when the small tail held no title
SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SKIP_PROMPT = ("<command-name>", "<local-command", "<system-reminder", "<task-notification", "<bash-")
CACHE_VERSION = 3
CONFIG = HOME / ".config" / "tmux-dashboard" / "config"
STATUS = HOME / ".claude" / "dash" / "status"          # claude-statusline's snapshot of each session
# /handoff notes, <sessionId>.md (digests made here are <sessionId>.digest.md), and {old sessionId: new sessionId} for threads
# continued from the browser. Tests point both elsewhere with the environment variables.
HANDOFFS = Path(os.environ.get("CLAUDE_HANDOFFS_DIR") or HOME / ".claude" / "handoffs")
LINKS = Path(os.environ.get("CLAUDE_DASH_LINKS") or HOME / ".claude" / "dash" / "continued.json")
# Per-transcript API responses read so far, for spend(). v2 keeps each response's local day (v1 only its month) and lives
# in its own file, so a dashboard still running the v1 code keeps its own index instead of fighting over this one.
SPEND = HOME / ".claude" / "dash" / "spend-index-v2.json"
SPEND_V1 = HOME / ".claude" / "dash" / "spend-index.json"     # read once, to carry over transcripts purged since
SPEND_VERSION = 2
SPEND_SAVE_EVERY = 600         # s: the index is written when it is older than this (whoever wrote it) ...
SPEND_SAVE_BYTES = 64_000_000  # ... or when this process is that much transcript ahead of it: a lost write is a re-count
# API list price, $/MTok (input, output, cache read), by model-id prefix - the longest prefix wins. A cache write costs
# 2x input with the 1-hour TTL, 1.25x with the 5-minute one. platform.claude.com/docs/en/about-claude/pricing, 2026-10-22.
PRICES = {"claude-fable-5-1": (10, 50, 0.25), "claude-mythos-5-1": (10, 50, 0.25), "claude-fable-5": (10, 50, 1.0),
          "claude-mythos-5": (10, 50, 1.0), "claude-opus-5-5": (4, 20, 0.20), "claude-opus-5": (5, 25, 0.50),
          "claude-opus-4": (5, 25, 0.50),          # Opus 4.5-4.8; Opus 4 and 4.1 below
          "claude-opus-4-0": (15, 75, 1.50), "claude-opus-4-1": (15, 75, 1.50), "claude-opus-4-2025": (15, 75, 1.50),
          "claude-sonnet-5-5": (2, 10, 0.10), "claude-sonnet-5": (2, 10, 0.20), "claude-sonnet-4": (3, 15, 0.30),
          "claude-haiku-5-5": (0.10, 0.50, 0.01), "claude-haiku-4": (1, 5, 0.10)}
# A prompt (input + cache writes + cache reads) over this many tokens is billed at the second price, the whole request.
LONG_PRICES = {"claude-haiku-5-5": (100_000, (0.50, 2.50, 0.05))}
# usage.speed == "fast": every token category costs this multiple (the cache multipliers stack on top).
FAST_PRICES = {"claude-opus-5": 2, "claude-opus-4-8": 2}      # Opus 5.5 $8/$40, Opus 5 and 4.8 $10/$50
FAST = "+fast"                                                # tag on the model id of a fast response in the spend index
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


def _obj(raw):
    """A transcript line as a dict, or None: half-written, not JSON, or JSON that is not an object (`null`, a list)."""
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def _scan_head(f):
    out = {"first_ts": None, "cwd": None, "first_prompt": "", "entrypoint": None}
    with open(f, "rb") as fh:
        for i, raw in enumerate(fh):
            if i >= HEAD_LINES:
                break
            d = _obj(raw)
            if d is None:
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
        d = _obj(raw)
        if d is None:
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


_disk = {}                     # path -> (mtime_ns, size, ino, parsed JSON): files re-read only when they changed


def _read_json(p, st=None):
    """Parsed JSON file `p`, re-read only when its mtime, size or inode changed (other processes replace these files).
    Raises OSError / ValueError like json.loads(p.read_text())."""
    key = str(p)
    st = st or os.stat(key)
    sig = (st.st_mtime_ns, st.st_size, st.st_ino)
    hit = _disk.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    with open(key, "rb") as fh:
        data = json.loads(fh.read())
    _disk[key] = (sig, data)
    return data


def _load_cache():
    try:
        d = _read_json(CACHE)
        return d["files"] if d.get("v") == CACHE_VERSION else {}
    except Exception:
        return {}


def _save_cache(files):
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE.with_suffix(f".{os.getpid()}.tmp")      # the browser and the dash panel both write it
        tmp.write_text(json.dumps({"v": CACHE_VERSION, "files": files}))
        os.replace(tmp, CACHE)
        st = os.stat(CACHE)
        _disk[str(CACHE)] = ((st.st_mtime_ns, st.st_size, st.st_ino), {"v": CACHE_VERSION, "files": files})
    except OSError:
        pass


_dirs = {}                     # folder -> (mtime_ns, subfolders, transcripts): listed again only when its mtime moves


def _transcripts():
    """{path: stat} of every transcript under PROJECTS, subagents included - the same paths as
    PROJECTS.glob("**/*.jsonl"), symlinked folders not followed. A folder is listed again only when its mtime changed
    (an entry was added, removed or renamed); the transcripts themselves are stat'ed on every call. Shared by build()
    and the spend index."""
    out, todo = {}, [str(PROJECTS)]
    while todo:
        d = todo.pop()
        try:
            m = os.stat(d).st_mtime_ns
        except OSError:
            continue
        hit = _dirs.get(d)
        if not hit or hit[0] != m:
            subs, files = [], []
            try:
                with os.scandir(d) as it:
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                subs.append(e.path)
                            elif e.name.endswith(".jsonl"):
                                files.append(e.path)
                        except OSError:
                            pass
            except OSError:
                continue
            hit = _dirs[d] = (m, subs, files)
        todo += hit[1]
        for f in hit[2]:
            try:
                out[f] = os.stat(f)
            except OSError:
                pass
    return out


def _run(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return ""


_libc = []


def _proc_start(pid):
    """Start time (epoch) of process `pid` from the kernel - sysctl kern.proc.pid, the start time sits at the head of
    struct kinfo_proc - with no subprocess; None when unknown (no such process, not macOS)."""
    try:
        if not _libc:
            _libc.append(ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True))
        mib, buf, size = (ctypes.c_int * 4)(1, 14, 1, pid), ctypes.create_string_buffer(1024), ctypes.c_size_t(1024)
        if _libc[0].sysctl(mib, 4, buf, ctypes.byref(size), None, 0) or size.value < 16:
            return None
        sec, usec = struct.unpack_from("qi", buf.raw)
        return sec + usec / 1e6 if sec > 0 else None
    except (OSError, AttributeError, ValueError, struct.error):
        return None


def running(d):
    """True when registry entry `d` (~/.claude/sessions/<pid>.json) belongs to a running process: its pid is alive and
    the process started no later than the entry says it did (startedAt, +5 s) - a pid the system has since handed to
    another process is not alive for us. The one live check of the dashboard: live_map() and the panels use it."""
    pid = d.get("pid") if isinstance(d, dict) else None
    if not isinstance(pid, int) or pid <= 0:              # os.kill(0 or -1, 0) signals a whole group and succeeds
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:                              # exists, but someone else's: the start time decides
        pass
    except OSError:
        return False
    began, started = _proc_start(pid), (d.get("startedAt") or 0) / 1000
    return not (began and started and began > started + 5)


def registry():
    """The ~/.claude/sessions/<pid>.json entries (dicts) of the Claude Code processes that are running now."""
    out = []
    for f in REGISTRY.glob("*.json"):
        try:
            d = _read_json(f)
        except (OSError, ValueError):
            continue
        if running(d):
            out.append(d)
    return out


def live_map():
    """sessionId -> {pid, status, kind, tmux_session, window_index, window_id, pane_id} for running claude processes."""
    panes = {}                                                # pane -> (window, session, index), on this tmux server
    for line in _run("tmux", "list-panes", "-a", "-F", "#{pane_id} #{window_id} #{window_index} #{session_name}").splitlines():
        parts = line.split(" ", 3)
        if len(parts) == 4 and parts[2].isdigit():
            panes[parts[0]] = (parts[1], parts[3], int(parts[2]))
    live = {}
    for d in registry():
        sid, pid = d.get("sessionId"), d.get("pid")
        if not sid:
            continue
        entry = {"pid": pid, "status": d.get("status"), "kind": d.get("kind"), "tmux_session": None, "window_index": None, "window_id": None, "pane_id": None}
        m = re.match(r"(.+):(@\d+)\.(%\d+)$", d.get("tmux") or "")
        p = panes.get(m.group(3)) if m else None
        if p and p[0] == m.group(2):                          # its pane, still in its window: ids repeat across tmux servers
            entry["tmux_session"], entry["window_index"] = p[1], p[2]
            entry["window_id"], entry["pane_id"] = m.group(2), m.group(3)
        # two registry files can name one session (a stale resume); prefer the one that has a tmux window
        if sid not in live or (entry["window_index"] is not None and live[sid]["window_index"] is None):
            live[sid] = entry
    return live


def _unquote(v):
    """A config value as meant: `"250"` or `'250'` -> 250 (a quoted value is kept whole, # included), `250  # note` -> 250."""
    v = v.strip()
    if v[:1] in ("'", '"'):
        end = v.find(v[0], 1)
        return v[1:end] if end > 0 else v[1:]
    return re.sub(r"\s+#.*$", "", v).strip()


def setting(name, default):
    """KEY from the environment or ~/.config/tmux-dashboard/config (KEY=VALUE, the first such line), else the default.
    Quotes around the value and a ` # comment` after it are dropped. An empty environment variable counts as unset;
    `KEY=` in the config returns "" (an explicit "none")."""
    if os.environ.get(name):
        return _unquote(os.environ[name])
    try:
        for line in CONFIG.read_text().splitlines():
            k, sep, v = line.partition("=")
            if sep and k.strip() == name:
                return _unquote(v)
    except OSError:
        pass
    return default


_warned = set()


def setting_num(name, default, kind=int):
    """A numeric setting: `kind`(value), with $ , _ dropped ("$1,500" -> 1500). Unset or empty -> `default`; a value
    that is not a number -> `default` too, with one warning on stderr per process instead of an exception (a bad line
    in the config must not crash-loop every panel that imports this)."""
    v = setting(name, "")
    if v == "":
        return default
    try:
        return kind(re.sub(r"[$,_]", "", v))
    except ValueError:
        if name not in _warned:
            _warned.add(name)
            print(f"tmux-dashboard: {name}={v!r} (environment or {CONFIG}) is not a number - using {default}", file=sys.stderr)
        return default


# Every step of a session (each prompt and each tool call is one API request) re-reads its whole context from the cache,
# and the first step after the cache expired rewrites all of it, so a long session gets dearer per step. Thresholds in
# thousands of tokens, from ~/.config/tmux-dashboard/config: CTX_WARN_K (yellow, /compact soon) and CTX_CLEAR_K (red).
CTX_WARN = setting_num("CTX_WARN_K", 200) * 1000
CTX_CLEAR = setting_num("CTX_CLEAR_K", 400) * 1000

_rates_memo = {}


def _rates(model):
    """(price, long, fast) of a model id: the PRICES entry (None when unknown), the LONG_PRICES (threshold, price) or
    None, and the fast-mode multiple (1 unless the id carries the FAST tag). By longest prefix, [1m] dropped; memoised."""
    got = _rates_memo.get(model)
    if got is None:
        fast = (model or "").endswith(FAST)
        m = re.sub(r"\[.*\]$", "", (model or "").removesuffix(FAST))     # claude-opus-5-5[1m] -> claude-opus-5-5
        pick = lambda table: next((table[k] for k in sorted(table, key=len, reverse=True) if m.startswith(k)), None)
        got = _rates_memo[model] = (pick(PRICES), pick(LONG_PRICES), (pick(FAST_PRICES) or 1) if fast else 1)
    return got


def _price(model, prompt=0):
    """(input, output, cache read) $/MTok for `model` when its prompt is `prompt` tokens long, or None if unknown."""
    p, long, _ = _rates(model)
    return long[1] if long and prompt > long[0] else p


def context(r, now=None):
    """{tokens, model, warm, left, step, wake, wake_exact, missed, missed_usd, misses, miss_causes, level} for a row, or
    None before its first API response. `step` is what each request pays to re-read the context while the cache is warm;
    `wake` what the first one pays once it went cold - exact (wake_exact) when the statusline snapshot says how many
    tokens a cold request re-sends (prompt_cache.recache_tokens_if_cold), else the context size. `missed` is the tokens
    this session's process re-sent on cache misses so far (prompt_cache.miss_recache_tokens), `missed_usd` their price
    as cache writes, `miss_causes` {cause: count} (ttl_expired_1h, model_changed ...); None / {} without a snapshot."""
    now = now or time.time()
    u, ttl, expires, pc = r.get("usage"), "1h", None, {}
    last_req = _epoch(u["ts"] or "") if u else None            # the session's last API response, from the transcript
    try:
        s = _read_json(os.path.join(STATUS, r["id"] + ".json"))
        cur = (s.get("context_window") or {}).get("current_usage") or {}
        tokens = sum(cur.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        if tokens and s.get("_saved_at", 0) >= (last_req or 0) - 60:    # the statusline saw it last: it knows the cache too (all zeros = resumed, no request yet)
            u = {"tokens": tokens, "model": (s.get("model") or {}).get("id")}
            pc = s.get("prompt_cache") if isinstance(s.get("prompt_cache"), dict) else {}
            ttl, expires = pc.get("ttl") or ttl, pc.get("expires_at")    # None until this process has sent a request (just resumed)
            expires = expires if isinstance(expires, (int, float)) else None
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    if not u or not u["tokens"]:
        return None
    if not expires and last_req:                               # never from when the snapshot was saved: a resume sends nothing
        expires = last_req + (3600 if ttl == "1h" else 300)
    tokens, write = u["tokens"], (2 if ttl == "1h" else 1.25)
    price = _price(u["model"], tokens) or PRICES["claude-opus-5-5"]
    cold = pc.get("recache_tokens_if_cold")
    exact = type(cold) is int and cold > 0                     # type(), not isinstance: True is an int too
    missed = pc.get("miss_recache_tokens")
    missed = missed if type(missed) is int and missed >= 0 else None
    return {"tokens": tokens, "model": u["model"], "warm": bool(expires and expires > now), "left": (expires or now) - now,
            "step": tokens * price[2] / 1e6, "wake": (cold if exact else tokens) * price[0] * write / 1e6, "wake_exact": exact,
            "missed": missed, "missed_usd": missed * price[0] * write / 1e6 if missed else (0.0 if missed == 0 else None),
            "misses": pc.get("misses") if isinstance(pc.get("misses"), int) else None,
            "miss_causes": pc.get("miss_causes") if isinstance(pc.get("miss_causes"), dict) else {},
            "level": 2 if tokens >= CTX_CLEAR else 1 if tokens >= CTX_WARN else 0}


def money(usd):
    """Dollars in at most 5 cells, right-alignable: $0 (nothing), $.00 (under half a cent), $.41, $8.0, $25, $415,
    $1.7k, $12k, $415k, $1.2M, $12M. Each step is chosen on the rounded value, so 9.96 is $10, not $10.0."""
    if not usd or usd <= 0:
        return "$0"
    if round(usd, 2) < 1:
        return "$" + f"{usd:.2f}"[1:]
    if round(usd, 1) < 10:
        return f"${usd:.1f}"
    if round(usd) < 1000:
        return f"${usd:.0f}"
    for unit, size in (("k", 1e3), ("M", 1e6)):
        if round(usd / size, 1) < 10:
            return f"${usd / size:.1f}{unit}"
        if round(usd / size) < 1000 or unit == "M":
            return f"${usd / size:.0f}{unit}"


def ctx_cells(r):
    """(size, price, warm time left, total so far) strings for a row's context columns; blanks when unknown.
    Widths: size <= 5 cells, price <= 6 (↻ + money), time left <= 4 (dashlib.dur: 59m, 45s, 1h00), total <= 5."""
    c = r.get("ctx")
    size, price = ctx_label(c).split() if c else ("", "")
    return size, price, dur(c["left"]) if c and c["warm"] else "", money(r["spent"]) if r.get("spent") else ""


def ctx_label(c):
    """'312K $.06' while the cache is warm (cost of each step), '312K ↻$2.5' once it is cold (cost of waking it up)."""
    if not c:
        return ""
    t = c["tokens"]
    size = f"{t / 1e3:.0f}K" if t < 999_500 else f"{t / 1e6:.1f}M"
    return f"{size:>5} {money(c['step']) if c['warm'] else '↻' + money(c['wake'])}"


def usd(model, t):
    """API list price of one response; t = [input, output, cache write 5m, cache write 1h, cache read] tokens. The prompt
    length (t[0] + t[2] + t[3] + t[4]) picks the LONG_PRICES tier; a FAST-tagged model id costs FAST_PRICES times more."""
    p, long, x = _rates(model)
    if long and t[0] + t[2] + t[3] + t[4] > long[0]:
        p = long[1]
    p = p or PRICES["claude-opus-5-5"]
    return x * (t[0] * p[0] + t[1] * p[1] + t[2] * p[0] * 1.25 + t[3] * p[0] * 2 + t[4] * p[2]) / 1e6


def _response(raw):
    """(id, model, local day, tokens) of one API response line of a transcript, or None. The model id carries the FAST
    tag when usage.speed is "fast"; the day is YYYY-MM-DD, or "?" without a timestamp."""
    d = _obj(raw)
    if d is None:
        return None
    m = d.get("message") if d.get("type") == "assistant" else None
    if not isinstance(m, dict) or not isinstance(m.get("usage"), dict) or not m.get("id") or m.get("model") in (None, "<synthetic>"):
        return None
    u = m["usage"]
    cc = u.get("cache_creation") or {}
    w5, w1 = (cc.get("ephemeral_5m_input_tokens") or 0, cc.get("ephemeral_1h_input_tokens") or 0) if cc else (u.get("cache_creation_input_tokens") or 0, 0)
    ts = _epoch(d.get("timestamp") or "")
    return (m["id"], m["model"] + (FAST if u.get("speed") == "fast" else ""), time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "?",
            [u.get("input_tokens") or 0, u.get("output_tokens") or 0, w5, w1, u.get("cache_read_input_tokens") or 0])


# ---- spend: every API response at list price, from an incremental index of the transcripts ----
# The index lives in memory per process, {path: {ino, off, ids: {response id: [model, day, 5 token counts]}}}: each call
# reads each transcript on from `off`, and the file on disk is only re-read when another process replaced it (merged:
# per transcript the entry that got further wins) and only written every SPEND_SAVE_EVERY s across all processes, or
# when this one is SPEND_SAVE_BYTES ahead of it. Re-reading a transcript from an older offset changes nothing (a response
# keeps its largest token counts), so a lost write only costs a re-count. Per-session totals and the deduplicated
# response table are kept too, recomputed only for what changed.
_sp = {"files": None, "sig": None, "ahead": 0, "dirty": False, "gen": 0, "sid": {}, "keys": {}, "sess": {}, "stale": set(),
       "best": None, "best_gen": -1}
_sp_lock = threading.RLock()          # the usage panel counts in a thread while it draws


def _sig(p):
    try:
        st = os.stat(p)
        return (st.st_mtime_ns, st.st_ino, st.st_size)
    except OSError:
        return None


def _sid_of(key):
    """Session id of a transcript path: <proj>/<id>.jsonl, or <proj>/<id>/... for its subagents; None outside PROJECTS."""
    sid = _sp["sid"].get(key, 0)
    if sid == 0:
        root = str(PROJECTS) + os.sep
        parts = key[len(root):].split(os.sep) if key.startswith(root) else []
        sid = _sp["sid"][key] = (parts[1][:-6] if len(parts) == 2 else parts[1]) if len(parts) >= 2 else None
    return sid


def _touch(key):
    """Entry `key` gained or lost responses: its session's total and the response table are stale."""
    sid = _sid_of(key)
    if sid:
        _sp["keys"].setdefault(sid, set()).add(key)
        _sp["stale"].add(sid)
    _sp["gen"] += 1


def _spend_read():
    """The index on disk, {path: entry}; None when missing or unreadable."""
    try:
        d = json.loads(SPEND.read_text())
        return d["files"] if d.get("v") == SPEND_VERSION and isinstance(d.get("files"), dict) else None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _spend_migrate():
    """A start for v2 from the v1 index: only the transcripts that are gone since (their month stands in for the day);
    the others are re-read from the start, which is what gives their responses a day."""
    try:
        d = json.loads(SPEND_V1.read_text())
        old = d["files"] if d.get("v") == 1 else {}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        old = {}
    return {k: c for k, c in old.items() if isinstance(c, dict) and not os.path.exists(k)}


def _spend_merge(disk):
    """Fold the index another process wrote into ours: per transcript the entry that read further wins (same inode;
    else ours, which the next scan checks anyway). Sets how far ahead of the disk we are."""
    files, ahead, mine = _sp["files"], 0, False
    for key, d in disk.items():
        c = files.get(key)
        if c is None or (d.get("ino") == c["ino"] and d.get("off", 0) > c["off"]):
            files[key] = d
            _touch(key)
        else:
            ahead += c["off"] - d.get("off", 0) if d.get("ino") == c["ino"] else c["off"]
    for key in files.keys() - disk.keys():
        ahead, mine = ahead + files[key]["off"], True
    _sp["ahead"], _sp["dirty"] = ahead, bool(ahead or mine)


def _spend_files(found=None):
    """Bring the in-memory spend index up to date with the transcripts (`found` = _transcripts(), when the caller has it)
    and return its files: {path: {ino, off, ids}}. Call with _sp_lock held."""
    sig = _sig(SPEND)
    if _sp["files"] is None:
        disk = _spend_read()
        _sp["files"] = disk if disk is not None else _spend_migrate()
        _sp["sig"], _sp["dirty"] = sig, disk is None
        for key in _sp["files"]:
            _touch(key)
    elif sig and sig != _sp["sig"]:                          # another process wrote it
        disk = _spend_read()
        if disk is not None:
            _spend_merge(disk)
        _sp["sig"] = sig
    files = _sp["files"]
    for key, st in (found if found is not None else _transcripts()).items():
        c = files.get(key)
        if c and c["ino"] == st.st_ino and c["off"] > st.st_size:  # maybe only grown since `found` was listed
            try:
                st = os.stat(key)
            except OSError:
                continue
        if not c or c["ino"] != st.st_ino or c["off"] > st.st_size:          # new, replaced or truncated: from the start
            c = files[key] = {"ino": st.st_ino, "off": 0, "ids": {}}
            _touch(key)
            _sp["dirty"] = True
        if c["off"] == st.st_size:
            continue
        try:
            with open(key, "rb") as fh:
                fh.seek(c["off"])
                chunk = fh.read(st.st_size - c["off"])
        except OSError:
            continue
        end = chunk.rfind(b"\n") + 1                                          # a line still being written waits for next time
        got_any = False
        for raw in chunk[:end].split(b"\n"):
            if b'"usage"' in raw and b'"type":"assistant"' in raw:
                got = _response(raw)
                if got:
                    rid, model, day, t = got
                    old = c["ids"].get(rid)
                    c["ids"][rid] = [model, day, *(map(max, old[2:], t) if old else t)]
                    got_any = True
        if end:
            c["off"] += end
            _sp["ahead"] += end
            _sp["dirty"] = True
            if got_any:
                _touch(key)
    due = _sp["dirty"] and (sig is None or _sp["ahead"] >= SPEND_SAVE_BYTES or time.time() - sig[0] / 1e9 >= SPEND_SAVE_EVERY)
    if due and _sig(SPEND) == sig:                            # not if another process wrote it during this scan
        try:
            SPEND.parent.mkdir(parents=True, exist_ok=True)
            tmp = SPEND.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps({"v": SPEND_VERSION, "files": files}))
            os.replace(tmp, SPEND)
            _sp["sig"], _sp["ahead"], _sp["dirty"] = _sig(SPEND), 0, False
        except OSError:
            pass
    return files


def _best():
    """{response id: [model, day, tokens, usd, sessions]}: every API response once, at its largest token counts, with the
    sessions whose transcripts hold it. Rebuilt only after the index changed. Call with _sp_lock held."""
    if _sp["best_gen"] != _sp["gen"]:
        best = {}
        for key, c in _sp["files"].items():
            sid = _sid_of(key)
            for rid, (model, day, *t) in c["ids"].items():
                old = best.get(rid)
                if old:
                    old[0], old[1], old[2] = model, day, list(map(max, old[2], t))
                    if sid:
                        old[4].add(sid)
                else:
                    best[rid] = [model, day, t, 0.0, {sid} if sid else set()]
        for b in best.values():
            b[3] = usd(b[0], b[2])
        _sp["best"], _sp["best_gen"] = best, _sp["gen"]
    return _sp["best"]


def spend():
    """{"2026-10": usd, ...}: every API response in ~/.claude/projects (subagents included) at API list price, by local month.
    Incremental: each transcript is read on from where the last call stopped, and files Claude Code has since deleted keep
    counting. A response written over several lines (streaming) or copied into another file (a fork, a resume) counts
    once, at its largest token counts. A response whose final line was never written keeps its placeholder output count,
    so this runs a few percent low."""
    with _sp_lock:
        _spend_files()
        by = {}
        for model, day, t, cost, sids in _best().values():
            by[day[:7]] = by.get(day[:7], 0) + cost
    return by


def spend_by_session(found=None):
    """{sessionId: usd} at API list price, each session with its subagents (project/<id>.jsonl and everything under
    project/<id>/). Same counting as spend(), except that a response copied into a fork counts in both sessions.
    Only the sessions whose transcripts changed since the last call are summed again."""
    with _sp_lock:
        files = _spend_files(found)
        for sid in _sp["stale"]:
            d = {}
            for key in _sp["keys"].get(sid, ()):
                for rid, (model, day, *t) in files[key]["ids"].items():
                    old = d.get(rid)
                    d[rid] = (model, list(map(max, old[1], t)) if old else t)
            _sp["sess"][sid] = sum(usd(m, t) for m, t in d.values())
        _sp["stale"].clear()
        return dict(_sp["sess"])


def _week(day):
    """ISO week '2026-W43' of a local day 'YYYY-MM-DD', or None."""
    try:
        y, w, _ = date.fromisoformat(day).isocalendar()
        return f"{y}-W{w:02d}"
    except (ValueError, TypeError):
        return None


def week_days(week):
    """(first, last) local day, 'YYYY-MM-DD', of ISO week '2026-W43'."""
    y, w = week.split("-W")
    monday = date.fromisocalendar(int(y), int(w), 1)
    return monday.isoformat(), (monday + timedelta(days=6)).isoformat()


def spend_breakdown(first=None, last=None, rows=None):
    """Spend of the API responses made on local days `first`..`last` ('YYYY-MM-DD', both included; None = open), each
    response once: {"total", "by_day", "by_week", "by_month", "by_model", "by_block", "by_session"} - every value usd,
    every breakdown adding up to "total". A response two sessions hold (a fork) is split evenly between them. Blocks are
    the rows' home_block (from build(), called when `rows` is None); a session with no row - its transcript is gone -
    counts as "(gone)". Models are API ids, " fast" appended for fast mode. Responses whose day is unknown ("?", or only
    the month for transcripts purged before v2) count only when both bounds are None."""
    if rows is None:
        rows = build()
    block = {r["id"]: r.get("home_block") or r["block"] for r in rows}
    out = {"total": 0.0, "by_day": {}, "by_week": {}, "by_month": {}, "by_model": {}, "by_block": {}, "by_session": {}}

    def add(k, key, v):
        out[k][key] = out[k].get(key, 0.0) + v
    with _sp_lock:
        _spend_files()
        best = _best()
        for model, day, t, cost, sids in best.values():
            if (first or last) and (len(day) != 10 or (first and day < first) or (last and day > last)):
                continue
            out["total"] += cost
            add("by_month", day[:7], cost)
            if len(day) == 10:
                add("by_day", day, cost)
                add("by_week", _week(day), cost)
            add("by_model", model.removesuffix(FAST) + (" fast" if model.endswith(FAST) else ""), cost)
            share = cost / (len(sids) or 1)
            for sid in sids or (None,):
                add("by_session", sid or "?", share)
                add("by_block", block.get(sid, "(gone)"), share)
    return out


def spend_days(rows=None, now=None):
    """The spend of the last days, for the usage panel: {"today": usd, "last7": usd (today and the 6 days before),
    "days": [(day, usd)] x 7 oldest first, "top": {"id", "title", "usd"} of today's costliest session or None}."""
    if rows is None:
        rows = build()
    today = date.fromtimestamp(now or time.time())
    days = [(today - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    b = spend_breakdown(days[0], days[-1], rows)
    t = spend_breakdown(days[-1], days[-1], rows)
    title = {r["id"]: r["title"] for r in rows}
    sid, top = max(t["by_session"].items(), key=lambda kv: kv[1], default=(None, 0))
    return {"today": b["by_day"].get(days[-1], 0.0), "last7": b["total"], "days": [(d, b["by_day"].get(d, 0.0)) for d in days],
            "top": {"id": sid, "title": title.get(sid) or (sid or "?")[:8], "usd": top} if sid and top > 0 else None}


def budget(now=None):
    """Month to date against MONTH_BUDGET_USD (empty = no budget), for the usage panel: {"month": "2026-10", "mtd",
    "pace" (mtd / days gone x days in the month, at least half a day gone), "budget" (None without one), "fraction"
    (mtd / budget) and "pace_fraction" (pace / budget), both None without a budget}."""
    now = now or time.time()
    lt = time.localtime(now)
    start = time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1))
    month = time.strftime("%Y-%m", lt)
    mtd = spend().get(month, 0.0)
    ndays = ((date(lt.tm_year + lt.tm_mon // 12, lt.tm_mon % 12 + 1, 1) - date(lt.tm_year, lt.tm_mon, 1)).days)
    pace = mtd / max((now - start) / 86400, 0.5) * ndays
    cap = setting_num("MONTH_BUDGET_USD", None, float)
    cap = cap if cap and cap > 0 else None
    return {"month": month, "mtd": mtd, "pace": pace, "budget": cap, "fraction": mtd / cap if cap else None,
            "pace_fraction": pace / cap if cap else None}


# ---- threads: /handoff notes, and sessions continued in a fresh one ----
def handoffs():
    """sessionId -> {status, next, title, folder, continues, parked, updated, path} from the front matter of each
    ~/.claude/handoffs/<sessionId>.md. `updated` is an epoch: the note's `updated: YYYY-MM-DD HH:MM` (local time), else
    the file's mtime; `folder` is "" when the note names none."""
    out = {}
    for f in HANDOFFS.glob("*.md"):
        if f.name.endswith(".digest.md"):
            continue
        try:
            text = f.read_text()
            mtime = f.stat().st_mtime
        except (OSError, UnicodeDecodeError):
            continue
        if not text.strip():                                  # an empty file is no thread
            continue
        meta = _front_matter(text)
        updated = None
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                updated = time.mktime(time.strptime(meta.get("updated") or "", fmt))
                break
            except ValueError:
                pass
        out[f.stem] = {"status": meta.get("status") or "open", "next": meta.get("next") or "", "title": meta.get("title") or "",
                       "folder": meta.get("folder") or "", "continues": meta.get("continues") or None,
                       "parked": meta.get("parked") == "yes", "updated": updated or mtime, "path": str(f)}
    return out


def continue_prompt(r):
    """The first prompt of a fresh session that carries on row `r`'s thread (Enter then n in the browser): from its
    handoff note when Claude wrote it; else from a digest of the transcript, made here for free (a parked stub note is
    named too); an orphan row (transcript purged) from its digest if one was made before, else from the note itself."""
    h = r.get("handoff")
    if h and not h["parked"] and not r.get("orphan") and Path(h["path"]).stem != r["id"]:
        return (f"Continue the thread in the handoff note {h['path']} (written by an earlier session of it) and "
                f"{write_digest(r)}, a digest of the session that carried it on since, made from its transcript without "
                f"waking it. Read both, write this thread's handoff note with the handoff skill so the session browser "
                f"tracks it, then tell me in 3 lines where we left off and what you will do first.")
    if h and (not h["parked"] or (r.get("orphan") and not (HANDOFFS / f"{r['id']}.digest.md").exists())):
        return (f"Continue the thread in the handoff note {h['path']} (from an earlier session in this folder). Read it and "
                f"the files it names as needed, then tell me in 3 lines where we left off and what you will do first.")
    parked = f" (parked with the stub note {h['path']})" if h else ""
    digest = HANDOFFS / f"{r['id']}.digest.md" if r.get("orphan") else write_digest(r)
    return (f"{digest} is a digest of an earlier session in this folder{parked}, made from its transcript "
            f"without waking it. Read it, write this thread's handoff note with the handoff skill so the session "
            f"browser tracks it, then tell me in 3 lines where we left off and what is next.")


def _front_matter(text):
    meta = {}
    if text.startswith("---"):
        for line in text.split("\n---", 1)[0].splitlines()[1:]:
            k, sep, v = line.partition(":")
            if sep:
                meta[k.strip()] = v.strip()
    return meta


STUB_MARK = "this stub was not written by Claude"      # polish_park() only replaces a note that still says this


def park(r):
    """A stub note for a session not worth waking to write one (Ctrl-T in the browser): open, `next:` = your last prompt
    there, a digest of the transcript beside it - all made here, no API call. An existing note is only reopened, never
    overwritten. polish_park() then rewrites the stub from the digest in the background (PARK_MODEL), and Enter then n
    in the browser later hands the digest to a fresh session. Returns the path."""
    path = HANDOFFS / f"{r['id']}.md"
    if path.exists():
        set_handoff_status(path, "open")
        return path
    digest = write_digest(r)
    last = _clean(r["last_prompt"] or r["first_prompt"] or "", 200) or "(no prompt found)"
    made, used = time.strftime("%Y-%m-%d %H:%M"), time.strftime("%Y-%m-%d %H:%M", time.localtime(r["last"]))
    c = r.get("ctx")
    tmp = path.with_suffix(f".{os.getpid()}.tmp")             # whole or not at all; errors="replace": a stray surrogate in a prompt
    tmp.write_text(f"""---
session: {r['id']}
title: {_clean(r['title'], 60)}
folder: {r['cwd']}
status: open
next: {last}
parked: yes
updated: {made}
---

## Goal
Parked from the session browser on {made} without waking the session - {STUB_MARK}.
First prompt: {_clean(r['first_prompt'], 600) or '(none)'}

## Where it stands
Last used {used}{f", context {ctx_label(c).split()[0]} tokens" if c else ""}. Every prompt of yours and the last exchanges are in the digest:
{digest}

## Next steps
1. Your last prompt there: {last}
2. Enter then n in the session browser starts a fresh session that reads the digest and writes a proper note.
""", errors="replace")
    os.replace(tmp, path)
    return path


PARK_NOTE = """The text piped in is a digest of an earlier Claude Code session, taken from the transcript: its folder, \
first prompt, every prompt its owner typed in the whole session (oldest first, with timestamps; a very long session is \
sampled evenly) and the last exchanges verbatim. Write the handoff note that lets its owner pick the thread up next week \
without reading the digest: cover the whole arc of the session from the prompts - what was asked, in which order, what \
changed course - not only how it ended. Reply with exactly this layout and nothing else - no preamble, no code fences:

title: <what this thread is, at most 60 characters, not a date>
next: <the single next action, one line, concrete enough to start on>
---
## Goal
<why the thread exists and what done looks like, 2-4 lines>

## Where it stands
<results, files written, jobs, the numbers that matter>

## Decisions
<what was decided and why; "(none in the digest)" if none>

## Next steps
1. <the next: line>
2. ...

## Key files
<absolute paths named in the digest, one per line with a few words each; "(none in the digest)" if none>

Use only what the digest says - keep absolute paths, exact commands, ids and numbers; never invent. Under 50 lines."""


def polish_park_later(sid):
    """Run polish_park(sid) in the background, detached from the browser. Returns the model, or None if PARK_MODEL=off."""
    model = setting("PARK_MODEL", "haiku")
    if model.lower() in ("off", "no", "none"):
        return None
    subprocess.Popen([sys.executable, os.path.realpath(__file__), "polish", sid, model], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return model


def polish_park(sid, model="haiku"):
    """Rewrite the parked stub of session `sid` as a real note: `model` reads only the digest beside it (claude -p, no
    tools, no transcript kept; ~15 s and well under 1¢ on Haiku). The stub stays when the call fails or the reply is
    not a note, and a note that /handoff or a continuation wrote in the meantime is left alone. Why it failed goes to
    ~/.claude/dash/park.log."""
    path, digest = HANDOFFS / f"{sid}.md", HANDOFFS / f"{sid}.digest.md"
    claude = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or str(HOME / ".local" / "bin" / "claude")
    try:
        with open(digest) as fh:
            out = subprocess.run([*claude.split(), "-p", "--model", model, "--no-session-persistence", "--tools", "",
                                  "--system-prompt", "You turn session digests into concise handoff notes.", PARK_NOTE],
                                 stdin=fh, capture_output=True, text=True, timeout=180, cwd=HANDOFFS)
        reply = re.sub(r"^```\w*\n|\n```$", "", out.stdout.strip())
        reply = reply[m.start():] if (m := re.search(r"^title:", reply, flags=re.M)) else ""   # any preamble dropped
        head, _, body = reply.partition("\n---\n")
        meta = _front_matter("---\n" + head)
        filled = all(meta.get(k) and not meta[k].startswith("<") for k in ("title", "next"))   # not the template echoed
        if out.returncode or not filled or "## Next steps" not in body:
            return _park_log(sid, f"{model} gave no note (exit {out.returncode}): {(out.stderr or reply)[:200]!r}")
        stub = path.read_text()
        if STUB_MARK not in stub:
            return _park_log(sid, "the stub was replaced meanwhile - left as is")
        old = _front_matter(stub)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(f"""---
session: {sid}
title: {_clean(meta['title'], 60)}
folder: {old.get('folder', '')}
status: {old.get('status') or 'open'}
next: {_clean(meta['next'], 200)}
parked: yes
updated: {time.strftime("%Y-%m-%d %H:%M")}
---

_Written by {model} from the digest {digest}, without waking the session. The digest has every prompt of the session and the last exchanges verbatim._

{body.strip()}
""")
        os.replace(tmp, path)
    except (OSError, subprocess.SubprocessError) as e:
        _park_log(sid, f"{type(e).__name__}: {e}")


def _park_log(sid, msg):
    try:
        with open(HOME / ".claude" / "dash" / "park.log", "a") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M')} {sid[:8]} {msg}\n")
    except OSError:
        pass


def set_handoff_status(path, status):
    """Rewrite the `status:` line of a handoff's front matter (open / done); nothing else in the note changes."""
    p = Path(path)
    text = p.read_text()
    if not text.startswith("---"):                            # no front matter: give it one, or the status is never read
        text = f"---\nstatus: {status}\n---\n" + text
    head, sep, body = text.partition("\n---")
    if re.search(r"^status:", head, flags=re.M):
        head = re.sub(r"^status:.*$", f"status: {status}", head, count=1, flags=re.M)
    else:
        head += f"\nstatus: {status}"
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(head + sep + body)
    os.replace(tmp, p)


def links():
    try:
        return json.loads(LINKS.read_text())
    except (OSError, ValueError):
        return {}


def add_link(old, new):
    try:
        LINKS.parent.mkdir(parents=True, exist_ok=True)
        with open(LINKS.with_suffix(".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)              # read-modify-write: a second picker waits instead of losing a link
            d = links()
            d[old] = new
            tmp = LINKS.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(d, indent=1))
            os.replace(tmp, LINKS)
    except OSError:
        pass


def write_digest(r, budget=24_000, prompt_budget=8_000):
    """~/.claude/handoffs/<id>.digest.md: what a fresh session needs to pick up an old one, read from the transcript with no
    API call - title, folder, first prompt; every prompt you typed in the whole session, oldest first, ~300 characters
    each, within `prompt_budget` characters (a long session keeps its first and last prompts and an even sample between
    them, and says how many it left out); then the last exchanges (your prompts and Claude's answers, tool traffic left
    out), oldest first, within `budget` characters. Returns the path."""
    f = PROJECTS / r["proj_dir"] / f"{r['id']}.jsonl"
    size = f.stat().st_size
    prompts = [f"- {ts or '?'} · {c[:300]}{'…' if len(c) > 300 else ''}" for ts, t in _prompts(f) for c in [_clean(t, 301)]]
    shown, left = _fit(prompts, prompt_budget)
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
           f"## Your prompts, whole session ({len(prompts)})", "",
           "Oldest first, times UTC, each cut to 300 characters"
           + (f"; {left} of the {len(prompts)} left out where marked, to keep this section under {prompt_budget:,} "
              f"characters (the first and last ones are all here, the middle is an even sample)." if left else "."),
           "", *(shown or ["(none found)"]), "",
           f"## Last exchanges, oldest first ({len(picked)} of the {len(turns)} found in the last {min(nbytes, size) / 1e6:.0f} MB)", ""]
    out += reversed(picked)
    path = HANDOFFS / f"{r['id']}.digest.md"
    HANDOFFS.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out), errors="replace")         # a stray surrogate in a prompt must not stop Ctrl-T or n
    return path


PROMPT_SKIP = SKIP_PROMPT + ("[Request interrupted", "<command-message>")


def _prompts(f):
    """[timestamp UTC 'YYYY-MM-DD HH:MM', text] of every prompt typed in transcript `f`, oldest first. The whole file is
    read in 8 MB blocks, but only lines that say "type":"user" and carry no tool result, meta, subagent or compaction
    record are parsed - a 100 MB transcript takes well under a second."""
    out, rest = [], b""
    with open(f, "rb") as fh:
        while True:
            block = fh.read(8 << 20)
            lines = (rest + block).split(b"\n")
            rest = lines.pop() if block else b""
            for raw in lines:
                if (b'"type":"user"' not in raw or b'"tool_use_id":' in raw or b'"isMeta":true' in raw
                        or b'"isSidechain":true' in raw or b'"isCompactSummary":true' in raw):
                    continue
                d = _obj(raw)
                if d is None:
                    continue
                t = _text(d.get("message")).strip() if d.get("type") == "user" else ""
                if t and not t.startswith(PROMPT_SKIP) and not d.get("isMeta") and not d.get("isSidechain"):
                    out.append([(d.get("timestamp") or "")[:16].replace("T", " "), t])
            if not block:
                return out


def _fit(lines, budget, head=5, tail=10):
    """(lines, how many left out): all of `lines` if they fit in `budget` characters; else the first `head`, the last
    `tail` and as many evenly spaced ones between them as fit, each gap marked '- (... N left out ...)'."""
    n = len(lines)

    def render(keep):
        out, prev = [], -1
        for i in sorted(keep):
            if i > prev + 1:
                out.append(f"- (… {i - prev - 1} left out …)")
            out.append(lines[i])
            prev = i
        return out
    size = lambda out: sum(len(x) + 1 for x in out)
    if size(lines) <= budget:
        return lines, 0
    h, t = min(head, n), min(tail, n - min(head, n))
    while h + t > 1 and size(render(set(range(h)) | set(range(n - t, n)))) > budget:
        h, t = (h, t - 1) if t > h else (h - 1, t)
    base, mid = set(range(h)) | set(range(n - t, n)), range(h, n - t)
    pick = lambda k: base | {mid[int((j + 0.5) * len(mid) / k)] for j in range(k)}
    lo, hi = 0, len(mid)                                      # the most evenly spaced middle prompts that still fit
    while lo < hi:
        k = (lo + hi + 1) // 2
        lo, hi = (k, hi) if size(render(pick(k))) <= budget else (lo, k - 1)
    keep = pick(lo)
    return render(keep), n - len(keep)


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
        d = _obj(raw)
        if d is None:
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
            if name.strip() and words:                        # one pattern per topic: any keyword starting a word
                topics.append((name.strip(), [re.compile(r"\b(?:" + "|".join(map(re.escape, words)) + ")")]))
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


def _orphan_row(sid, h):
    """A row for an open handoff note whose transcript is gone (Claude Code purges transcripts after cleanupPeriodDays):
    title, folder, `next:` and time from the note; nothing to resume, no prompts, size 0, never live - only a fresh
    session from the note (continue_prompt). Every field the browser and the panels read is there."""
    return {"id": sid, "short": sid[:8], "title": h["title"] or sid[:8], "titled": bool(h["title"]),
            "created": h["updated"], "last": h["updated"], "cwd": h["folder"], "folder": folder_label(h["folder"]),
            "size": 0, "cost": None, "first_prompt": "", "last_prompt": "", "continued_in": None, "proj_dir": "",
            "live": None, "folded": False, "entrypoint": "", "usage": None, "empty": False, "orphan": True}


def build(use_cache=True):
    """Rows for every top-level transcript, newest use first, plus one for every open handoff note whose transcript is
    gone (r["orphan"], see _orphan_row). Stubs that were continued elsewhere are flagged. r["block"] is "Open" for an
    open thread; r["home_block"] is the block it would have otherwise (its experiment code, topic ...)."""
    cache = _load_cache() if use_cache else {}
    fresh, rows, rescanned = {}, [], not use_cache
    found = _transcripts()
    depth = str(PROJECTS).count(os.sep) + 2
    for key, st in found.items():
        if key.count(os.sep) != depth:                        # subagents: only the spend index reads them
            continue
        proj = os.path.dirname(key)
        c = cache.get(key)
        if c and c["size"] == st.st_size and c["mtime"] == st.st_mtime_ns:
            info = c["info"]
        else:
            try:
                info = _scan(key, st.st_size)
            except OSError:
                continue
            rescanned = True
        fresh[key] = {"size": st.st_size, "mtime": st.st_mtime_ns, "info": info}
        sid = os.path.basename(key)[:-6]
        created = _epoch(info.get("first_ts") or "") or st.st_ctime
        last = _epoch(info.get("last_ts") or "") or st.st_mtime
        title = info.get("custom") or info.get("agent") or info.get("ai") or _clean(info.get("first_prompt") or "", 70) or sid[:8]
        rows.append({
            "id": sid, "short": sid[:8], "title": title,
            "titled": bool(info.get("custom") or info.get("agent") or info.get("ai")),
            "created": created, "last": last, "cwd": info.get("cwd") or "", "folder": folder_label(info.get("cwd")),
            "size": st.st_size, "cost": info.get("cost"), "first_prompt": info.get("first_prompt") or "",
            "last_prompt": info.get("last_prompt") or "", "continued_in": info.get("continued_in"),
            "proj_dir": os.path.basename(proj), "live": None, "folded": False, "entrypoint": info.get("entrypoint") or "", "usage": info.get("usage"),
            "empty": not (info.get("custom") or info.get("agent") or info.get("ai") or info.get("first_prompt")), "orphan": False,
        })
    if rescanned or fresh.keys() != cache.keys():             # written only when an entry was rescanned or dropped
        _save_cache(fresh)
    live = live_map()
    hand, link = handoffs(), links()
    have = {r["id"] for r in rows}
    rows += [_orphan_row(sid, h) for sid, h in hand.items() if h["status"] == "open" and sid not in have and SESSION_ID.fullmatch(sid)]
    ids = {r["id"] for r in rows}
    try:
        spent = spend_by_session(found)
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
        r["ctx"] = None if r["orphan"] else context(r)
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
        r["home_block"] = r["block"]
        if r["handoff"] and r["handoff"]["status"] == "open" and not r["folded"]:
            r["block"] = "Open"
    rows.sort(key=lambda r: r["last"], reverse=True)
    return rows


def haystack(r):
    """One lowercase string per row: everything the browser can show, so a match is never invisible - its handoff
    note's title and next step included."""
    h = r.get("handoff") or {}
    return " ".join([r["title"], r["block"], r["folder"], r["cwd"], r["id"], r["first_prompt"], r["last_prompt"],
                     h.get("title") or "", h.get("next") or "", time.strftime("%Y-%m-%d", time.localtime(r["created"]))]).lower()


def matches(r, words):
    h = haystack(r)
    return all(w in h for w in words)


def ago(seconds):
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit}"
    return "now"


def _usd(v):
    """$1,234 / $8.40 / $0.03 / - (nothing): Markdown money, exact enough to compare weeks."""
    return f"${v:,.0f}" if v >= 10 else f"${v:.2f}" if v >= 0.005 else "-"


def _change(a, b):
    return f"{(a - b) / b:+.0%}" if b >= 0.005 else ("new" if a >= 0.005 else "")


def weekly_markdown(week=None, rows=None, now=None):
    """(week, text): the weekly summary as Markdown, for claude-session-index --weekly - the open threads with their
    next step, the week's spend at API list price against the week before (total, and the same days of it while the
    week is still running), by block and by model, and its 5 costliest sessions. `week` is an ISO week '2026-W43';
    default the current one, so far."""
    now = now or time.time()
    rows = build() if rows is None else rows
    today = date.fromtimestamp(now)
    week = week or _week(today.isoformat())
    first, last = week_days(week)
    prev = _week((date.fromisoformat(first) - timedelta(days=7)).isoformat())
    pfirst, plast = week_days(prev)
    cur, old = spend_breakdown(first, last, rows), spend_breakdown(pfirst, plast, rows)
    ndays = min(7, (today - date.fromisoformat(first)).days + 1)
    by_id = {r["id"]: r for r in rows}
    fmt = lambda d: date.fromisoformat(d).strftime("%a %d %b %Y")
    out = [f"# Claude week {week} · {fmt(first)} - {fmt(last)}", "",
           f"Made {time.strftime('%Y-%m-%d %H:%M', time.localtime(now))} by `claude-session-index --weekly`. Spend is at API "
           f"list prices (what an API or Enterprise account is billed), each response once, subagents included.", ""]
    line = f"**{_usd(cur['total'])}** this week"
    if 0 < ndays < 7:
        same = sum(v for d, v in old["by_day"].items() if d <= (date.fromisoformat(pfirst) + timedelta(days=ndays - 1)).isoformat())
        line += f" (its first {ndays} day{'s' * (ndays > 1)}) · same days of {prev} {_usd(same)} ({_change(cur['total'], same) or '-'})"
    out += [line + f" · all of {prev} {_usd(old['total'])}", ""]
    b = budget(now)
    if b["budget"]:
        out += [f"Month to date {_usd(b['mtd'])} of the {_usd(b['budget'])} budget ({b['fraction']:.0%}) · pace {_usd(b['pace'])}", ""]
    threads = sorted((r for r in rows if r["block"] == "Open"), key=lambda r: -(r["handoff"]["updated"] or 0))
    out += [f"## Open threads ({len(threads)})", ""]
    for r in threads:
        h = r["handoff"]
        age = ago(now - h["updated"]) if h["updated"] else "?"
        out.append(f"- **{h['title'] or r['title']}** - next: {h['next'] or '?'}  \n"
                   f"  `{r['short']}` · {r['folder']} · note updated {time.strftime('%Y-%m-%d', time.localtime(h['updated']))} ({age} ago)"
                   + (" · transcript purged" if r["orphan"] else "") + (" · parked" if h["parked"] else ""))
    out += ([] if threads else ["(none)"]) + ["", "## Costliest sessions this week", "",
            "| # | spent | session | block | title |", "|---:|---:|---|---|---|"]
    for i, (sid, v) in enumerate(sorted(cur["by_session"].items(), key=lambda kv: -kv[1])[:5], 1):
        r = by_id.get(sid) or {}
        out.append(f"| {i} | {_usd(v)} | `{sid[:8]}` | {r.get('home_block', '(gone)')} | {(r.get('title') or '(transcript gone)').replace('|', '/')} |")
    for name, key in (("block", "by_block"), ("model", "by_model")):
        out += ["", f"## Spend by {name}", "", f"| {name} | {week} | {prev} | change |", "|---|---:|---:|---:|"]
        for k in sorted(cur[key].keys() | old[key].keys(), key=lambda k: (-cur[key].get(k, 0), -old[key].get(k, 0))):
            a, z = cur[key].get(k, 0.0), old[key].get(k, 0.0)
            if a >= 0.005 or z >= 0.005:
                out.append(f"| {k} | {_usd(a)} | {_usd(z)} | {_change(a, z)} |")
    return week, "\n".join(out) + "\n"


def live_label(r, current_session=None):
    """'w7' for a window of the current tmux session, '1:w3' for another, 'bg' / 'term' outside tmux."""
    lv = r["live"]
    if not lv:
        return ""
    if lv["window_index"] is not None:     # short enough for a 6-column label: w18 here, 0:w18 in another tmux session
        short = lv["tmux_session"].split("-")[0][:3]
        return f"w{lv['window_index']}" if lv["tmux_session"] == current_session else f"{short}:w{lv['window_index']}"
    return "bg" if "back" in (lv["kind"] or "") or lv["kind"] == "bg" else "term"


if __name__ == "__main__" and sys.argv[1:2] == ["polish"]:      # polish_park_later() runs this, detached
    polish_park(*sys.argv[2:4])
