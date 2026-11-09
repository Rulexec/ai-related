# oc-spawn — spawn opencode sessions into agterm companion panes

A small toolchain for launching multiple **interactive opencode TUI sessions**,
each in its own agterm **companion** session (created in the background, not
stealing focus, tinted blue), from any agterm pane on this machine. Host-only:
the client, the coordinator, and every spawned session run on the same
machine.

```
┌──────────────┐   agterm msg (request JSON)   ┌──────────────────────────┐
│ oc-spawn     │ ────────────────────────────▶ │ oc-spawn-coordinator     │
│ (client,     │                               │ (daemon, PLAIN pane,     │
│  any pane)   │ ◀──────────────────────────── │  session "ag-a2a-        │
└──────────────┘   reply FILE in state dir     │  coordinator")           │
                  + best-effort msg reply      └───────────┬──────────────┘
                                                            │ per entry:
                                                            │ agtermctl session new
                                                            │   --parent <requester>
                                                            │   --no-select ...
                                                            ▼
                                                zsh -ic 'opencode --prompt
                                                "$(cat <promptfile>)" ; ... ;
                                                exec zsh -i'
```

The client contains **no spawning logic** — it only sends one agterm message.
Both sides keep their state (replies, logs, prompt files, coordinator
address) under one state dir: `~/.cache/ocspawn` by default, `$OCSPAWN_STATE_DIR`
when set (must match on the client and the coordinator).

## Files

| File                  | What                                            |
|-----------------------|-------------------------------------------------|
| `oc-spawn`            | client (python3 stdlib only)                    |
| `oc-spawn-coordinator`| daemon (python3 stdlib, plain pane)             |
| `coordinator-start`   | sh wrapper: one-token start command for `session new --command` |
| `SKILL.md`            | agent-facing usage skill (client only)          |

## Starting the coordinator (once)

Run it **in a plain agterm pane** — not inside an opencode TUI pane: opencode
instances load a global plugin that auto-acks any non-nudge message in their
pane's mailbox, which would eat the coordinator's requests.

```sh
agtermctl session new --name ag-a2a-coordinator --no-select --cwd /tmp \
  --command /path/to/repo/oc-spawn/coordinator-start
# equivalent direct form:
#   --command '/usr/bin/python3 /path/to/repo/oc-spawn/oc-spawn-coordinator'
```

Use an **absolute** path to the script — the pane starts in `--cwd /tmp`
and cannot resolve repo-relative paths. `coordinator-start` locates the
coordinator next to itself, so any absolute path to the checkout works.

On startup it renames its session to `ag-a2a-coordinator`, creates the state
dir, publishes `<state>/coordinator.json` (heartbeat, refreshed ≤ every 20 s),
then loops on `agtermctl msg read --wait --timeout 30 --json`, handling
requests and acking them explicitly after processing. Logs go to the pane and
to `<state>/logs/coordinator.log`. An `agtermctl events` watcher thread logs
every message queued for the coordinator pane and warns if one never reaches
the read loop (third-consumer detector).

Options (see `--help`): `--state-dir` (default: `$OCSPAWN_STATE_DIR` or
`~/.cache/ocspawn`), `--session-name`, `--opencode`, `--tint` (default
`blue`; `none` disables).

## Using the client

```sh
oc-spawn --prompts-file prompts.jsonl [--cwd DIR] \
         [--wait-reply SECS | --no-wait-reply]
```

**Prompts file = JSON Lines** (one JSON object per line; blank lines and `#`
comments skipped). Multiline prompts are fine — JSON escapes them:

```json
{"prompt": "Fix the failing tests in this repo. Run them first.\nReport a summary when done.", "cwd": "/Users/me/proj"}
{"prompt": "Summarize the architecture of this repo.", "cwd": "/Users/me/other", "name": "arch-review"}
```

Per-entry fields:

| field    | required | meaning                                                        |
|----------|----------|----------------------------------------------------------------|
| `prompt` | yes      | first message; the opencode TUI auto-submits it via `--prompt` |
| `cwd`    | no       | working dir, an absolute path on this machine; default: client `--cwd` then client cwd |
| `name`   | no       | agterm session name; default `oc-<slug of prompt>`             |

opencode always starts with its **default agent and model**; per-entry
`agent`/`model` overrides were removed, and so was `target` (this is a
host-only build — sessions always spawn on this machine). Both client and
coordinator reject entries carrying those fields with a "remove the field"
error (change agent/model manually in the spawned TUI if needed).

