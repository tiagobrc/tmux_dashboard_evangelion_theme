# tmux-dashboard news

## 2026-10-22: spend index v3, sums instead of every response

- **What changed.** A transcript left unwritten for an hour (`FREEZE_AFTER`) is folded into token sums by day and
  model, plus an 8-byte hash of each response id. The index no longer keeps every response.
  - A session resumed later is read on from where it stopped, and its new responses are added on top of the sums.
  - A response copied into another transcript (a fork, a continuation, a subagent file) is recognised by its hash and
    counted once, at its largest token counts.
  - A transcript Claude Code purged keeps its sums and its hashes, since a copy of its responses can outlive it. So
    does the older version of a transcript that was replaced or truncated (kept as `path#inode`).
- **Memory.** Per process: 22.6 MB before, now 1.8 MB with everything frozen, plus the responses of the sessions
  written in the last hour (7 MB on a busy afternoon). It still grows with history, but by 16 bytes a response (its
  hash) instead of about 900: about 4 MB a year per process at October's rate, instead of about 240 MB. The index file
  went from 2.3 MB to 0.9 MB, and a panel's first count after a restart from 4.8 s to 0.1 s.
- **Same numbers.** Monthly totals, every session's total, and spend by day, week and model are identical to v2 to the
  cent. Tested on a snapshot of every transcript: a fresh build, a migration from v2, everything frozen,
  nothing frozen, a second process, a session resumed after freezing (with a copy from another session), two
  processes merging each other's writes, a purged transcript, a changed long-prompt threshold, and three processes
  starting at once. An independent review added a fuzzer: three processes, random writes, idling, freezes, merges,
  lock timeouts, restarts, purges and replaced files, checked against the old code after every step: 2,000 runs, all identical.
