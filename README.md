# tmux-dashboard

A monitoring window (`dash`) for a tmux session full of Claude Code sessions, plus manual
save/restore of those sessions across reboots. Built on one Mac and one cluster; the machine-specific bits (cluster alias, weather location)
live in `~/.config/tmux-dashboard/config`, so it installs on another Mac, see below.

## Layout

```
bin/        scripts, symlinked into ~/.local/bin by install.sh
config/     tmux.conf, symlinked to ~/.tmux.conf
launchd/    dash-web.plist.in (template for the lock-screen web mirror; install.sh fills in paths)
saver/      sources of the "Tmux Dashboard" screensaver (WKWebView -> dash-web). Parked, see below
```

## The dash window (9 panes)

| Pane | Script | Shows |
|---|---|---|
| btop | `dash-btop` | system monitor; watchdog restarts it if it freezes |
| slurm | `slurm-panel` | HPC queue (`squeue` every 60 s, `sacct` 24 h every 5 min), shared cache so N dashboards = one poller |
| local jobs | `local-jobs-panel` | jobs Claude sessions started on this Mac, plus finished ones (24 h) |
| sessions | `claude-sessions-panel` | running Claude sessions, status, topic |
| usage | `claude-usage-panel` | context %, 5 h and 7 d rate limits (fed by `claude-statusline`) |
| sensors | `sensors-panel` | macmon + iSMC temperatures |
| clock | `clock-panel` | big-digit clock |
| weather | `weather-panel` | Open-Meteo, keyless, 15 min cache |
| spotify | `spotify-panel` | now playing; space/n/p/+/- when focused |

`tmux-dashboard` builds the window (`prefix+g` jumps to it; a `session-created` hook adds it to new
sessions unless `TMUX_NO_AUTODASH=1`). `tmux-dash-layout` fits it to the screen: wide (landscape) or
tall (portrait), re-run by a `window-resized` hook. Panes are found by the command they run
(`ROLES` in that script), then swapped into the order tmux fills a custom layout in; tmux ignores
the ids inside a layout string.

Every pane ignores keystrokes (`select-pane -d`) so a stray key cannot quit btop, pause Spotify or change
the local-jobs sort; `prefix+i` toggles input for the pane under the cursor. A dead widget is respawned by a
`pane-died` hook.

## Local jobs pane

Ownership is found by walking the parent chain to a live session in `~/.claude/sessions/*.json`;
for detached jobs (`nohup`, double fork) it falls back to `CLAUDE_PID` in the process environment,
read via `sysctl KERN_PROCARGS2` (`ps e` shows nothing on this macOS), but only if it names a
live session: the tmux server and apps launched from a session carry a stale value.

Labels are the tool plus what it works on (script, sample, `src -> dst`), not shell plumbing.
Keys: `s` sort (age/cpu/mem/session), `f` filter (all/active/long/busy-sessions/done), `r` reset.
Finished jobs are remembered for 24 h in `~/.claude/dash/localjobs-history.json`, recorded only
while some dashboard is open. **The exit code is not observable from outside the process**, so a
finished row says when it ended and how long it ran, not whether it worked.

## Themes

`dash-theme magi|eva01|eva02|default` flips tmux, btop, iTerm and VS Code together. For MAGI the
dashboard is orange only: inactive pane borders `#a85a1c`, the selected pane `#ff4400` with a
dark-orange background tint, default text in dash windows `#d9772b` (`DASH_TINT` in `dash-theme`;
window-level options, so Claude's own windows are untouched). Needs the upstream MAGI-theme files
(see `install.sh`).

## Save / restore

`tmux-claude-save` maps each pane to its Claude session id and writes `~/.tmux-claude-restore.sh`
(archive copies in `~/.tmux-claude-saves/`), so each window comes back with `claude --resume <id>`
in the right cwd. Saving is manual by choice. Never run the restore script while the sessions are
live: it would double-resume every conversation.

## Install on another Mac

Requires macOS (Apple Silicon for `macmon`), Homebrew, Python >= 3.11 and tmux. Claude Code is needed for the
sessions/usage panes; the SLURM pane needs an ssh alias that logs in without a password.

```
git clone git@github.com:tiagobrc/tmux_dashboard_evangelion_theme.git ~/tmux-dashboard
cd ~/tmux-dashboard
./install.sh --check                      # report what is missing, change nothing
./install.sh --brew --theme magi          # install deps, link everything, fetch + apply the MAGI theme
$EDITOR ~/.config/tmux-dashboard/config   # SLURM host/user, weather location
tmux new -s main                          # the dash window is added automatically; prefix+g jumps to it
```

`install.sh` is idempotent and never overwrites a regular file (it is moved to `<name>.bak-<date>`).

| Flag | Does |
|---|---|
| *(none)* | symlinks `bin/*` into `~/.local/bin` and `config/tmux.conf` into `~/.tmux.conf`; creates `~/.config/tmux-dashboard/config`; sets `statusLine` in `~/.claude/settings.json` if unset (backed up first) |
| `--brew` | `brew bundle` from `Brewfile` (tmux, btop, macmon, python) |
| `--theme magi\|eva01\|eva02` | clones SinsuSquid/MAGI-theme into `~/.local/share/dash-theme/` and runs `dash-theme <variant>` (backs up your tmux/btop/iTerm/VS Code configs first; `dash-theme default` reverts) |
| `--launchd` | installs and starts the lock-screen web mirror (`dash-web`, `http://127.0.0.1:8765/`) |
| `--check` | dry run |

Make sure `~/.local/bin` is on your `PATH`.

### Per-machine settings

`~/.config/tmux-dashboard/config` (template: `config/dashboard.conf.example`), `KEY=VALUE`, environment variables of
the same name win:

| Key | Meaning |
|---|---|
| `SLURM_HOST` / `SLURM_USER` | ssh alias of the cluster login node and your user there. Empty = the SLURM pane says it is not configured |
| `WEATHER_LAT` / `WEATHER_LON` / `WEATHER_PLACE` / `WEATHER_TZ` | weather location; `WEATHER_TZ=auto` takes the zone from the coordinates |

### Optional pieces

- **Temperatures** in the sensors pane: drop the [iSMC](https://github.com/dkorunic/iSMC) release binary at
  `~/.local/bin/ismc`. Without it the pane shows macmon data only.
- **Spotify** pane: needs the Spotify desktop app; otherwise it idles.
- **Screensaver** (`saver/`): parked, not installed by `install.sh`.

### Updating

`git pull` is enough: scripts are symlinks. Re-run `./install.sh` after new scripts are added, then
`tmux source-file ~/.tmux.conf`.

## Known issues

- Screensaver (`saver/`): shows the dashboard for a few seconds, then goes black. Leading
  hypothesis: it blanks on the global `com.apple.screensaver.willstop` notification and never
  reloads. Parked.
- `tmux-dashboard <session> [index] [-d]`: `-d` is the third argument; in the index slot it is read
  as a window offset.
