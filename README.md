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
| btop | `dash-btop` | system monitor (`update_ms = 4000` in `~/.config/btop/btop.conf`); watchdog restarts it if it freezes |
| slurm | `slurm-panel` | HPC queue (`squeue` every 60 s, `sacct` 24 h every 5 min), shared cache so N dashboards = one poller; when ssh or one section fails it keeps the last good rows, shows the error and retries after 30 s; after a timeout it runs `ssh -O stop` so the next try opens a fresh connection (your open `ssh` shells stay up) |
| local jobs | `local-jobs-panel` | jobs Claude sessions started on this Mac, plus finished ones (24 h); one `ps` scan every 10 s shared by all dashboards |
| sessions | `claude-sessions-panel` | running Claude sessions: window (`w9`, or `1:w2` in another tmux session; a red `×2` = one conversation running in two windows), busy dot, title, context tokens, cost per step, then the minutes left while the cache is warm (bold cyan) or `↻$8.6`, what waking a cold one costs, then spent and last used. Busy first, then warm (least time left), then cold by wake cost; what does not fit ends in `… N more · K over 400K hidden`. **Nudge**: when an idle session past `CTX_WARN_K` has under `NUDGE_MIN` (10) minutes of warm cache left, a tmux message says so once (continue it or `/handoff`), even while the dash is hidden |
| usage | `claude-usage-panel` | 5 h and 7 d rate limits (fed by `claude-statusline`); this month at API list prices, pace and last month, and with `MONTH_BUDGET_USD` its share (pace yellow from 80%, red from 100%; one tmux message each at 80% and 100% of month-to-date); today, last 7 days and today's costliest session; what the warm sessions cost per step right now; 7-day spend bars when the pane is tall. Spend: `claude_sessions.spend()`, incremental, index `~/.claude/dash/spend-index-v3.json` saved at most every 10 min (a day-old copy beside it, `.bak`); a transcript left unwritten for an hour is folded into token sums by day and model plus an 8-byte hash per response (to recognise copies), so a resumed session adds on top and a purged one keeps counting; it grows by ~16 bytes per response, not a whole record |
| sensors | `sensors-panel` | macmon every 4 s + iSMC temperatures every 15 s; the power line is hidden while it reads all zero |
| clock | `clock-panel` | big-digit clock, drawn once a minute (and at once on resize or a theme change) |
| weather | `weather-panel` | Open-Meteo, keyless, 15 min cache |
| spotify | `spotify-panel` | now playing; space/n/p/+/- when focused. Polled every 15 s while playing (sooner near the end of a track), 30 s when paused or closed, one poll shared by all dashboards; a key or `spotify-ctl` re-polls at once |

`tmux-dashboard` builds the window (`prefix+g` jumps to it; a `session-created` hook adds it to new
sessions unless `TMUX_NO_AUTODASH=1`). `tmux-dash-layout` fits it to the screen, re-run by a `window-resized` hook
(serialised by a lock, `~/.claude/dash/layout.lock`). It takes the first of these that fits, trying tall first on a
portrait screen: **wide** (only when btop gets its 80 columns), **tall** (btop capped at 30 rows, the rest to the
sessions pane), **compact** (from 96 columns: btop parked on one row, where it idles at 0% CPU) and **narrow** (one
column); windows under 40x14 are left alone. The sessions pane gets at least 12 rows wherever the window has 23. Panes
are found by the command they run (`ROLES` in that script; btop too, not by its index), then swapped into the order
tmux fills a custom layout in; tmux ignores the ids inside a layout string. Window names are never parsed, so a name
with spaces is fine.

**Nobody looking, no work.** Every panel and the session browser pause while no tmux client shows their window
(`dashlib.visible()`: one `tmux list-windows` every 2 s, shared by all panels through `~/.claude/dash/shared/`), and
catch up within ~4 s when you switch back; the web mirror counts as looking (it touches `~/.claude/dash/web-heartbeat`
on every frame). They also skip redrawing unchanged frames, and an error draws one red line (traceback in
`~/.claude/dash/panels.err`) instead of crash-looping through the respawn hook. Shared helpers: `bin/dashlib.py`.

Every pane ignores keystrokes (`select-pane -d`) so a stray key cannot quit btop, pause Spotify or change
the local-jobs sort; `prefix+i` toggles input for the pane under the cursor. A dead widget is respawned by a
`pane-died` hook.

## Local jobs pane

Ownership is found by walking the parent chain to a live session in `~/.claude/sessions/*.json`;
for detached jobs (`nohup`, double fork) it falls back to `CLAUDE_PID` in the process environment,
read via `sysctl KERN_PROCARGS2` (`ps e` shows nothing on this macOS), but only if it names a
live session: the tmux server and apps launched from a session carry a stale value.

