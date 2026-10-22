#!/bin/bash
# Install the tmux dashboard on a Mac. Idempotent; a regular file in the way is moved aside, never overwritten.
#
#   ./install.sh                 link scripts + tmux.conf + Claude skills, create the config file, wire the statusline
#   ./install.sh --brew          also `brew bundle` the dependencies (tmux btop macmon python)
#   ./install.sh --theme magi    also fetch the MAGI theme files and apply the variant (magi|eva01|eva02)
#   ./install.sh --launchd       also install + start the lock-screen web mirror (dash-web)
#   ./install.sh --check         change nothing, just report what is missing
set -eu
here="$(cd "$(dirname "$0")" && pwd)"
do_brew=0 theme="" launchd=0 check=0
while [ $# -gt 0 ]; do
    case "$1" in
        --brew) do_brew=1 ;;
        --theme) theme="${2:?--theme needs magi|eva01|eva02}"; shift ;;
        --launchd) launchd=1 ;;
        --check) check=1 ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

[ "$(uname)" = Darwin ] || { echo "macOS only (macmon, AppleScript, launchd)." >&2; exit 1; }
BREW="$(brew --prefix 2>/dev/null || true)"; [ -n "$BREW" ] || BREW=/opt/homebrew
export PATH="$BREW/bin:$HOME/.local/bin:$PATH"

# ---- what is missing ---------------------------------------------------------------------------------------------
missing=()
for t in tmux btop macmon python3; do command -v "$t" >/dev/null || missing+=("$t"); done
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null || missing+=("python>=3.11")
extras=()
command -v ismc >/dev/null   || extras+=("ismc (temperatures in the sensors pane; release binary from github.com/dkorunic/iSMC into ~/.local/bin/ismc)")
command -v claude >/dev/null || extras+=("claude (the sessions/usage panes need Claude Code)")
[ -d /Applications/Spotify.app ] || extras+=("Spotify.app (the spotify pane shows 'not running' without it)")
if [ "$check" = 1 ]; then
    echo "required missing: ${missing[*]:-none}"
    printf 'optional missing: %s\n' "${extras[@]:-none}"
    exit 0
fi
if [ "$do_brew" = 1 ]; then
    command -v brew >/dev/null || { echo "Homebrew not found: https://brew.sh" >&2; exit 1; }
    brew bundle --file="$here/Brewfile"
elif [ "${#missing[@]}" -gt 0 ]; then
    echo "missing: ${missing[*]}  (re-run with --brew, or: brew bundle --file=$here/Brewfile)"
fi

# ---- scripts + tmux config ---------------------------------------------------------------------------------------
mkdir -p ~/.local/bin ~/.tmux ~/.claude/dash ~/.config/tmux-dashboard
for f in "$here"/bin/*; do
    dest=~/.local/bin/$(basename "$f")
    [ -e "$dest" ] && [ ! -L "$dest" ] && mv "$dest" "$dest.bak-$(date +%Y%m%d)"
    ln -sfn "$f" "$dest"
done
dest=~/.tmux.conf
[ -e "$dest" ] && [ ! -L "$dest" ] && mv "$dest" "$dest.bak-$(date +%Y%m%d)"
ln -sfn "$here/config/tmux.conf" "$dest"

# ---- Claude Code skills (/handoff writes the notes behind the session browser's Open block) ----------------------
# Each file is linked into a real ~/.claude/skills/<name>/ folder: a linked folder may not be picked up as a skill.
# An identical copy in the way is replaced; a different one is moved aside.
for d in "$here"/claude/skills/*/; do
    name=$(basename "$d")
    mkdir -p ~/.claude/skills/"$name"
    for f in "$d"*; do
        dest=~/.claude/skills/$name/$(basename "$f")
        if [ -e "$dest" ] && [ ! -L "$dest" ] && ! cmp -s "$f" "$dest"; then mv "$dest" "$dest.bak-$(date +%Y%m%d)"; fi
        ln -sfn "$f" "$dest"
    done
done

# ---- per-machine settings ----------------------------------------------------------------------------------------
conf=~/.config/tmux-dashboard/config
if [ ! -e "$conf" ]; then
    cp "$here/config/dashboard.conf.example" "$conf"
    echo "created $conf  <- edit SLURM_HOST / SLURM_USER and the WEATHER_* location"
fi

# ---- Claude Code statusline (feeds the usage pane) ---------------------------------------------------------------
python3 - <<'PY'
import json, os, shutil, time
p = os.path.expanduser("~/.claude/settings.json")
cmd = os.path.expanduser("~/.local/bin/claude-statusline")
try:
    d = json.load(open(p))
except FileNotFoundError:
    d = {}
if "statusLine" in d:
    # The setting may spell the path with ~ or $HOME; expand both before comparing.
    used = os.path.expandvars(json.dumps(d["statusLine"]).replace("~/", os.path.expanduser("~/")))
    print("statusLine already set in ~/.claude/settings.json - left alone" +
          ("" if cmd in used else " (it does not call claude-statusline, so the usage pane stays empty)"))
else:
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if os.path.exists(p):
        shutil.copy2(p, p + time.strftime(".bak-%Y%m%d"))
    d["statusLine"] = {"type": "command", "command": cmd}
    json.dump(d, open(p, "w"), indent=2)
    print("set statusLine -> claude-statusline in ~/.claude/settings.json")
PY

# ---- theme -------------------------------------------------------------------------------------------------------
if [ -n "$theme" ]; then
    repo=~/.local/share/dash-theme/MAGI-theme
    [ -d "$repo" ] || git clone --depth 1 https://github.com/SinsuSquid/MAGI-theme "$repo"
    ~/.local/bin/dash-theme "$theme"
fi

# ---- lock-screen web mirror --------------------------------------------------------------------------------------
if [ "$launchd" = 1 ]; then
    if [ -e ~/Library/LaunchAgents/com.tiagobrc.dash-web.plist ]; then
        echo "old com.tiagobrc.dash-web agent present - not installing a second one on the same port"
    else
        plist=~/Library/LaunchAgents/local.tmux-dashboard.web.plist
        mkdir -p ~/Library/LaunchAgents
        sed "s#@HOME@#$HOME#g; s#@BREW@#$BREW#g" "$here/launchd/dash-web.plist.in" > "$plist"
        launchctl bootout "gui/$(id -u)/local.tmux-dashboard.web" 2>/dev/null || true
        launchctl bootstrap "gui/$(id -u)" "$plist"
        echo "dash-web listening on http://127.0.0.1:8765/"
    fi
fi

echo
echo "Done. Linked $(ls "$here/bin" | wc -l | tr -d ' ') scripts into ~/.local/bin, config/tmux.conf into ~/.tmux.conf and the" \
     "$(ls "$here/claude/skills" | tr '\n' ' ')skill(s) into ~/.claude/skills."
[ "${#extras[@]}" -gt 0 ] && printf 'Optional, not found: %s\n' "${extras[@]}"
echo "Next: make sure ~/.local/bin is on PATH, then  tmux new -s main  (the dash window appears; prefix+g jumps to it)."
