---
name: handoff
description: Write or update the handoff note for this Claude Code session, so the session can be closed and its thread picked up later in a fresh one. Use when the user runs /handoff or asks for a handoff / wrap-up note, or when a session started from a digest or handoff file is asked to write one.
argument-hint: "[done | extra note for next time]"
---

# Handoff note

Write the note to `~/.claude/handoffs/${CLAUDE_SESSION_ID}.md` (create the folder if needed). The session browser in
tmux (window 1) reads it: threads with `status: open` gather in its **Open** block with their `next:` line, and Enter
there offers to continue the thread in a fresh session that starts from this note — so closing this session loses nothing.

If the file already exists, this session wrote a note before: update it in place — keep what is still true, revise the
rest. Never touch other handoff files.

## Arguments

- `$ARGUMENTS` is `done` → `status: done` (the thread is finished). Still update the note so it records how it ended.
- anything else → an extra note for next time; work it into the right section.
- empty → `status: open`.

## Format — this front matter, then these sections

```markdown
---
session: ${CLAUDE_SESSION_ID}
title: <what this thread is, ≤ 60 characters — not a date, not "handoff">
folder: <absolute path of the working folder>
status: open
next: <the single next action, one line, concrete enough to start on without reading further>
continues: <id of the handoff note or digest this session started from — leave the line out if none>
updated: <YYYY-MM-DD HH:MM, local time>
---

## Goal
Why this thread exists and what "done" looks like. 2–4 lines.

## Where it stands
What exists now: results, files written, jobs running (job ids, where their output lands), the numbers that matter.

## Decisions
What was decided and why — the reasons are what a fresh session can't reconstruct.

## Dead ends
What was tried and failed, and why, so it isn't tried again.

## Next steps
Numbered, in order. The first is the `next:` line.

## Key files
Absolute paths, one per line, a few words each on what it is.
```

## Rules

- Write for a reader with no memory of this conversation — the user next week, or a fresh Claude session: absolute paths,
  exact commands, job ids, sample names, numbers. Never "the file we discussed".
- Under ~80 lines. Point at files instead of pasting their contents or long outputs.
- Only what is true now; don't narrate the conversation.
- If this session began from another handoff note or digest (`~/.claude/handoffs/<id>.md` or `<id>.digest.md`), put
  that id in `continues:` — the browser uses it to fold the old session under this one.
- Write the note and nothing else: no commits, no clean-up, no other edits.

## Then reply in three lines

1. The path written.
2. The `next:` line.
3. "Safe to close this session: it stays in the session browser under Open (window 1), and Enter there continues it in a
   fresh session from this note." — for `done`: "Marked done: it leaves the Open block."