Labels are the tool plus what it works on (script, sample, `src -> dst`), not shell plumbing (Claude's Bash wrapper,
`… && pwd -P >| …`, is unwrapped). Columns: `win cpu mem age job` (mem is dropped below 34 columns; numbers are never
cut); CPU turns yellow from 50% and red from 400%.
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
already running in another window is not resumed (this Mac keeps transcripts for a year: `"cleanupPeriodDays": 365`
in `~/.claude/settings.json`; the default is 30 days) - the pane gets a one-line note with its stored title and the
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
each tool call) re-sends: `312K $.06 42m` = 312 thousand tokens re-read for 6 cents per step, the prompt cache warm for 42 more minutes
(bold, in the theme's second colour); `312K $.06 ↻$2.5` = the cache went cold and the first step after resuming rewrites all
of it for $2.50 (API list prices, by model; "exact" in the preview when the statusline reports the recache size). Money is
always dollars, right-aligned (`$.06 $8.0 $25 $1.7k`).
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

Filter words combine with tokens: `is:open is:warm is:cold is:running is:idle is:orphan`, `>400k` / `<100k` (context),
`block:Exp` (prefix), `$>5` / `$<1` (spent); the search also reads the handoff note's title and `next:`. Ctrl-S cycles the
sort within blocks (recent / spent / context / wake cost, remembered), Ctrl-V opens the row's note - or its digest - in a
tmux popup, and `?` (with an empty filter), Ctrl-/ or F1 lists every key. The hint line shows what fits, most useful first;
below 110 columns the id and date columns give way to the title (the rule line above the preview still names both).
Colours: bold cyan (theme second) = cache warm, plain = cold, yellow / bright red = context past `CTX_WARN_K` /
`CTX_CLEAR_K`, a bold white dot = busy right now, green = idle. Before acting, Enter / n / r re-check whether the session
started running elsewhere or was already continued, so neither happens twice. `CLAUDE_PICKER_STATE` moves
`picker.state` for tests.

**Threads** - so a session can be closed without forgetting the task. `/handoff` (the skill in
`claude/skills/handoff/SKILL.md`, linked into `~/.claude/skills/handoff/` by `install.sh`) writes
`~/.claude/handoffs/<sessionId>.md`: front matter (`title`, `status: open|done`,
`next:` the single next action, `continues:`) and Goal / Where it stands / Decisions / Dead ends / Next steps / Key
files. While a note is open its session sits in the **Open** block at the top of the browser, whatever its folder, with
the `next:` line in the preview and the note's age (`note 3d`, yellow after 7 days; the block lists the oldest note
first); `/handoff done` or Ctrl-X in the browser closes it (Ctrl-X again reopens). An open note whose transcript is gone
(Claude Code purges transcripts after `cleanupPeriodDays`) still gets a row, marked "transcript gone", where Enter offers
only `n`. For a
cold session not worth waking just to write a note, Ctrl-T **parks** it: the browser writes a stub note itself
(`parked: yes`, `status: open`, `next:` = your last prompt there, plus a transcript digest), so it joins Open at once;
then, in the background, `PARK_MODEL` (default `haiku`: `claude -p` with no tools, reading only the digest, ~15 s and
under 1¢) rewrites the stub as a real note with a title and `next:` - `PARK_MODEL=off` keeps the stub, no AI call, and
failures go to `~/.claude/dash/park.log`. Enter then `n` later hands the digest to a fresh session, which writes the
full note. Ctrl-T never overwrites a note -
on a done one it only reopens it. Enter on an
idle session that has a note, or is past `CTX_WARN_K` and cold, asks first: `n` starts a new session in the same folder
(`claude --session-id <new> --model <the old session's model> -n "<title> (cont.)" "<prompt>"`, so a Sonnet
thread stays on Sonnet) that picks up from the note - or, with no note, from a
digest the browser writes from the transcript with no API call (`~/.claude/handoffs/<id>.digest.md`: first prompt, every
prompt you typed in the session, trimmed to ~8K characters, and the last ~25K characters of prompts and answers, tool
traffic left out) and turns into a note; `r` resumes as is and
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
| `--launchd` | installs and starts the lock-screen web mirror (`dash-web`, `http://127.0.0.1:8765/`, `DASH_WEB_PORT` to change; it follows the dash-theme palette and the dash window's text colour) |
| `--check` | dry run |

Make sure `~/.local/bin` is on your `PATH`.

### Per-machine settings

`~/.config/tmux-dashboard/config` (template: `config/dashboard.conf.example`), `KEY=VALUE` (quotes and ` # comments` are fine; a bad number falls back to the default with a warning),
environment variables of the same name win:

| Key | Meaning |
|---|---|
| `SLURM_HOST` / `SLURM_USER` | ssh alias of the cluster login node and your user there. Empty = the SLURM pane says it is not configured |
| `WEATHER_LAT` / `WEATHER_LON` / `WEATHER_PLACE` / `WEATHER_TZ` | weather location; `WEATHER_TZ=auto` takes the zone from the coordinates |
| `CTX_WARN_K` / `CTX_CLEAR_K` | context size (thousand tokens) that turns yellow (200) / red (400) in the browser and the sessions pane |
| `NUDGE_MIN` | minutes of warm cache left at which an idle session past `CTX_WARN_K` gets a tmux nudge (10; 0 = off) |
| `MONTH_BUDGET_USD` | monthly budget at API prices for the usage pane (empty = none) |
| `PARK_MODEL` | model that writes a parked session's note from its digest (`haiku`; `off` = keep the stub) |
| `WEEKLY_EXPORT_DIR` | where `claude-session-index --weekly` writes `Claude week <YYYY-Www>.md` (empty = print it) |
| `SESSION_BLOCK_ORDER` | block order in the browser (`Gen,Cyt,Seq,Exp`) |

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