- **Changed on purpose.** In `spend_breakdown` (today's top session, `claude-session-index --weekly`), a response two
  sessions hold now counts for the oldest transcript, the session that made the API call; it used to be split evenly.
  On one day's data that moved a few dollars between sessions.
- **Safety.**
  - Writes and freezes hold `~/.claude/dash/spend-index-v3.lock`, and a freeze first folds in what other processes
    wrote, so two transcripts never both count one response.
  - A day-old copy, `spend-index-v3.json.bak`, is kept: the sums of purged transcripts exist only in the index.
  - When a long-prompt price threshold changes, the frozen transcripts still on disk are read again.
- **Upgrade.** The first run starts from `spend-index-v2.json`, so no transcript is read again. After that,
  `spend-index-v2.json` is not used and can go to a holding folder with v1.
- **Transcripts kept a year.** The recommended `cleanupPeriodDays` is now 365, not 3650. At October's rate that is
  about 1.3 GB of transcripts a month, about 16 GB a year. The index only reads transcripts; it never changes or
  deletes them.

## 2026-10-22: efficiency, layout and session-browser review

### Efficiency

- **Nobody looking, no work.** Every panel and the session browser pause while no tmux client shows their window,
  and skip frames that did not change. The check, `dashlib.visible()`, is one `tmux list-windows` every 2 s, shared by
  all panels. The web mirror counts as looking. Measured:
  - Two dashboards together used about 25% of a core around the clock. Each now uses about 1.7% of a core while
    viewed and about 1.0% while hidden.
  - About 375 process spawns per minute are gone.
- **One fetch for every dashboard** (`dashlib.shared`):
  - local jobs: one `ps` every 10 s;
  - Spotify: every 15 s while playing (sooner near the end of a track, never under 5 s), every 30 s when paused or
    closed;
  - SLURM: one cache already shared by all dashboards.
- **Spend index v2** (`~/.claude/dash/spend-index-v2.json`). It is kept in memory and written at most every 10 min,
  down from about 27 GB of rewrites a day to about 0.3 GB. Each transcript is read on from where the last read
  stopped. The browser's `build()` went from 0.074 s to 0.008 s.
- **Panels:**
  - sensors: macmon every 4 s, iSMC every 15 s, and macmon is stopped while the window is hidden.
  - dash-btop: its watchdog waits on a FIFO instead of polling.
  - clock: draws once a minute, and at once on resize or a theme change.
  - btop: `update_ms = 4000` is recommended.
- **`claude-statusline`:** starts with `python3 -S`, writes no temp files, and never fails on bad input.

### Layout

- **`tmux-dash-layout`, rewritten.** It takes the first of four layouts that fits; on a portrait screen it tries tall
  first. Windows under 40x14 are left alone.

  | Layout | When |
  |---|---|
  | wide | only when btop gets its 80 columns |
  | tall | btop capped at 30 rows; the rest goes to sessions |
  | compact | from 96 columns; btop parked on one row, where it idles at 0% CPU |
  | narrow | one column |

  The sessions pane gets at least 12 rows. Panes are found by the command they run, never by index or window name.
  Runs are serialised by `~/.claude/dash/layout.lock`.
- **Names with spaces.** Window names containing spaces now work in `dash-theme`, `tmux-sessions-window` and
  `tmux-pin-tab`.

### Sessions panel

- Sessions are sorted busy, then warm, then cold by wake cost.
- A red `×2` marks one conversation running in two windows.
- "… N more · K over 400K hidden" ends the list when it does not fit.
- The legend shows only when rows are free.
- **Cache-expiry nudge.** An idle session past `CTX_WARN_K` with less than `NUDGE_MIN` minutes of warm cache left gets
  one tmux message on every attached client: continue it now, or `/handoff`. `NUDGE_MIN` defaults to 10; 0 turns it
  off.

### Usage panel

- **Removed:** the per-session context block. The sessions panel and the browser show it.
- **Added:**
  - "warm now: N sessions, $/step · next cold in …";
  - this month, its pace, and its share of `MONTH_BUDGET_USD`;
  - today, the last 7 days, and the top session;
  - 7-day bars when the pane is tall.
- **Budget alerts.** One tmux message at 80% of the budget and one at 100%.
- **`claude-session-index --weekly [--week YYYY-Www] [--out FILE]`** writes a weekly summary in Markdown. The file goes
  to `WEEKLY_EXPORT_DIR` when that is set.

### Session browser

- **Keys:**
  - Ctrl-S cycles the sort within blocks: recent, spent, context, wake cost. The choice is remembered.
  - Ctrl-V opens the row's note, or its digest, in a tmux popup.
  - `?` (with an empty filter), Ctrl-/ or F1 lists every key. A hint line shows the keys that fit.
- **Filters.** Words combine with tokens: `is:open is:warm is:cold is:running is:idle is:orphan`, `>400k` / `<100k`
  for context, `block:Exp`, and `$>5` / `$<1` for spend. The search also reads a note's title and its `next:` line.
- **Colours:**
  - bold cyan (the theme's second colour): cache warm;
  - plain: cold;
  - yellow or bright red: context past `CTX_WARN_K` or `CTX_CLEAR_K`;
  - bold white dot: busy right now.
- **Open block.** The oldest note comes first. Each row shows its note's age (`note 3d`), yellow after 7 days.
- **Orphans.** A note whose transcript Claude Code purged still gets a row, marked "transcript gone". Enter there offers
  only `n`.
- **Continuing a thread.** Enter, `n` and `r` first check again whether the session started running elsewhere or was
  already continued, so neither happens twice. When a thread is continued from a note it inherited, the new session's
  prompt names that note and a fresh digest.
- **Park (Ctrl-T)** writes the stub note at once. In the background, `PARK_MODEL` then rewrites it as a real note with
  a title and a `next:` line. The model reads only the digest. The default, `haiku`, takes about 15 s and costs under
  1¢; `off` keeps the stub.
  - A reply that is not a real note is rejected.
  - A note edited in the meantime is never overwritten.
  - Failures go to `~/.claude/dash/park.log`.
- **Digests** now include every prompt you typed in the session, sampled when long (about 8K characters).

### Prices and counting

- **Corrected prices:**
  - Sonnet 5.5 cache reads cost $0.10/MTok, not $0.20.
  - Haiku 5.5 prompts over 100K tokens are now priced at $0.50 / $2.50 / $0.05.
  - Fast mode is priced at 2× on Opus 5.5, Opus 5 and Opus 4.8.
  - Opus 4 and 4.1 are priced at $15 / $75.
- **Money format.** Money is always in dollars, right-aligned: `$.06 $8.0 $25 $1.7k`.
- **Duration format.** Every panel uses one format: `45s 59m 4h05 4d17h 12d`.

### Fixes

- **tmux formats.** `#` is escaped as `##` wherever a folder or title reaches a tmux format: `new-window -c` and the
  new Ctrl-V popup title. A folder or title containing `#(...)` could otherwise run a shell command.
- **Two browsers.** They no longer overwrite each other's collapsed or expanded blocks: `picker.state` is merged
  three ways and written atomically.
- **Enter pressed twice** within 30 s no longer resumes a session twice.
- **Bad input.** A JSON line that is not an object no longer stops a panel.
- **Transcripts.** A transcript that only looked truncated is re-checked before it is read again from the start.
- **Handoff notes.**
  - The status is written atomically, and front matter is added when a note has none.
  - Links are written under a lock.
  - Empty notes are skipped.
- **Windows.** A session maps to a window only when its pane is in that window.
- **SLURM.**
  - A hung ssh no longer freezes the panel. A ControlMaster left dead by the Mac sleeping, for example, now times out
    after 90 s.
  - The panel stops the master only when a second probe also times out. It uses `-O stop`, which keeps your open
    `ssh cluster` shells alive.
  - squeue and sacct back off and report errors separately, and the last good rows stay on screen.
- **Weather.** It redraws on resize through a SIGWINCH pipe instead of inside the signal handler, where a frame could
  land in the middle of another. The clock does the same, and `dash-theme` repaints it.
- **Errors.** A panel error draws one red line, with the traceback in `~/.claude/dash/panels.err`, instead of
  crash-looping through the respawn hook.
- **Config.** Quotes and ` # comments` are allowed in values; a bad number falls back to its default with a warning.
- **`install.sh`.** It no longer warns that the status line is missing when `settings.json` refers to it through
  `~/` or `$HOME`.
- **`tmux-claude-save`** gains `--live --all`.

### Upgrade notes

- **Old cache files.** `~/.claude/dash/spend-index.json` (v1) is read once, to keep transcripts that were purged before
  v2. `~/.claude/dash/visible.json` was replaced by `shared/visible-<server>.json`. Neither is used again, and both
  can be moved to a holding folder.
- **Keeping transcripts.** Claude Code deletes transcripts after `cleanupPeriodDays`, 30 days by default.
  `"cleanupPeriodDays": 365` in `~/.claude/settings.json` keeps them a year; `install.sh` does not set it.
- **New settings** in `~/.config/tmux-dashboard/config`: `NUDGE_MIN`, `MONTH_BUDGET_USD`, `PARK_MODEL` and
  `WEEKLY_EXPORT_DIR`. See `config/dashboard.conf.example`.
- **Known issue, fixed by spend index v3 (above).** The spend index lives in each process that shows spend: the
  sessions panel, the usage panel and the browser, once per dashboard.
  - On 2026-10-22 it took 21.6 MB per process for 23.8k API responses, about 0.9 KB per response.
  - It grows with your history. At the October rate, about 20k responses a month, that is about 240 MB per process
    after a year.
  - Rebuilding the response table took 13 ms.
