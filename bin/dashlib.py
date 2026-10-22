"""Helpers shared by the dashboard panels: is anyone looking at this pane, one fetch shared by every dashboard, a draw
that skips unchanged frames and survives errors, one duration format, and the active theme's colours.

A panel loop built from them:

    screen = dashlib.Screen()
    while True:
        screen.show(dashlib.safe(render))
        dashlib.wait(3)              # 3 s while the window is on screen; while it is not, re-check every HIDDEN_EVERY
"""
import fcntl, json, os, select, shutil, signal, subprocess, sys, time, traceback, zlib
from pathlib import Path

DASH = Path.home() / ".claude" / "dash"
SHARED = DASH / "shared"                     # results of shared() fetches, one JSON per name
# {window_id: clients}, one tmux call every VISIBLE_TTL s for every panel - one file per tmux server, whose window ids
# repeat across servers (a test server started with -L would otherwise mix its windows into the real ones)
VISIBLE = SHARED / f"visible-{zlib.crc32(os.environ.get('TMUX', '').split(',')[0].encode()):08x}.json"
WEB_BEAT = DASH / "web-heartbeat"           # dash-web touches it on each frame it serves: the web mirror counts as looking
WEB_FRESH = 15
VISIBLE_TTL = 2
HIDDEN_EVERY = 2                             # seconds between visibility checks while nobody looks: back within ~4 s
ERRLOG = DASH / "panels.err"
THEME = Path.home() / ".local" / "share" / "dash-theme" / "colours.json"   # written by dash-theme

_window = {}                                 # this pane's window id, looked up once


def _my_window():
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return None
    if pane not in _window:
        try:
            _window[pane] = subprocess.run(["tmux", "display-message", "-p", "-t", pane, "#{window_id}"],
                                           capture_output=True, text=True, timeout=2).stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None
    return _window[pane]


def _read_json(f):
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return None


def _write_json(f, data):
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, f)
    except OSError:
        pass


def visible():
    """True when a tmux client shows this pane's window, or the web mirror served a frame in the last WEB_FRESH s.
    Outside tmux, or when tmux does not answer, always True - a panel never freezes on an error here. The client
    counts of every window are read with one tmux call and shared through VISIBLE for VISIBLE_TTL s."""
    win = _my_window()
    if not win:
        return True
    try:
        if 0 <= time.time() - WEB_BEAT.stat().st_mtime < WEB_FRESH:      # a clock stepped back is not "fresh"
            return True
    except OSError:
        pass
    d = _read_json(VISIBLE)
    if not isinstance(d, dict) or not 0 <= time.time() - d.get("t", 0) <= VISIBLE_TTL:
        try:
            out = subprocess.run(["tmux", "list-windows", "-a", "-F", "#{window_id} #{window_active_clients}"],
                                 capture_output=True, text=True, timeout=2).stdout
        except (OSError, subprocess.SubprocessError):
            return True
        d = {"t": time.time(), "w": dict(l.split() for l in out.splitlines() if len(l.split()) == 2)}
        _write_json(VISIBLE, d)
    clients = d.get("w", {}).get(win)
    return clients is None or clients != "0"


def wait(every, wake_fd=None):
    """Sleep until the next frame is due: `every` seconds, then on in HIDDEN_EVERY steps while nobody looks; a byte on
    `wake_fd` (from winch_fd()) cuts any of these short. Returns True when it had to wait for the window to come back
    (the data is stale: refresh before drawing)."""
    _nap(every, wake_fd)
    hidden = False
    while not visible():
        hidden = True
        _nap(HIDDEN_EVERY, wake_fd)
    return hidden


def _nap(secs, fd):
    if fd is None:
        time.sleep(max(secs, 0))
    elif select.select([fd], [], [], max(secs, 0))[0]:
        try:
            os.read(fd, 4096)
        except OSError:
            pass


def winch_fd():
    """A pipe that a resize (SIGWINCH) writes to, for wait(..., wake_fd): the panel then redraws in its own loop, never
    inside a signal handler, where a frame could land between another one's render and write. Main thread only."""
    r, w = os.pipe()
    os.set_blocking(r, False)
    os.set_blocking(w, False)
    signal.set_wakeup_fd(w)
    signal.signal(signal.SIGWINCH, lambda *_: None)
    return r


def shared(name, max_age, fetch):
    """fetch() at most once per `max_age` seconds across every dashboard: the result (JSON-serialisable) is kept in
    SHARED/<name>.json, and a lock makes the other dashboards wait for it instead of fetching too. If fetch() raises,
    the exception reaches the caller and nothing is stored."""
    f = SHARED / f"{name}.json"

    def fresh():
        d = _read_json(f)
        if isinstance(d, dict) and 0 <= time.time() - d.get("t", 0) < max_age:
            return d
        return None

    if d := fresh():
        return d["data"]
    SHARED.mkdir(parents=True, exist_ok=True)
    with open(SHARED / f"{name}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if d := fresh():
            return d["data"]
        data = fetch()
        _write_json(f, {"t": time.time(), "data": data})
        return data


class Screen:
    """Writes a frame only when it differs from the last one or the pane was resized, so an idle panel costs tmux
    (and an SSH client watching it) nothing."""

    def __init__(self):
        self.last = None

    def show(self, frame):
        key = (frame, shutil.get_terminal_size())
        if key != self.last:
            sys.stdout.write(frame)
            sys.stdout.flush()
            self.last = key

    def invalidate(self):
        self.last = None


_logged = set()


def safe(render, *args):
    """render(*args), or a one-line error frame instead of the exception ending the panel (its respawn hook would
    restart it every 2 s). The traceback goes to ERRLOG once per distinct error, at most 20 per process."""
    try:
        return render(*args)
    except Exception as e:
        name = Path(sys.argv[0]).name
        msg = f"{type(e).__name__}: {e}"
        if msg not in _logged and len(_logged) < 20:           # each distinct error once, at most 20 per process
            _logged.add(msg)
            try:
                with open(ERRLOG, "a") as fh:
                    fh.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} {name}\n{traceback.format_exc()}")
            except OSError:
                pass
        cols = shutil.get_terminal_size().columns
        return f"\033[H\033[2J\033[91m{f'{name}: {msg}'[:cols]}\033[0m\n{f'retrying - traceback in {ERRLOG}'[:cols]}"


def dur(s):
    """A duration in at most 5 cells, rounded down, the same in every panel: 45s 59m 4h05 4d17h 12d."""
    s = max(int(s), 0)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{s % 3600 // 60:02d}"
    if s < 864000:
        return f"{s // 86400}d{s % 86400 // 3600}h"
    return f"{s // 86400}d"


def theme(key, default=""):
    """The active dash-theme colour `key` (accent, second, grey) as a truecolor escape; `default` without a theme."""
    try:
        h = _read_json(THEME)[key].lstrip("#")
        return "\033[38;2;%d;%d;%dm" % (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except (TypeError, KeyError, ValueError, IndexError):
        return default


def theme_hex(key, default=None):
    """The active dash-theme colour `key` as '#rrggbb', or `default`."""
    d = _read_json(THEME)
    return d.get(key, default) if isinstance(d, dict) else default
