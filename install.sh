#!/bin/bash
# Link this repo into place on a Mac. Idempotent; a regular file is moved aside (never overwritten) first.
set -e
here="$(cd "$(dirname "$0")" && pwd)"
mkdir -p ~/.local/bin
for f in "$here"/bin/*; do
    dest=~/.local/bin/$(basename "$f")
    [ -e "$dest" ] && [ ! -L "$dest" ] && mv "$dest" "$dest.bak-$(date +%Y%m%d)"
    ln -sfn "$f" "$dest"
done
dest=~/.tmux.conf
[ -e "$dest" ] && [ ! -L "$dest" ] && mv "$dest" "$dest.bak-$(date +%Y%m%d)"
ln -sfn "$here/config/tmux.conf" "$dest"
mkdir -p ~/Library/LaunchAgents ~/.tmux ~/.claude/dash
cp -n "$here/launchd/com.tiagobrc.dash-web.plist" ~/Library/LaunchAgents/ || true
cat <<MSG
Linked $(ls "$here/bin" | wc -l | tr -d ' ') scripts into ~/.local/bin and config/tmux.conf into ~/.tmux.conf.
Still manual:
  - Claude statusline: set "statusLine" in ~/.claude/settings.json to ~/.local/bin/claude-statusline
  - dash-theme needs the MAGI theme files:
      git clone https://github.com/SinsuSquid/MAGI-theme ~/.local/share/dash-theme/MAGI-theme
  - dash-web:  launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/com.tiagobrc.dash-web.plist
  - tools:     brew install tmux btop macmon; iSMC release binary in ~/.local/bin/ismc (sensors-panel)
  - reload:    tmux source-file ~/.tmux.conf
MSG
