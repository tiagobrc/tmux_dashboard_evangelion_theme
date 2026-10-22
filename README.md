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
| sessions | `claude-sessions-panel` | running Claude sessions, status, topic, context (`231K 5¢ 42m` = tokens, cost per step, minutes still warm; `↻$8.6` = cost to wake a cold one; the price is orange while warm, blue once cold) |
| usage | `claude-usage-panel` | context %, 5 h and 7 d rate limits (fed by `claude-statusline`); this month at API list prices, pace and last month (`claude_sessions.spend()`, incremental, cache in `~/.claude/dash/spend-index.json`) |
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
(dated copies in `~/.tmux-claude-saves/`), so each window comes back with `claude --resume <id>` in the right cwd.
`tmux-claude-save NAME` writes a named snapshot instead (`~/.tmux-claude-saves/named/NAME.sh`, restored into a tmux
session called NAME; saving the same name again moves the old file to `named/history/`), and `--list` shows every
snapshot with its age and which of its sessions are gone. The restore script checks each session when it runs: a
session whose transcript was deleted since (Claude Code purges inactive ones, `cleanupPeriodDays`) or one that is
already running in another window is not resumed - the pane gets a one-line note with its stored title and the
script lists them at the end. Saving is manual by choice. Never run a restore script blindly against live sessions
older than this guard (snapshots saved before 2026-10-21 have no guard and would double-resume them).

## Session browser (window 1), pinned tabs, window numbers

| key | does |
|---|---|
| `prefix + g` | dashboard (window 0); recreated in its slot if you closed it |
| `prefix + S` | session browser (window 1, `sessions`); recreated in its slot if you closed it |
| `prefix + T` | pin / unpin the current tab |
| `prefix + R` | close the gaps in the window numbers |

