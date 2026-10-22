# How it works

The details behind the [README](../README.md), for when you change the code or wonder why something behaves as it
does.

## Layout

`tmux-dashboard` builds the dash window, and a `session-created` hook adds it to every new tmux session (unless
`TMUX_NO_AUTODASH=1`). `tmux-dash-layout` fits it to the screen and is re-run by a `window-resized` hook. A lock,
`~/.claude/dash/layout.lock`, keeps two runs apart.

It takes the first layout that fits. On a portrait screen it tries tall first.

| Layout | When |
|---|---|
| wide | only when btop gets its 80 columns |
| tall | btop capped at 30 rows; the rest goes to the sessions pane |
| compact | from 96 columns; btop parked on one row, where it idles at 0% CPU |
| narrow | one column |

- Windows under 40x14 are left alone.
- The sessions pane gets at least 12 rows wherever the window has 23.
- Panes are found by the command they run (`ROLES` in the script), btop included, never by index or window name. So
  a window name with spaces is fine.
- The panes are then swapped into the order tmux fills a custom layout in, because tmux ignores the pane ids inside a
  layout string.

## Hidden panels, redraws and errors

- `dashlib.visible()` asks tmux which windows a client shows: one `tmux list-windows` every 2 s, shared by all panels
  through `~/.claude/dash/shared/`.
- A panel or session browser nobody looks at pauses, and catches up within ~4 s of being shown again. The sessions
  pane's nudge and the usage pane's budget message are still checked while hidden.
- The web mirror counts as looking: it touches `~/.claude/dash/web-heartbeat` on every frame.
- The panels and the browser skip frames that did not change.
- A panel error draws one red line and writes the traceback to `~/.claude/dash/panels.err`, instead of crash-looping
  through the respawn hook.
- Every pane ignores keystrokes (`select-pane -d`). A dead pane is respawned by a `pane-died` hook.
- Shared helpers live in `bin/dashlib.py`.

## Panes: what they poll

| Pane | Polls |
|---|---|
| btop | `update_ms = 4000` (set it in `~/.config/btop/btop.conf`); `dash-btop`'s watchdog restarts btop if it freezes |
| slurm | `squeue` every 60 s and `sacct` every 5 min, one shared cache for all dashboards |
| local jobs | one `ps` scan every 10 s, shared by all dashboards |
| sensors | macmon every 4 s, iSMC every 15 s; the power line is hidden while it reads all zero |
| clock | once a minute, and at once on resize or a theme change |
| weather | Open-Meteo, no key, cached 15 min |
| spotify | every 15 s while playing (sooner near the end of a track), 30 s when paused or closed; a key or `spotify-ctl` polls at once |

**SLURM after a timeout.** The pane runs `ssh -O stop`, so the next try opens a fresh connection. Your open `ssh`
shells stay up.

## Local jobs: whose job is it?

- A process belongs to the Claude session found by walking its parent chain to a live session in
  `~/.claude/sessions/*.json`.
- A detached job (`nohup`, a double fork) has no such parent. Then the pane reads `CLAUDE_PID` from the process's
  environment through `sysctl KERN_PROCARGS2`, because `ps e` shows nothing on this macOS.
- That value is trusted only if it names a live session. The tmux server, and apps launched from a session, carry a
  stale one.
- Labels show the tool and what it works on (a script, a sample, `src -> dst`), not shell plumbing. Claude's Bash
  wrapper (`… && pwd -P >| …`) is unwrapped.
- The mem column is dropped when the pane is under 34 columns wide; numbers are never cut.
- Finished jobs are kept in `~/.claude/dash/localjobs-history.json`, and recorded only while some dashboard is open.

## Spend

- `claude_sessions.spend()` prices every API response in `~/.claude/projects`, subagents included, at API list
  price. A response that appears in several transcripts (streaming, a fork, a resume) counts once, at its largest
  token counts.
- It reads each transcript on from where it last stopped, and keeps what it read in an index in memory. The index is
  saved to `~/.claude/dash/spend-index-v3.json` at most every 10 min, and a day-old copy of that file is kept beside
  it (`.bak`).
- A transcript left unwritten for an hour is folded into token sums by day and model, plus an 8-byte hash per response
  to recognise copies. A resumed session adds on top of its sums; a purged one keeps counting.
- The index grows by ~16 bytes per response. The usage pane shows its size.
- The full design is in the comment above `_sp` in `bin/claude_sessions.py`.
- A response whose final streaming line was never written keeps its placeholder output count, so spend runs a few
  percent low.

## Session browser

- `claude-session-picker` lists the sessions from the transcripts in `~/.claude/projects`. Shared code:
  `bin/claude_sessions.py`.
- "Last used" is the last user or assistant record, not the file's mtime: Claude Code touches every open session's
  file.
- Prices are API list prices, by model. The preview says "exact" when the status line reports the size of the cache
  rewrite.
