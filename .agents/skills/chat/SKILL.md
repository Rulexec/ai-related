---
name: chat
description: Use when the user wants several agents to talk to each other in one shared chat — asks to create or set up a chat for given participants ("create a chat for the following participants: reviewer, dev"), describes a scenario of two or more agents discussing a topic and wants ready-to-paste prompts for their sessions, mentions multi-agent or a2a chat setup, or refers to a chat by a container path (/workspace/shared/chats/...). Produces an initialized chat plus one paste-ready prompt per participant. Not for plain participation in an existing chat — that is the a2a skill.
---

# Skill: Multi-Agent Chat Setup

Turn a user's scenario ("a reviewer and a developer discussing code") into a
running a2a chat: a fresh chat directory plus one ready-to-paste prompt per
participant. This skill only sets things up — the participants do the talking.

## Step 0 — Read the a2a skill

Read `a2a/SKILL.md` (repo root) before anything
else. It defines the chat protocol (init/claim/send/wait, join recipe,
etiquette, DONE and leaving) that every generated prompt relies on.
Shorthand below: `A2A = a2` (the repo-root folder),
tool = `python3 <A2A>/scripts/a2a_chat.py`.

## Step 1 — Clarify the scenario

From the user's request determine: the chat's goal, the participants (count
and roles), and what each participant should do. If any of these is
ambiguous, ask before creating anything. Never invent extra participants.

## Step 2 — Create the chat: pick folder AND init, always

Creating a chat is one atomic action — picking the folder and initializing
it. A mere directory is not a chat: without a successful `init` every
participant command fails with exit 4, so the skill never leaves a folder
behind uninitialized and never hands out prompts for one.

1. Set `CHAT` to a random name under `~/chats` that does not exist yet.
   Format `<slug>-<hex4>`: slug is 1–2 short lowercase hyphenated words from
   the goal (e.g. `code-review`), hex4 is 4 random lowercase hex chars —
   e.g. `code-review-7f3a`. List `~/chats` (a plain `ls` of that directory
   is enough; if it does not exist yet, every name is free — `init` creates
   the directory) and verify the name is free; on collision regenerate
   hex4 and re-check. Never reuse or write into an existing chat folder
   unless the user explicitly names it.
2. Immediately initialize it with the one-line goal as the topic:

   ```
   python3 <A2A>/scripts/a2a_chat.py init --dir <CHAT> --topic "<one-line goal>"
   ```

3. The chat is created only when this `init` exits 0. If it fails with
   exit 5 ("chat already initialized") despite the fresh name, pick a new
   name and retry once; report any other failure to the user as-is and
   stop — do not continue to the prompts.

The orchestrator stays out of the chat after init: no claim, no messages
(exception in "Current agent as participant" below).

## Step 3 — Assign participant names

One name per participant, derived from its role: `reviewer`, `dev`,
`tester`, `writer`... Allowed characters: letters, digits, `_`, `-`; max 32
chars; all names distinct within the chat. The chat is fresh, so every name
is free — no need to check.

## Step 4 — Write one prompt per participant

Each prompt is at most two short paragraphs and must contain, in any order:

1. An instruction to read the repo's `a2a/SKILL.md`
   and follow it (its "joining an existing chat" recipe covers claim,
   catch-up read, greeting, conversation loop, etiquette).
2. The participant's name.
3. The chat path, in its local form (`~/chats/<name>`; for `/workspace/...`
   forms see "Paths from the container environment" below).
4. The chat's goal and this participant's role in it: what to do, what to
   expect from the others, and what "done" looks like for them.
5. An instruction to send a short greeting (who they are, their role) right
   after joining, then wait for messages and work toward the goal until
   they can send a `DONE:` message.

Do not duplicate a2a mechanics (flags, exit codes, file formats) in the
prompts — the a2a skill owns that. Keep each prompt self-contained, with no
references to "the other prompt".

## Step 5 — Present the result

Give the user:

- The chat path and its topic line.
- Each prompt in its own labeled code block (`### reviewer`, `### dev`, ...)
  ready to copy into a fresh agent session that has access to these
  directories (the chat folder and `A2A`).
- One line on monitoring:
  `python3 <A2A>/scripts/a2a_chat.py status --dir <CHAT>` (or `read --all`)
  shows the conversation's progress at any time.

## Paths from the container environment

New chats always live under `~/chats`. But a chat path handed to you (or to
a participant prompt) may come from the container environment and look like
`/workspace/shared/chats/<rest>` — there is no `/workspace` here. Such a
path refers to the shared mount's `chats/<rest>` directory (the host path
backing `/workspace/shared` in your environment): search for the chat there
and use that local path. (The a2a skill is different: it lives in this
repo — a `/workspace/shared/a2a` reference means the repo's `a2a/` folder.)
Joining and monitoring chats found there is fine; never create new chats
in the shared mount — new chats go to `~/chats` only.

## Current agent as participant

If the user asks this very agent to take one of the roles ("you will be the
reviewer"), it becomes that participant itself: claim the name (the chat is
already initialized — Step 2 did it), send the greeting with `--wait`, and
follow the conversation loop from the a2a skill. Generate paste-prompts
only for the remaining participants.

## Rules

- Creating a chat always includes `init` — never hand out prompts for a
  chat where init has not exited 0.
- Fresh chat folders only; never write into an existing chat.
- Create new chats only under `~/chats`; `/workspace/shared/chats/...`
  paths resolve via the shared-mount mapping and are never written as new.
- Stay out of the chat unless participating.
- Never run `unlink`, delete, or archive anything under `~/chats` or the
  shared mount's `chats/` directory — those belong to the
  human.
- On any unexpected failure, stop and report the command and exit code.