**Session browser** - `claude-session-picker` lists every Claude session (title, folder, id, created, last used,
size, running or not) from the transcripts in `~/.claude/projects`; `claude-session-index` prints the same table in
a shell (`claude-session-index pcr --idle`, `--json`). The **context** column is what every step of a session (each prompt and
each tool call) re-sends: `312K 6¢` = 312 thousand tokens re-read for 6 cents per step while the prompt cache is warm,
`312K ↻$2.5` = the cache went cold and the first step after resuming rewrites all of it for $2.50 (API list prices, by model;
the price is orange while the cache is warm, sky blue once it is cold).
It turns yellow from `CTX_WARN_K=200` (/compact soon) and red from `CTX_CLEAR_K=400` thousand tokens (/clear or /compact now;
for an idle session, a fresh one with a handoff note is cheaper than resuming), both settable in `~/.config/tmux-dashboard/config`. Sessions are grouped into **blocks** made automatically from the experiment code of their folder (`Exp011530` -> Exp,
`Gen022830` -> Gen, `Cyt...`, `Seq...`, `TC...` and so on), or of a code named in the title ("exp011530 qPCR primers"); sessions
started in the home folder go to `~`, the rest to Other. Gen, Cyt, Seq, Exp come first (`SESSION_BLOCK_ORDER=Gen,Cyt,Seq,Exp`
in `~/.config/tmux-dashboard/config` changes that), other codes follow by recent use, then Other, then `~`; a block appears
as soon as it has a session. Sessions started in `~` that name no experiment are split by topic (`~ Lab work`, `~ Lookups`, `~ Mac & tmux` ...) from
keywords in the title, then the first prompt; the rules are `Name: keyword, keyword` lines, first match wins, and live in
`~/.config/tmux-dashboard/topics` (start from `claude-session-index --print-topics`); a word shared by 4+ leftover titles
becomes a topic by itself. **Auto** (always last, folded until you open it once) holds the sessions you did not start by hand: headless / SDK runs
(`claude -p`, recorded as `entrypoint: sdk-*` in the transcript) and anything under `~/.claude/tests`, e.g. benchmark runs.
Left / Right jump between blocks, Enter on a header folds it (remembered in
`~/.claude/dash/picker.state`), Ctrl-G toggles a flat list, and `claude-session-index --block Exp` prints one block.
Type to filter (a search opens every block and selects the first match); Enter resumes the highlighted session in a
new window in its own folder, or jumps to its window if it is already running, so a conversation is never resumed
twice. Tab cycles all / idle / running, Ctrl-Y copies the `claude --resume <id>` line, Ctrl-K ends a running
session completely (asks first: `y`, or capital `Y` when it is busy; any other key or 8 s cancels; tmux sessions also
lose their window, background ones go through `claude stop`; the transcript is never touched), Ctrl-O shows the stubs
(sessions that were continued in another one, and empty ones), Esc / Ctrl-U clears the filter. It ignores the
mouse and Ctrl-C/Z/D and has no quit key; the `pane-died` hook respawns it. "Last used" is the last user/assistant
record, not the file mtime (Claude Code touches every open session's file). Shared code: `claude_sessions.py`.

**Threads** - so a session can be closed without forgetting the task. `/handoff` (the skill in
`claude/skills/handoff/SKILL.md`, linked into `~/.claude/skills/handoff/` by `install.sh`) writes
`~/.claude/handoffs/<sessionId>.md`: front matter (`title`, `status: open|done`,
`next:` the single next action, `continues:`) and Goal / Where it stands / Decisions / Dead ends / Next steps / Key
files. While a note is open its session sits in the **Open** block at the top of the browser, whatever its folder, with
the `next:` line in the preview; `/handoff done` or Ctrl-X in the browser closes it (Ctrl-X again reopens). For a
cold session not worth waking just to write a note, Ctrl-T **parks** it: the browser writes a stub note itself, no API
call (`parked: yes`, `status: open`, `next:` = your last prompt there, plus a transcript digest), so it joins Open too;
Enter then `n` later hands the digest to a fresh session, which writes the real note. Ctrl-T never overwrites a note -
on a done one it only reopens it. Enter on an
idle session that has a note, or is past `CTX_WARN_K` and cold, asks first: `n` starts a new session in the same folder
(`claude --session-id <new> --model <the old session's model> -n "<title> (cont.)" "<prompt>"`, so a Sonnet
thread stays on Sonnet) that picks up from the note - or, with no note, from a
digest the browser writes from the transcript with no API call (`~/.claude/handoffs/<id>.digest.md`: first prompt and
the last ~25K characters of prompts and answers, tool traffic left out) and turns into a note; `r` resumes as is and
shows what waking it costs. The old session is recorded in `~/.claude/dash/continued.json` and folds under the new one
(Ctrl-O shows it); a note's `continues:` line links them too. `CLAUDE_HANDOFFS_DIR` / `CLAUDE_DASH_LINKS` move both
files for tests.

**Pinned tabs** - a window with the option `@pinned 1` is drawn on the left of the status bar, after the session
badge, and left out of the scrolling window list, so it never scrolls away; clicking it works. `dash` and `sessions`
are pinned by their launchers; `tmux-pin-tab [toggle|on|off|list|defaults]` does the rest (state `0` = explicitly
unpinned). `dash-theme` writes the tab-strip formats into `~/.tmux/theme.conf` for every variant (`pin_block()`), so
re-run `dash-theme <variant>` after changing them. `tmux-claude-save` records tabs you pinned.

**Window numbers** - `tmux-claude-renumber` closes the gaps that build up in a session's window numbers and keeps
`dash` at 0 and `sessions` at 1. Windows are only moved, never restarted, so every Claude session keeps running and
the current window stays selected (the `~/.claude/sessions` registry stores `@window` ids, not indexes). `-n`
previews, `-s NAME` picks a session, `--by title|path` also sorts. Reopening `dash` / `sessions` inserts it into its
slot and shifts the windows behind it up by one. `claude --bg` sessions are not tmux windows, so they are not numbered.

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
| *(none)* | symlinks `bin/*` into `~/.local/bin`, `config/tmux.conf` into `~/.tmux.conf` and each file of `claude/skills/<name>/` into `~/.claude/skills/<name>/` (the `/handoff` command); creates `~/.config/tmux-dashboard/config`; sets `statusLine` in `~/.claude/settings.json` if unset (backed up first) |
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