**cwd rule**: `cwd` must be an absolute path on this machine; the coordinator
validates existence and **rejects entries whose cwd is missing** (or is not a
directory) with a clear per-entry error.

Exit codes: `0` all spawned · `1` bad usage/input · `2` coordinator
unreachable / send rejected · `3` no reply within `--wait-reply` · `4` some
entries failed.

## How a session is launched

Chosen after reading the opencode sources: the interactive TUI's native
`--prompt` flag pre-fills the input **and auto-submits it** once the app is
ready (`packages/tui/src/routes/home.tsx`). No keystroke injection, no
headless-then-resume. The pane stays fully interactive for later watching.
Prompt text is handed over via a file + `"$(cat <file>)"` — no quoting
problems. Rejected alternatives: `opencode run` (exits on idle, auto-rejects
permission questions), `session type` injection (timing-fragile).

Prompt file under `<state>/prompts/`; pane command `zsh -ic 'opencode
--prompt "$(cat <f>)" [flags]; rc=$?; echo "[oc-spawn] opencode exited ($rc);
reconnect with: opencode --continue"; exec zsh -i'` — the pane stays a live
shell after opencode exits.

Each spawned session is created with `agtermctl session new --parent
<requester session> --no-select --cwd ... --command ... --name ... --tint blue`.

## Message protocol (v1)

The client's `--prompts-file` is parsed **locally at the client edge** — only
the already-parsed entries travel (inlined in the request body, subject to
agterm's 64 KiB message cap; the client refuses bodies over ~60 KiB and the
coordinator caps at 64 entries).

**Request** — one agterm message, JSON body, sent by the client to the
coordinator pane (`agtermctl msg send --stdin --to <session>:<pane> --json`;
coordinator address resolved from `<state>/coordinator.json` if its
heartbeat is fresh, else by scanning `agtermctl window list` + `tree --json`
for the session named `ag-a2a-coordinator`):

```json
{
  "ocspawn": 1,
  "id": "req-<12hex>",
  "requester": {"session": "<AGTERM_SESSION_ID>", "pane": "<AGTERM_PANE_ID>"},
  "entries": [
    {"prompt": "...", "cwd": "/Users/...", "name": "..."}
  ]
}
```

**Reply** — the coordinator writes `<state>/replies/<reqid>.json` (atomic
rename):

```json
{
  "ocspawn-reply": 1, "id": "req-...", "ok": true, "spawned": 2, "total": 2,
  "results": [
    {"index": 0, "ok": true, "session": "<new session id>", "name": "oc-...",
     "cwd": "/Users/..."},
    {"index": 1, "ok": false, "error": "cwd '/Users/me/nope' does not exist (or is not a directory)"}
  ],
  "coordinator": {"session": "...", "pane": "...", "name": "ag-a2a-coordinator"},
  "ts": 1234567890.1
}
```

plus a best-effort one-line summary via `msg send` to the requester's pane
(note: auto-consumed if the requester is an opencode TUI pane — that is why
the file is authoritative). Messages are acked only after being handled, so a
crashed coordinator re-reads pending requests on restart.

## Demos

```sh
cat > /tmp/demo1.jsonl <<'EOF'
{"prompt": "List the files in this directory and briefly say what this project is. Then stop.", "cwd": "/Users/me/proj"}
EOF
oc-spawn/oc-spawn --prompts-file /tmp/demo1.jsonl

cat > /tmp/demo2.jsonl <<'EOF'
{"prompt": "Run the tests and report failures. Then stop.", "cwd": "/Users/me/proj"}
{"prompt": "Summarize the architecture of this repo.", "cwd": "/Users/me/proj", "name": "arch-review"}
EOF
oc-spawn/oc-spawn --prompts-file /tmp/demo2.jsonl --wait-reply 90
```

Spawned companions appear in the agterm sidebar under the requesting session
(tinted blue). After opencode exits, the pane stays on a shell with a
`opencode --continue` reconnect hint.

## Caveats

- The coordinator must be the **sole consumer** of its pane mailbox — keep it
  in a plain pane and don't run `msg read --ack` against it by hand.
- Requests are limited to 64 entries / ~60 KiB body (agterm caps at 64 KiB).
- The coordinator validates cwd existence on this machine and reports a
  per-entry error if the dir is missing.
- If the requesting pane closes before the spawn, `session new --parent` fails
  and the entry reports the error in the reply.
