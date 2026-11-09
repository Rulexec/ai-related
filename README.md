# Agent tooling for opencode

Assorted tooling for running AI agents with [opencode](https://opencode.ai):
a directory-based multi-agent chat, agterm integration plugins, a spawner
for interactive opencode sessions, and a set of agent roles and skills.

Everything here is transport-light and mostly stdlib-only: the chat is a
plain folder, the plugins are single TypeScript files, the spawner is two
Python scripts. Each component works standalone; together they let several
opencode instances (and humans) talk to each other and be launched on
demand.

## Repository layout

| Path | What it is |
|---|---|
| `a2a/` | Agent-to-agent chat over a shared directory (Python, stdlib only) |
| `opencode/` | opencode plugins for the agterm terminal (server + TUI) |
| `oc-spawn/` | Launch interactive opencode sessions into background panes |
| `.agents/skills/` | Skills discovered by the agent harness (`chat`, `auto-chat`, plan-workflow skills) |
| `.agents/roles/` | Role files for the agent harness (architect, code, ask, …) |
| `plans/` | Plan documents consumed by the plan-workflow skills |

## a2a — multi-agent chat (`a2a/`)

Agents (and humans) talk through a shared directory: every participant
writes message files, reads the others back. Broadcast by default, direct
messages optional, no server, no locks. Works between processes on one
machine, including containers that mount the same directory. Not for
network-synced folders.

- **Needs from zero**: `python3` (≥ 3.9, stdlib only) and a directory all
  participants can see. No opencode, no agterm, no daemons. The folder
  `a2a/` itself just needs to be readable wherever it is invoked from.
- **Use it**: hand an agent the skill at `a2a/SKILL.md` (or paste its
  commands yourself). One participant runs `init`, everyone `claim`s a
  name, then `send` / `wait` / `read` in a loop. Full command reference,
  join/leave recipes, and etiquette live in that skill file.
- **Humans participate** by writing a message file by hand — the format
  is documented in the skill's "Notes and limits".
- **Nudges are optional**: when messages wait for an idle participant,
  `a2a/scripts/agterm.py` can poke it awake over the agterm message bus.
  Outside agterm this pass is a silent no-op — silent peers are not gone
  peers; poll with `wait` instead.

### Tuning for other harnesses

The chat core is transport-agnostic (files + atomic rename). Any agent
that can run `python3` against a shared directory can join — different
harnesses, humans, plain shells. To replicate the wake-up nudge in
another harness, the payload contract and delivery rules are documented
in the header of `a2a/scripts/agterm.py` and the receiver plugin
(`opencode/plugins/agterm-a2a.ts`, "Contract (v1)"): one anonymous JSON
message addressed to a pane, turned into a prompt by a receiver plugin.

## Skills and roles (`.agents/`)

Harnesses that read `.agents/` (opencode does) get:

**Roles** (`.agents/roles/`, indexed in `ROLES.md`) — one active mode per
conversation: `architect` (plan before implementation), `code`
(implement), `ask` (questions, no changes), `orchestrator` (multi-step
coordination), `scoper` (vague idea → specification), `prompter`
(LLM prompt crafting), `librarian` (knowledge capture).

**Skills**:

| Skill | Purpose |
|---|---|
| `a2a` (lives in `a2a/`) | participate in / run a chat via the shared directory |
| `chat` | set up a chat for given participants, producing paste-ready prompts |
| `auto-chat` | set up a chat **and spawn** its participants via oc-spawn |
| `oc-spawn` (lives in `oc-spawn/`) | launch interactive opencode companion sessions |
| `subplans-orchestrate` / `subplan-implement` / `plan-split` / `plans-archival` | plan lifecycle: split a grand plan into phase sub-plans, implement them one by one, archive when done (works on `plans/`) |

The skills reference each other and the tools by repo-relative paths
(`a2a/SKILL.md`, `oc-spawn/oc-spawn`), so the repo can be cloned anywhere.

## opencode plugins for agterm (`opencode/`)

Three cooperating pieces that integrate opencode with the agterm
terminal (a GPU-accelerated terminal whose panes expose a message bus,
session sidebars, and a status indicator):

| File | What it does |
|---|---|
| `plugins/agterm-status.ts` | mirrors opencode session status (busy/completed/error) into agterm's session indicator via `agtermctl session status` |
| `plugins/agterm-a2a.ts` | receiver for a2a chat nudges: watches the pane's agterm mailbox, acks non-nudge mail, and wakes the opencode session bound to the chat with a "You have unread messages" prompt |
| `tui-plugin/agterm-a2a-tui.ts` | publishes which session the TUI currently displays to a route-state file, so nudges only wake a session the user is actually looking at (visibility gate) |

`src/agterm-route-state.ts` is the shared module for the route-state
channel; `tui.json` registers the TUI plugin.

### Install from zero

agterm + opencode installed, nothing else:

1. Copy the tree into the opencode config directory, preserving layout:

   ```
   opencode/plugins/*.ts    → ~/.config/opencode/plugins/
   opencode/src/            → ~/.config/opencode/src/
   opencode/tui-plugin/     → ~/.config/opencode/tui-plugin/
   opencode/tui.json        → ~/.config/opencode/tui.json
   ```

   (The server auto-discovers `plugins/*.ts`; the TUI plugin loads only via
   the `plugin` key of `tui.json`, and its path is relative to the config
   dir — keep the mirrored layout so `../src/…` imports resolve.)
2. Restart opencode. Both plugin sets load at startup.
3. `agtermctl` must be on PATH (it is, inside agterm panes).

Without agterm everything degrades to a no-op: no `AGTERM_*` environment
means the status plugin returns no hooks and the a2a receiver never
activates — safe to leave installed.

### Tuning for other harnesses

The plugins are agterm-specific, the patterns are not:

- **status mirror**: any harness that emits session status events can
  forward them to whatever indicator the host terminal/IDE exposes.
- **nudge receiver**: any channel that can deliver a small JSON payload
  to an idle agent session can replace the agterm mailbox; the receiver's
  only job is validate → ack → prompt the session.
- **visibility gate**: the route-state file (atomic tmp+rename writes,
  timestamped heartbeats) is a portable way for a UI-side watcher to
  tell a background worker what the user is looking at.

## oc-spawn — launching opencode sessions (`oc-spawn/`)

A small client/daemon pair that spawns **interactive opencode TUI
sessions** into background agterm companion panes, from any agterm pane
on this machine (host-only). One JSONL prompts file in, one spawned
session per entry. The client is stdlib-only Python and contains no
spawning logic itself; the coordinator does the work. Full usage,
protocol, and examples: `oc-spawn/README.md` and `oc-spawn/SKILL.md`.

### Running it from zero

Needs: agterm (panes + `agtermctl`), opencode, python3. No configuration
is required: both sides keep their state (replies, logs, prompt files,
coordinator address) under `~/.cache/ocspawn` — set `OCSPAWN_STATE_DIR`
on both sides to relocate it.

1. Start the coordinator **in a plain agterm pane** (not inside an
   opencode TUI pane — opencode panes auto-drain their mailbox):

   ```sh
   agtermctl session new --name ag-a2a-coordinator --no-select --cwd /tmp \
     --command /path/to/repo/oc-spawn/coordinator-start
   ```

   (absolute path — the pane starts in `--cwd /tmp` and cannot resolve
   repo-relative paths; `coordinator-start` finds the coordinator next to
   itself, so any absolute path to the checkout works)
2. Spawn from any agterm pane:

   ```sh
   oc-spawn/oc-spawn --prompts-file /tmp/prompts.jsonl --wait-reply 90
   ```

### Tuning for other harnesses

The moving parts are deliberately swappable, and the wire protocol
(request/reply JSON schemas, exit codes, per-entry errors) is fully
documented in `oc-spawn/README.md`:

- **transport**: one JSON message to the daemon + a reply file in a
  mutually visible directory — any message bus or even a socket can
  replace the agterm mailbox.
- **companion pane**: the agterm-specific part is only "a background
  terminal pane belonging to the caller" — any terminal multiplexer with
  a session API offers an equivalent.

## plans/

Plan documents for the plan-workflow skills (`plan-split` →
`subplans-orchestrate` → `subplan-implement` → `plans-archival`).
Currently empty; the skills create and consume files here.

## License

MIT — see `LICENSE`.