- Below 110 columns the id and date columns give way to the title; the rule line above the preview still names both.
- The hint line shows the keys that fit, most useful first.
- Folded blocks and the sort are remembered in `~/.claude/dash/picker.state`. `CLAUDE_PICKER_STATE` moves it, for
  tests.
- A search opens every block and selects the first match. Enter on a header folds the block.
- Ctrl-K ends tmux sessions together with their window, and background sessions through `claude stop`.
- Before acting, Enter, `n` and `r` check again whether the session started running elsewhere or was already
  continued, so neither happens twice.

**Topics.** The `~/.config/tmux-dashboard/topics` file holds `Name: keyword, keyword` lines, and the first match wins.
The title is tried first, then the first prompt. A word shared by 4 or more leftover titles becomes a topic by itself.

**Blocks.** A code can also come from the title ("exp011530 qPCR primers"). Sessions started in the home folder go
to `~`; anything else without a code goes to Other. A block appears as soon as it has a session. Auto is folded until
you open it once. Automated runs are the ones recorded as `entrypoint: sdk-*` in the transcript.

## Threads

- The `/handoff` skill is `claude/skills/handoff/SKILL.md`, linked into `~/.claude/skills/handoff/` by `install.sh`.
- A note's front matter holds `title`, `status: open|done`, `next:` and `continues:`. Its sections are Goal, Where it
  stands, Decisions, Dead ends, Next steps and Key files.
- **Parking.** Ctrl-T writes a stub note: `parked: yes`, `status: open`, `next:` set to your last prompt there, plus a
  digest. Then `claude -p` with no tools, reading only the digest, rewrites the stub as a real note.
  - The rewrite is rejected if the reply is not a real note, and a note edited in the meantime is never overwritten.
  - Failures go to `~/.claude/dash/park.log`.
  - Ctrl-T never overwrites a note; on a done one it only reopens it.
- **Continuing with `n`.** The new session runs as
  `claude --session-id <new> --model <the old session's model> -n "<title> (cont.)" "<prompt>"`, so a Sonnet thread
  stays on Sonnet.
- **The digest**, `~/.claude/handoffs/<id>.digest.md`, is written without an API call. It holds the first prompt,
  every prompt you typed (trimmed to ~8K characters), and the last ~25K characters of prompts and answers. Tool
  traffic is left out. The fresh session turns it into a note.
- The old session is recorded in `~/.claude/dash/continued.json`, and a note's `continues:` line links the two as well.
  `CLAUDE_HANDOFFS_DIR` and `CLAUDE_DASH_LINKS` move both files, for tests.

## Tabs and window numbers

- A window with the option `@pinned 1` is drawn after the session badge and left out of the scrolling window list.
  Clicking it works.
- `tmux-pin-tab` state `0` means explicitly unpinned. `tmux-claude-save` records the tabs you pinned.
- `dash-theme` writes the tab-strip formats into `~/.tmux/theme.conf` for every variant (`pin_block()`). After changing
  them, re-run `dash-theme <variant>`.
- `tmux-claude-renumber` keeps every Claude session running and the current window selected, because the
  `~/.claude/sessions` registry stores `@window` ids, not indexes. `-s NAME` picks a session; `--by title|path` also
  sorts.
- Reopening `dash` or `sessions` inserts it into its slot and shifts the windows behind it up by one.
- `claude --bg` sessions are not tmux windows, so they are not numbered.

## Save and restore

- Saving a named snapshot again moves the old file to `~/.tmux-claude-saves/named/history/`. Named snapshots live in
  `~/.tmux-claude-saves/named/NAME.sh`.
- A session the restore script skips gets a one-line note in its pane, with its stored title.
- Transcripts disappear when Claude Code purges them (`cleanupPeriodDays`, 30 days by default). This Mac keeps them for
  a year.

## Themes

- With MAGI, the dashboard is orange only:
  - inactive pane borders `#a85a1c`;
  - the selected pane `#ff4400`, with a dark-orange background tint;
  - default text in dash windows `#d9772b` (`DASH_TINT` in `dash-theme`).
- These are window-level options, so Claude's own windows are untouched.
- `install.sh --theme` clones SinsuSquid/MAGI-theme into `~/.local/share/dash-theme/` and backs up your tmux, btop,
  iTerm and VS Code configs first. `dash-theme default` reverts.
- The web mirror follows the theme's palette and the dash window's text colour. `DASH_WEB_PORT` changes its port.

## Screensaver

- `saver/` holds the sources of the "Tmux Dashboard" screensaver: a WKWebView showing `dash-web`. `install.sh` does not
  install it.
- It shows the dashboard for a few seconds, then goes black. The leading guess: it blanks on the global
  `com.apple.screensaver.willstop` notification and never reloads.

## Settings file

- Quotes and trailing ` # comments` are allowed in values.
- A bad number falls back to its default, with a warning.
