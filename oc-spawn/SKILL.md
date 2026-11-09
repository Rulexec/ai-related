---
name: oc-spawn
description: Use when the user wants to launch one or more opencode instances as separate interactive sessions — asks to spawn, launch, or start opencode (e.g. "spawn opencode with these prompts", "start opencode sessions for these tasks"), mentions oc-spawn or its prompts file. Each prompt becomes its own agterm companion pane running the opencode TUI with the prompt auto-submitted. Not for agents talking to each other in a shared chat — that is the a2a skill.
---

# Skill: oc-spawn

Launch interactive opencode TUI sessions into background agterm companion
panes — host-only: the client, the coordinator, and every spawned session
run on this machine.

## What you get

For every entry in a prompts file, oc-spawn creates an agterm session
that is:

- a **companion of the pane you run the client from** — listed under
  your session in the sidebar, tinted blue, never stealing focus;
- a **fully interactive opencode TUI** with the entry's prompt
  auto-submitted as its first message (watch it, type into it later);
- still alive after opencode exits — the pane drops to a shell with a
  `opencode --continue` reconnect hint.

## Requirements

- Run the client **from an agterm pane**: spawned sessions attach under
  that pane's session.
- `python3` and `agtermctl` on PATH.
- The oc-spawn coordinator daemon running (started once; it survives).
  If it is down, the client exits 2 and prints the exact command to
  start it — run that in a plain agterm pane, then retry.

## Where the files are

| What   | Repo path           |
|--------|---------------------|
| client | `oc-spawn/oc-spawn` |
| skill  | `oc-spawn/SKILL.md` |

Client and coordinator keep their state (replies, logs, prompt files,
coordinator address) under `~/.cache/ocspawn`, or `$OCSPAWN_STATE_DIR`
when set — both sides must use the same directory.

## Usage

```sh
<client> --prompts-file prompts.jsonl [--cwd DIR] \
          [--wait-reply SECS | --no-wait-reply]
```

| flag               | meaning                                                        |
|--------------------|----------------------------------------------------------------|
| `--prompts-file`   | required: JSONL file of entries (below)                        |
| `--cwd DIR`        | default `cwd` for entries that omit it (default: client's cwd) |
| `--wait-reply SECS`| seconds to wait for the result summary (default 60)            |
| `--no-wait-reply`  | send and exit immediately                                      |

## Prompts file (JSON Lines)

One JSON object per line; blank lines and `#` comments are skipped.
Multiline prompts are fine — JSON escapes them:

```json
{"prompt": "Fix the failing tests in this repo. Run them first.\nReport a summary when done.", "cwd": "/Users/me/proj"}
{"prompt": "Summarize the architecture of this repo.", "cwd": "/Users/me/other", "name": "arch-review"}
```

| field    | required | meaning                                                |
|----------|----------|--------------------------------------------------------|
| `prompt` | yes      | first message, auto-submitted in the new opencode TUI  |
| `cwd`    | no       | working dir — an absolute path on this machine         |
| `name`   | no       | agterm session name; default `oc-<slug of the prompt>` |

opencode always starts with its default agent and model — change them
manually in the spawned TUI if needed. Entries carrying `target`,
`agent`, or `model` fields are rejected with a "remove the field"
error.

**cwd rule**: `cwd` must be an absolute path on this machine; the
coordinator rejects entries whose cwd does not exist (or is not a
directory).

## Reading the result

The client prints a per-entry summary:

```
oc-spawn: coordinator reply: 2/2 entries spawned
  [0] OK   session 85AF160B… name='oc-Fix-the-failing-tests' cwd='/Users/me/proj'
  [1] FAIL cwd '/Users/me/nope' does not exist (or is not a directory)
```

| exit | meaning                                                                  |
|------|--------------------------------------------------------------------------|
| 0    | all entries spawned                                                      |
| 1    | bad usage / bad prompts file                                             |
| 2    | coordinator unreachable or send rejected (see Requirements)              |
| 3    | no reply within `--wait-reply` — the spawn may still land; check the sidebar or `~/.cache/ocspawn/replies/` |
| 4    | some entries failed — see the printed per-entry errors                   |

## Examples

```sh
# one session in a host repo
cat > /tmp/one.jsonl <<'EOF'
{"prompt": "List the files in this directory and say what this project is. Then stop.", "cwd": "/Users/me/proj"}
EOF
oc-spawn/oc-spawn --prompts-file /tmp/one.jsonl

# two sessions, one named
cat > /tmp/two.jsonl <<'EOF'
{"prompt": "Run the tests and report failures. Then stop.", "cwd": "/Users/me/proj"}
{"prompt": "Summarize the architecture of this repo.", "cwd": "/Users/me/proj", "name": "arch-review"}
EOF
oc-spawn/oc-spawn --prompts-file /tmp/two.jsonl --wait-reply 90
```

## Limits and notes

- Max 64 entries and ~60 KiB per request — split bigger batches.
- Run the client from a live agterm pane — spawned sessions attach
  under the calling pane's session; if that pane closes before the
  spawn, the entry fails with a `session new --parent` error.
- Machine-readable results land in
  `~/.cache/ocspawn/replies/<reqid>.json`
  (or `$OCSPAWN_STATE_DIR/replies/…`).
