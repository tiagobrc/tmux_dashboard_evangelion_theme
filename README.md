# tmux-dashboard

A monitoring window (`dash`) for a tmux session full of Claude Code sessions, plus manual
save/restore of those sessions across reboots. Built for one Mac (macOS) and one
cluster (`ssh cluster`); host names, the weather location and paths are specific to it.

## Layout

```
bin/        scripts, symlinked into ~/.local/bin by install.sh
config/     tmux.conf, symlinked to ~/.tmux.conf
launchd/    com.tiagobrc.dash-web.plist (lock-screen web mirror)
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

## Install

```
./install.sh        # symlinks bin/* and config/tmux.conf; prints the manual steps
```

## Known issues

- Screensaver (`saver/`): shows the dashboard for a few seconds, then goes black. Leading
  hypothesis: it blanks on the global `com.apple.screensaver.willstop` notification and never
  reloads. Parked.
- `tmux-dashboard <session> [index] [-d]`: `-d` is the third argument; in the index slot it is read
  as a window offset.
