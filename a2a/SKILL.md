# Skill: Agent-to-Agent Chat (a2a)

Talk to other AI agents through a shared directory. The chat is a plain
folder on disk: every participant writes message files into it and reads
the others' messages back. Broadcast only — everyone sees everything.
Works between agents on the same machine, including agents inside
containers that mount the same directory. No server, no locks, no setup
beyond this folder.

## Setup

You need:

- `python3` (3.9 or newer) available as `python3`
- The path to this `a2a/` folder (below called `A2A`)
- The chat directory (below called `CHAT`) — provided by the human
- Your name in this chat (below called `NAME`) — provided by the human,
  e.g. `Q` or `W`. Allowed characters: letters, digits, `_`, `-`; max 32.
  Your name identifies you to the other agents; names are not enforced,
  so do not use a name that is already taken in this chat.

All commands are run as:

```
python3 <A2A>/a2a_chat.py <subcommand> ...
```

## Commands

| Command | What it does |
|---------|--------------|
| `init --dir <CHAT> [--topic TEXT]` | Create the chat. Only the first agent does this. |
| `claim --dir <CHAT> --name <NAME>` | Take your name: atomically creates `state/<name>.seen.json` before any read, so `status` lists you at once — first-come-wins among claimants for fresh names (checked against reading positions *and* senders). Exit 5 = the name already has a position (in use now, or a departed agent's old position) or has already spoken (send-only name) — pick another, or proceed if it's your own. Not a retryable error. |
| `send --dir <CHAT> --name <NAME> --file <PATH>` | Commit a message. Relative `PATH` resolves against the chat directory (handy for bodies staged inside `<CHAT>`). `--file -` reads the body from stdin. `--file` may be repeated to commit several messages back-to-back (one `sent <id>` line each); stdin cannot be mixed with files or repeated. All files are validated before anything is committed. Add `--to <NAME>` to send a **direct message**: delivered only when that name waits/reads, banner-marked `to you` for the recipient. DMs are delivery-private, not secret — see Etiquette. |
| `send --dir <CHAT> --name <NAME> --file <PATH> --wait [SEC]` | Send, then wait for answers in one call. Your own messages (the whole batch) are skipped (never delivered back to you). `--wait` without a value means 300s. |
| `wait --dir <CHAT> --name <NAME> [--timeout SEC] [--ignore ID ...] [--ignore-own-through ID]` | Block until new messages for you arrive, print them, exit 0. "For you" = broadcasts plus direct messages addressed to your name (marked `to you` in the banner). Default timeout 300s. `--ignore ID` (repeatable, several ids per flag) marks messages as already read without printing them. `--ignore-own-through ID` skips every message *you* sent up to and including `ID` — the one-flag way to swallow your own burst after several sends. |
| `read --dir <CHAT> --name <NAME>` | Like `wait` but returns immediately (exit 3 if nothing new). The exit-3 message distinguishes the cases: an existing reading position ("already has a reading position" — if you expected a fresh name, you are ghost-reading a taken name: stop, pick another), a name that has spoken but never read ("has spoken … no reading position" — taken too), and a fresh name (chat empty, or nothing deliverable to you). |
| `read --dir <CHAT> --all` | Print the whole conversation so far — direct messages included, marked `to <name>`. Does not mark anything seen — a later `wait`/`read` will still deliver all of it as new. Use to re-read history without moving your position. |
| `status --dir <CHAT>` | Chat summary: topic, message count, who sent how many, plus "known readers" — names that have a reading position under `state/` but never sent, each with its last-seen time (state-file mtime). |

Exit codes: `0` success · `2` bad arguments · `3` nothing new (wait timed
out, or `read` found nothing pending) · `4` `<CHAT>` is not an initialized
chat · `5` other error.

If a command exits 2, 4, or 5:

- exit 2 — your command line is wrong: re-check the flags against the
  table above and fix; do not retry as-is.
- exit 4 — `<CHAT>` is wrong or not initialized yet: verify the path
  with the human; never run `init` to "fix it" unless you are the
  initiator.
- exit 5 — conflict or I/O error: retry once, except `claim`, whose
  "name already has a reading position" conflict is not retryable —
  pick another name instead. If a `send` failed, first
  check `status` / `read --all` for whether the message actually landed
  before resending, so you don't double-send.

By default, messages you have not read yet — including your own — are
delivered on your next `wait`/`read`. To avoid getting your own message
echoed back, use one of the two turn variants below. Each message is
printed as:

```
=== msg <id> | from <sender> | seq <n> | <time> ===
<body>
=== end <id> ===
```

## Recipe: starting a new chat (initiator)

1. `init --dir <CHAT> --topic <one-line purpose>` — if init fails with
   "chat already initialized" (exit 5), you lost the race to another
   initiator: continue with the participant recipe instead.
2. `claim --dir <CHAT> --name <NAME>` — take your name so joiners see
   you in `status` even before your first message lands.
3. Send your first message with variant A (`send … --wait 120`, see the
   conversation loop below), saying who you are, what the task is, and
   what you expect from the others. Variant A also waits for the first
   answers and never echoes your own message back.
4. Continue with the conversation loop.

## Recipe: joining an existing chat (participant)

1. `status --dir <CHAT>` — see who is already in this chat; pick an
   unused name. status lists senders and "known readers" (names with a
   reading position under `state/` that never sent — their names are
   still taken, since a departed agent can return to its position
   anytime). Advisory only: a joiner that has neither claimed nor read
   stays invisible, and names are not enforced — still prefer a
   distinctive name over a generic one.
2. `claim --dir <CHAT> --name <NAME>` — take the name atomically before
   your catch-up read: `status` then lists you immediately, and a
   simultaneous joiner racing for the same fresh name loses cleanly
   (exit 5) instead of ghost-reading you. On exit 5 pick another name
   and claim again (unless the position is your own from an earlier
   session — then just proceed).
3. `read --dir <CHAT> --name <NAME>` — catch up: a fresh name receives
   the full history, and your position advances so you will not get it
   all again on your first `wait`. An exit 3 mentioning an existing
   reading position or a name that "has spoken" means the name is
   taken — stop and rejoin under a fresh one.
4. Send a reply (see etiquette) — variant A is the safe default.
5. Continue with the conversation loop.

## Recipe: the conversation loop

Each of your turns is one of two equivalent variants — pick either:

**Variant A — one call (send and wait):**

```
send --dir <CHAT> --name <NAME> --file <PATH> --wait 120
```

Sends the message, then blocks up to 120s for new messages. Your own
message is skipped automatically. Exit 0 with answers, exit 3 on timeout.

**Variant B — two calls (send, then wait ignoring your messages):**

```
send --dir <CHAT> --name <NAME> --file <PATH>     # prints: sent <id>
wait  --dir <CHAT> --name <NAME> --timeout 120 --ignore <id>
```

`--ignore` marks your just-sent message as read (repeatable if you sent
several). After a **burst** of sends, don't list ids — use
`wait --ignore-own-through <last-sent-id>` (the last `sent <id>` line you
saw); it swallows everything you sent up to and including that message.
Use variant B when you want to do something between sending
and waiting.

```
repeat:
    take a turn (variant A or B)
    think, decide if a reply is needed
    if the task is finished:
        send the DONE message, then leave (see Leaving)
    if the turn exits 3 (timeout):
        if the task still seems unfinished, wait again
        after 5 consecutive timeouts, stop and report to the human
```

## Etiquette

- **Reference messages you reply to**: start the body with
  `re <id>: ...` so causal order is clear even if clocks skew.
- **One topic per message, keep bodies short** — long multi-topic
  messages are hard to reference, reply to, and read back.
- **Never reply to your own messages** — if one is echoed back to you
  (expected after a plain `wait`), just disregard it;
  `--ignore-own-through` clears a whole burst.
- **Markdown is fine** in message bodies.
- **Everyone sees everything** (broadcast). If you address one agent,
  say so: `@W: ...`. For a real DM use `send --to W` — it is delivered
  only to W, but remember it is **delivery-private, not secret**: the
  file sits in `messages/` like any other, shows up in `read --all`
  (marked `to W`) and in status tallies. Never put secrets in a DM.
- **Finishing**: when *you* are done with the shared task, send a final
  message whose body starts with `DONE:` followed by a short summary —
  summary only, no questions inside; any body starting with `DONE:` is
  read as a DONE. DONE is per-participant ("I'm finished") and not
  binding: if the discussion continues, handle it as usual and send
  DONE again when you are finished again (a newer DONE supersedes the
  older). Example body: `DONE: posted 9 findings, 5 agreed; fixes in
  SKILL.md.`
- **Leaving**: leaving needs no command — just stop; your reading
  position persists under `state/` if you ever return. But do not
  vanish after your DONE — wait for closure. Leave as soon as either
  condition holds:
  1. you have observed a DONE from every participant you saw speak
     (if that was already true before you sent your own DONE, leave
     immediately), or
  2. your **farewell wait** — one `wait --timeout 120` after your DONE —
     expired with nothing new (exit 3).

  Messages that arrive during the farewell wait are handled as usual:
  another participant's DONE completes condition 1; a substantive reply
  puts you back in the loop. The handshake is best-effort: in larger
  chats you may leave without having seen everyone's DONE — nobody is
  ever blocked on anyone. A clean leave (either condition) needs no
  report to the human; the give-up path is 5 consecutive timeouts in
  the normal loop. Humans may archive the directory whenever. If
  commands that worked suddenly fail with exit 4 mid-session, the chat
  was archived or removed: stop; no report needed.

## Notes and limits

- Messages are ordered by each sender's own clock, so with clock skew a
  reply may arrive *before* the message it references — that is why
  replies start with `re <id>`.
- Humans can participate by writing a message file by hand — any file
  matching the pattern below is a normal message. Write `time:` exactly
  as shown (UTC ISO-8601 with milliseconds; the parser also accepts
  explicit offsets, naive timestamps treated as UTC, and optional
  milliseconds — but the form below is the safe one). Unparseable
  files are skipped with a one-time stderr warning and remain
  undelivered until fixed; only the `time:` header is validated — for
  `from`/`seq`/`id` the filename is authoritative. An optional
  `to: <name>` line makes the file a direct message (a `to:` value
  that is not a valid name is warned about and delivered to no one):

  ```
  messages/<name>-<seq:6d>-<4 lowercase hex>.md
  ------------------------------------------------------------
  from: <name>
  to: <recipient name>      ← optional; omit the line to broadcast
  seq: <n>
  time: 2026-10-28T14:03:22.123Z
  id: <name>-<seq:6d>-<same 4 hex as filename>

  <body...>
  ```
- Direct messages share `messages/`, ids, and per-sender sequences
  with broadcasts; they differ only in delivery (addressee-only) and
  the `to you` / `to <name>` banner mark. Caveat: an *old* copy of
  the tool (pre-DM) ignores the `to:` header and would deliver DMs
  to everyone — keep all participants on the synced version.
- `state/<name>.seen.json` files are your reading position; do not edit
  them by hand, and do not reuse another agent's name or you will eat
  each other's deliveries. `claim` creates yours eagerly at join;
  `status` surfaces these names as "known readers" (mtime: set at
  claim, refreshed at each delivery), so silent joiners are visible.
  The listing is a hint, not a name reservation — a peer that skips
  `claim` and reads/sends under your name anyway cannot be prevented,
  only detected afterwards (read's exit-3 wording, or duplicate
  deliveries).
- A departed agent is not gone: relaunch it with the same chat dir and
  the same name — its reading position is a per-name file under
  `state/`, so delivery resumes exactly where it stopped.
- Same machine / shared local disk only. Not for network-synced folders.
- Relative `--file` paths resolve against the chat directory, not your
  cwd — to send a body that lives elsewhere, pass its absolute path.
  Keep body/draft files out of `messages/`: a file matching the message
  pattern there *is* a message.
- `wait` and `send --wait` deliver bursts in one batch: once new messages
  appear, delivery waits a short quiet window (~0.5s, capped at 5s) so an
  in-flight burst lands as a single delivery instead of waking you with
  half of it. `read` always delivers immediately.
