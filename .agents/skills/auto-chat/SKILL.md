---
name: auto-chat
description: Use when the user wants a multi-agent chat whose participants are launched automatically — asks to create a chat AND spawn/start its participants ("create a chat where sunny and rainy discuss the weather", "spawn two agents to discuss X", "set up a chat and launch everyone"), mentions auto-chat, or wants several opencode instances spawned to work on one topic together without paste-ready prompts. Produces an initialized a2a chat plus one spawned opencode companion per participant, with the initiator joined as a participant. Not for chat setup with paste-ready prompts — that is the chat skill; not for spawning opencode sessions outside a shared chat — that is the oc-spawn skill.
---

# Skill: Auto-Chat (participants spawned, not pasted)

Fully automated variant of the chat skill: create an a2a chat, then
LAUNCH each participant as an interactive opencode companion via
oc-spawn — no paste-ready prompts, the agents join by themselves. The
initiator (you, the session using this skill) is always a participant of
the chat too, by default an observer.

## Step 0 — Read the three skills

- `chat` (`.agents/skills/chat/SKILL.md`) — folder naming, init rules,
  the participant-prompt spec. auto-chat inherits all of them.
- `a2a` (`a2a/SKILL.md`, repo root) — the chat protocol every
  participant follows (join recipe, etiquette, DONE/leaving).
- `oc-spawn` (`oc-spawn/SKILL.md`, repo root) — the launcher.

Shorthand below: `CLIENT` = `oc-spawn/oc-spawn` (repo root).

## Step 1 — Goal → participants

The user provides the goal of the chat. From it determine the
participants (count, names, roles) exactly like the chat skill: ask if
ambiguous, never invent extras. Also fix your own stance (observer by
default; active participant if the goal says so).

## Step 2 — All participants run on this machine

Spawn entries carry no `target` field — oc-spawn always spawns on this
machine (host-only). Every participant can read the a2a skill and the
chat directory right where they are.

## Step 3 — Create the chat (init, always)

Per the chat skill: pick `<slug>-<hex4>` (slug from the goal), verify
the name is free, then `init` with the one-line goal as the topic —

```
python3 a2a/scripts/a2a_chat.py init --dir <CHAT> --topic "<goal>"
```

The chat exists only when `init` exits 0. Never spawn participants for
a chat where init has not succeeded. Never reuse an existing folder.

## Step 4 — Join FIRST: the initiator is a participant

Before spawning anyone:

1. `claim --dir <CHAT> --name <your name>` — you must be in the chat,
   not just watching from outside (your name appears in `status`, and
   participants can address you).
2. Send your greeting with variant B (`send … --file <body>`, no wait),
   saying who you are and your stance — default: "I set this up and
   observe; discuss between yourselves; DONE per etiquette when you've
   said your piece". Note the returned message id: swallow it later with
   `wait --ignore-own-through <id>`.

Sending the greeting BEFORE spawning means every participant catches it
in their catch-up read — it primes the discussion without interrupting
it later.

## Step 5 — Build the prompts file (one JSONL, one entry per participant)

Each participant's prompt is at most two short paragraphs and contains,
in any order (the chat skill's participant-prompt spec):

1. an instruction to read the repo's `a2a/SKILL.md` and follow
   it (its joining recipe covers claim, catch-up, greeting, loop,
   etiquette, DONE/leaving);
2. the participant's name;
3. the chat path;
4. the goal and this participant's role, what to expect from the others
   (including your stance), and what "done" looks like — keep it
   concrete: short messages, a message budget (e.g. "3–5 messages"),
   DONE when finished;
5. an instruction to send a short greeting right after joining.

Entry fields: `prompt`, `cwd` = the chat dir, `name` = the
participant's name. Build the JSONL with `json.dumps` (prompts contain
newlines and quotes — hand-written JSON breaks).

## Step 6 — Spawn everyone in one request

```
<CLIENT> --prompts-file /tmp/<chat-name>.jsonl --wait-reply 90
```

Expect `N/N entries spawned` (exit 0); every participant becomes a
blue-tinted companion under YOUR session. On failures: the printed
per-entry errors say why — fix the prompts file and re-run; do not
retry blindly. The oc-spawn coordinator must be running (if it is down,
the client exits 2 and prints its start command).

## Step 7 — Participate and observe

Catch up (`read`), then run the a2a conversation loop (`wait --timeout
120` …): relay the discussion to the user. As a participant you may
address people by name to steer (`@sunny: wrap up, please`) — that is
the point of joining. When every participant you saw speak has sent
DONE and you are finished, send your own DONE and leave per a2a
etiquette. After 5 consecutive timeouts with the task unfinished,
stop and report to the user.

## Rules

- All chat-skill rules about chat creation apply unchanged (init-0
  gate, fresh `<slug>-<hex4>` folders, `~/chats`).
- The initiator ALWAYS joins the chat (claim + greeting) — never stays
  fully outside.
- One oc-spawn request for all participants; never spawn before init
  exited 0; never spawn participants the user did not ask for.
- Participant panes stay alive after their agents leave (interactive by
  design) — never close or restart them on your own; the human decides.
- On any unexpected failure, stop and report the command and exit code.
