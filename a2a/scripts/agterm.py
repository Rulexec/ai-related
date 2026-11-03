#!/usr/bin/env python3
"""agterm.py — optional agterm poke support for a2a_chat.

Behavioral contract, self-contained: everything here is fail-closed and
best-effort. No public function may ever raise into the chat core,
block beyond its bounded timeout, or write state for an unvalidated
name. All a2a_chat dependencies arrive via the injected `Deps`
namespace (this module imports nothing from it — the running chat
script may be a differently-installed copy, so cross-imports would bind
to the wrong instance).

Delivery mechanism: nudges travel on the agterm message bus
(`agtermctl msg send`), never by typing into the receiver's composer.
One nudge is one anonymous, versioned JSON payload addressed to the
receiver's exact `SESSION:PANE-ID`; a receiver-side plugin (opencode
TUI plugin) owns the mailbox: it reads, validates, acks, and wakes its
idle session. The bus is queue-only — delivery wakes nobody — and a
mailbox evaporates silently when its pane's program dies, so the poke
pass never assumes durability: chat-side reading positions are the
durable truth, and every send recomputes who still has undelivered
mail and re-nudges within cooldown/cap.

Sender-side identity stays gated (session-in-tree, program-not-shell,
status completed, cwd pin, chat token, pane-ambiguity exclusion): the
live pane id is resolved from the window tree and must equal the
mapping's recorded pane id — drift means stale identity, fail closed.

Failure policy in one breath: the automatic poke pass is fully
silent — runtime failures (session gone, status not idle, guards
tripped, ctl errors, budget exhausted, ambiguous pane, pane-id drift,
bus rejection), config problems (no agterm, no ctl binary, no
mappings), and mitigations (cooldown, re-poke cap, nothing pending)
all skip that receiver without a word; `wake` is the loud, exit-coded
diagnostic path. A failed or skipped poke never fails the enclosing
chat command.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import namedtuple
from datetime import datetime, timezone

# --------------------------------------------------------------------------
# Constants (all named)

CTL_ENV = "AGTERMCTL"
SESSION_ENV = "AGTERM_SESSION_ID"
PANE_ENV = "AGTERM_PANE"
WINDOW_ENV = "AGTERM_WINDOW_ID"   # every ctl call is frontmost-window-scoped
PANE_ID_ENV = "AGTERM_PANE_ID"    # recorded; cross-checked against the tree

CTL_CALL_TIMEOUT = 2.0        # per agtermctl call
POKE_BUDGET = 5.0             # total per send invocation
POKE_COOLDOWN = 10.0          # min seconds between pokes to one name
POKE_CAP = 3                  # consecutive pokes without position advance
STATUS_SYNC_WINDOW = 6 * 3600  # diagnostic classification only, never gated

# The nudge payload contract with the receiver plugin. The plugin
# filters its pane's shared mailbox by `type`, validates the payload's
# chat_token against chat_dir's chat.json (accident hygiene — the
# sender gates on the same token; any local bus sender could mint one,
# so this is re-creation/staleness detection, not authentication),
# acks, and prompts its idle session. `v` gates forward compatibility:
# an unknown type or future v is ignored (and still acked — the plugin
# is its pane's sole reader).
NUDGE_TYPE = "a2a-nudge"
NUDGE_VERSION = 1

# Injected chat-engine callables: the *same function objects* the running
# a2a_chat module uses (pending is computed by the delivery engine's own
# deliverable_to, never a reimplementation). atomic_write is
# atomic_write_json; chat_token reads chat.json's "created" (None-safe).
Deps = namedtuple("Deps",
                  "deliverable_to scan_messages load_seen atomic_write chat_token")

STATE_DIR = "state"


# --------------------------------------------------------------------------
# Control binary

def resolve_ctl():
    """$AGTERMCTL if it points at an executable file, else agtermctl on
    PATH, else None (caller returns silently — config, no warning)."""
    candidate = os.environ.get(CTL_ENV)
    if candidate:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
        return None
    return shutil.which("agtermctl")


def run_ctl(ctl, args, timeout, stdin=None):
    """Run agtermctl; never raises. Returns (ok, stdout, stderr).

    Children never inherit the parent's stdin: the record hook runs
    BEFORE `send --file -` reads its body, and an inherited pipe got
    drained by the ctl subprocess chain in the wild (a piped ack landed
    headers-only). With explicit input the child's stdin is the input
    pipe; otherwise DEVNULL."""
    try:
        kwargs = {}
        if stdin is not None:
            kwargs["input"] = stdin
        else:
            kwargs["stdin"] = subprocess.DEVNULL
        proc = subprocess.run(
            [ctl, *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=max(0.05, timeout), **kwargs)
        return proc.returncode == 0, proc.stdout or "", proc.stderr or ""
    except Exception:
        return False, "", "agtermctl call failed"


# --------------------------------------------------------------------------
# Tree parsing (deployed schema, live-pinned; see the agtermctl reference)

def _collect_sessions(node, out):
    """Recursively collect session dicts (have an "id" and any of
    foreground/status/cwd). The exact envelope around the session list
    is not load-bearing — per-window payloads nest differently — so the
    parser finds session nodes wherever they sit. Nothing found → the
    gate fails closed on every receiver."""
    if isinstance(node, dict):
        if isinstance(node.get("id"), str) and (
                "foreground" in node or "status" in node or "cwd" in node
                or "surfaces" in node):
            out[node["id"]] = node
        for value in node.values():
            _collect_sessions(value, out)
    elif isinstance(node, list):
        for item in node:
            _collect_sessions(item, out)


def parse_tree(stdout):
    sessions = {}
    try:
        _collect_sessions(json.loads(stdout), sessions)
    except Exception:
        return {}
    return sessions


def surface_pane_id(node, pane):
    """The bus pane id for `pane` ("left"/"right") from a session's
    tree node: surfaces[].paneID where kind == pane — the same UUID as
    the PANE-ID half of a bus address. None when the surface is
    absent, not yet created (split hidden since launch), an overlay,
    or malformed: omitted paneID is agterm's own "not addressable"
    mark, and the caller fails closed."""
    surfaces = node.get("surfaces") if isinstance(node, dict) else None
    if not isinstance(surfaces, list):
        return None
    for surface in surfaces:
        if (isinstance(surface, dict) and surface.get("kind") == pane
                and isinstance(surface.get("paneID"), str)
                and surface["paneID"]):
            return surface["paneID"]
    return None


def _parse_status_age(value):
    """Age in seconds of a statusChangedAt stamp, or None. The deployed
    schema emits epoch-seconds floats (live-verified); ISO-8601
    strings are accepted too."""
    try:
        if isinstance(value, (int, float)):
            age = time.time() - float(value)
        elif isinstance(value, str) and value.strip():
            text = value.strip()
            if text.endswith(("Z", "z")):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age = time.time() - dt.timestamp()
        else:
            return None
        return max(0.0, age)
    except Exception:
        return None


# --------------------------------------------------------------------------
# Mapping

def mapping_path(chat_dir, name):
    return os.path.join(chat_dir, STATE_DIR, f"{name}.agterm.json")


def record_mapping(chat_dir, name, deps):
    """Record this agent's agterm identity at command start. Never
    raises, never fatal; caller has already validated name (NAME_RE)."""
    try:
        session_id = os.environ.get(SESSION_ENV)
        if not session_id:
            return
        pane_raw = os.environ.get(PANE_ENV)
        if pane_raw is None:
            pane = "left"
        else:
            pane = pane_raw if pane_raw in ("left", "right") else None
        window_id = os.environ.get(WINDOW_ENV) or None
        pane_id = os.environ.get(PANE_ID_ENV) or None

        # One guarded tree call captures the pane-scoped cwd plus the
        # frozen diagnostics pair. Any failure → fields absent, never
        # fatal (worst case +CTL_CALL_TIMEOUT on a wedged socket).
        # Failures here leave the mapping without a cwd pin —
        # pokes to this name fail closed until the next refresh.
        cwd = None
        expected_foreground = None
        status_sync = None
        status_age_s = None
        ctl = resolve_ctl()
        if ctl and window_id:
            ok, out, _ = run_ctl(ctl, ["tree", "--json", "--window", window_id],
                                 CTL_CALL_TIMEOUT)
            if ok:
                node = parse_tree(out).get(session_id)
                if node is not None:
                    cwd_field = "splitCwd" if pane == "right" else "cwd"
                    raw_cwd = node.get(cwd_field)
                    cwd = raw_cwd if isinstance(raw_cwd, str) and raw_cwd else None
                    fg_field = "splitForeground" if pane == "right" else "foreground"
                    fg = node.get(fg_field)
                    if isinstance(fg, list) and fg and isinstance(fg[0], str):
                        expected_foreground = os.path.basename(fg[0])
                    age = _parse_status_age(node.get("statusChangedAt"))
                    if age is not None:
                        status_age_s = round(age, 1)
                        status_sync = age <= STATUS_SYNC_WINDOW

        try:
            chat_token = deps.chat_token(chat_dir)
        except Exception:
            chat_token = None

        now = datetime.now(timezone.utc)
        mapping = {
            "session_id": session_id,
            "window_id": window_id,
            "pane": pane,
            "pane_id": pane_id,
            "cwd": cwd,
            # Frozen record-time diagnostics, never recomputed, never
            # gated: the human investigating a missed poke reads these.
            "expected_foreground": expected_foreground,
            "status_sync": status_sync,
            "status_age_s": status_age_s,
            "chat_token": chat_token,
            # The receiver's own world: the nudge payload references
            # these, and the plugin validates against its own mapping.
            "chat_dir": os.path.abspath(chat_dir),
            # The skill root (parent of scripts/) — the plugin renders
            # the read command from {a2a_dir}/scripts/a2a_chat.py.
            "a2a_dir": os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "updated": now.strftime("%Y-%m-%dT%H:%M:%S.")
                       + f"{now.microsecond // 1000:03d}Z",
        }
        state = os.path.join(chat_dir, STATE_DIR)
        os.makedirs(state, exist_ok=True)
        deps.atomic_write(state, f"{name}.agterm.json", mapping)
    except Exception:
        pass


def load_mappings(chat_dir):
    """name → mapping for every state/*.agterm.json; corrupt entries
    are skipped silently (a bad mapping must never break the sender)."""
    mappings = {}
    state = os.path.join(chat_dir, STATE_DIR)
    try:
        entries = os.listdir(state)
    except OSError:
        return mappings
    for entry in entries:
        m = re.fullmatch(r"(.+)\.agterm\.json", entry)
        if not m:
            continue
        try:
            with open(os.path.join(state, entry), encoding="utf-8") as f:
                mapping = json.load(f)
            if isinstance(mapping, dict):
                mappings[m.group(1)] = mapping
        except Exception:
            continue
    return mappings


def ambiguous_panes(mappings):
    """names whose (session_id, pane) is shared with another mapping —
    the displayed inner window may host either agent, so eligibility of
    the twin is irrelevant; both refuse, fail-closed.
    Evaluated over ALL recorded mappings, the sender's included: two
    agents in one pane is ambiguous no matter who is talking."""
    seen = {}
    for name, mapping in sorted(mappings.items()):
        key = (mapping.get("session_id"), mapping.get("pane"))
        if key[0] is None or key[1] is None:
            continue
        seen.setdefault(key, []).append(name)
    ambiguous = set()
    for names in seen.values():
        if len(names) > 1:
            ambiguous.update(names)
    return ambiguous


# --------------------------------------------------------------------------
# Gate — every condition must pass, else (ok=False, reason)

def gate(tree_sessions, mapping, own_chat_token):
    window_id = mapping.get("window_id")
    if not window_id:
        return False, "no window in mapping"
    pane = mapping.get("pane")
    if pane not in ("left", "right"):
        return False, "no pane in mapping"
    session_id = mapping.get("session_id")
    node = tree_sessions.get(session_id) if session_id else None
    if node is None:
        return False, "session not in tree"

    # 2. Program-not-shell (owner-relaxed: no argv parsing). The
    # basename == opencode check was demoted, not disproven; identity
    # lives in the pane-id cross-check.
    fg_field = "splitForeground" if pane == "right" else "foreground"
    fg = node.get(fg_field)
    if not (isinstance(fg, list) and fg):
        return False, "not a program pane"

    # 3. status == "completed" exactly; key-absent never pokes. The
    # receiver plugin also idle-gates on its side — this is the cheap
    # sender-side half that keeps mail out of busy sessions.
    if node.get("status") != "completed":
        return False, f"status {node.get('status') if 'status' in node else 'absent'}"

    # 4. pane-scoped cwd pin.
    recorded_cwd = mapping.get("cwd")
    if not recorded_cwd:
        return False, "no cwd pin"
    cwd_field = "splitCwd" if pane == "right" else "cwd"
    if node.get(cwd_field) != recorded_cwd:
        return False, "cwd mismatch"

    # 5. chat-identity token: accident hygiene (cross-chat state
    # confusion), not insider protection.
    if not own_chat_token or mapping.get("chat_token") != own_chat_token:
        return False, "chat token mismatch"

    return True, ""


# --------------------------------------------------------------------------
# Nudge payload and bus delivery

def render_nudge(mapping, name):
    """The nudge payload — one compact JSON line, the entire sender
    side of the receiver contract: {type, v, chat_dir, a2a_dir, name,
    chat_token}. Refuses (None) when a recorded receiver path or the
    chat token is absent: a corrupt or forged mapping must fail closed,
    never ship a payload the plugin would render against missing paths
    (the gate refuses token-less mappings first, so this is the belt to
    that braces)."""
    chat_dir = mapping.get("chat_dir")
    a2a_dir = mapping.get("a2a_dir")
    chat_token = mapping.get("chat_token")
    if not chat_dir or not a2a_dir or not chat_token or not name:
        return None
    return json.dumps({
        "type": NUDGE_TYPE,
        "v": NUDGE_VERSION,
        "chat_dir": str(chat_dir),
        "a2a_dir": str(a2a_dir),
        "name": str(name),
        "chat_token": str(chat_token),
    }, separators=(",", ":"), sort_keys=True)


def reply_lists_target(entries, target):
    """True when the send reply's delivered/rejected list names our
    target; matching on the pane-id half absorbs canonical-address
    spelling differences."""
    if not isinstance(entries, list):
        return False
    pane_half = target.split(":", 1)[1] if ":" in target else target
    for entry in entries:
        if isinstance(entry, str) and (
                entry == target or entry.endswith(":" + pane_half)):
            return True
    return False


def deliver_nudge(ctl, mapping, name, node, timeout):
    """One nudge attempt over the bus. Returns (ok, reason); ok=True
    iff the bus accepted the payload for the receiver's mailbox —
    which is also the moment the caller records the poke. Counting is
    reply-first, exit-code-second (bus semantics: a nonzero exit only
    means *something* was rejected — a partial send still queues the
    listed recipients' copies, so exit-1-with-our-recipient-delivered
    is a counted poke; only transport-level failures fail closed).
    Also fail-closed on: unaddressable surface (no paneID), stale
    identity (tree pane id != recorded pane id — a re-split or
    respawn; the mailbox is gone anyway), missing receiver paths,
    explicit rejection (unknown pane / mailbox full)."""
    pane = mapping.get("pane")
    pane_id = surface_pane_id(node, pane)
    if not pane_id:
        return False, f"no paneID for surface {pane!r} in tree"
    recorded = mapping.get("pane_id")
    if not isinstance(recorded, str) or not recorded:
        return False, "no pane id in mapping"
    if pane_id != recorded:
        return False, f"pane id drift (tree {pane_id}, mapping {recorded})"
    payload = render_nudge(mapping, name)
    if payload is None:
        return False, "no receiver paths in mapping"
    target = f"{mapping.get('session_id')}:{pane_id}"
    ok, out, err = run_ctl(ctl, ["msg", "send", "--stdin", "--to", target],
                           timeout, stdin=payload)
    try:
        reply = json.loads(out)
    except Exception:
        reply = None
    if isinstance(reply, dict):
        if reply_lists_target(reply.get("delivered"), target):
            return True, ""
        if reply_lists_target(reply.get("rejected"), target):
            return False, f"bus rejected {target}"
    # Our target is in neither list (single-recipient sends land here
    # only on odd replies) or the reply is unparsable: exit 0 means
    # the queue accepted it (rejected panes are named, not implied);
    # nonzero is the transport-level fail-closed skip.
    if ok:
        return True, ""
    return False, f"agtermctl failed: {err.strip() or 'unknown'}"


# --------------------------------------------------------------------------
# Cooldown store

def poked_path(chat_dir, name):
    return os.path.join(chat_dir, STATE_DIR, f"{name}.poked.json")


def _load_poked(chat_dir, name):
    try:
        with open(poked_path(chat_dir, name), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def poke_allowed(chat_dir, name, deps, now=None):
    """(allowed, state): cooldown, then cap with the monotonic
    seen-count re-arm proxy (growth via --ignore also re-arms —
    documented, accepted)."""
    now = time.time() if now is None else now
    state = _load_poked(chat_dir, name)
    last_poke = state.get("last_poke")
    if isinstance(last_poke, (int, float)) and now - last_poke < POKE_COOLDOWN:
        return False, state
    consecutive = state.get("consecutive", 0)
    if isinstance(consecutive, int) and consecutive >= POKE_CAP:
        try:
            seen_count = len(deps.load_seen(chat_dir, name))
        except Exception:
            seen_count = 0
        if seen_count <= state.get("last_seen_count", 0):
            return False, state
    return True, state


def record_poke(chat_dir, name, deps, now=None):
    """After a nudge the bus accepted only: timestamp + delivery
    counter. The counter tracks *consecutive pokes without reader
    progress* — when the seen position advanced since the previous
    poke, the cap resets. The reading position is the durable truth
    the bus cannot provide (mailboxes evaporate silently), so re-arm
    never trusts `delivered` alone. Last-writer-wins between
    concurrent pokers — accepted."""
    now = time.time() if now is None else now
    try:
        previous = _load_poked(chat_dir, name)
        try:
            seen_count = len(deps.load_seen(chat_dir, name))
        except Exception:
            seen_count = 0
        prev_seen = previous.get("last_seen_count")
        progressed = (isinstance(prev_seen, (int, float))
                      and seen_count > prev_seen)
        state = {
            "last_poke": now,
            "last_seen_count": seen_count,
            "consecutive": 1 if progressed else int(
                previous.get("consecutive", 0)) + 1,
        }
        state_dir = os.path.join(chat_dir, STATE_DIR)
        os.makedirs(state_dir, exist_ok=True)
        deps.atomic_write(state_dir, f"{name}.poked.json", state)
    except Exception:
        pass


# --------------------------------------------------------------------------
# The poke pass

def after_send(chat_dir, sender, deps):
    """Nudge eligible receivers after a send's messages are committed.
    Never raises, never blocks beyond POKE_BUDGET, never fails the send."""
    try:
        _after_send(chat_dir, sender, deps)
    except Exception:
        pass


def _remaining(deadline):
    return deadline - time.monotonic()


def _after_send(chat_dir, sender, deps):
    mappings = load_mappings(chat_dir)
    if not mappings:
        return  # agterm-free chat: zero subprocesses, zero writes

    # Ambiguity detection over ALL recorded mappings, the sender's own
    # included (two agents in one pane is ambiguous no matter who is
    # talking). Ambiguous names are excluded fail-closed, silently.
    ambiguous = ambiguous_panes(mappings)

    msgs = deps.scan_messages(chat_dir)
    pending = []
    for name in sorted(mappings):
        if name == sender:
            continue  # the sender is excluded unconditionally
        try:
            seen = deps.load_seen(chat_dir, name)
            if deps.deliverable_to(msgs, name, seen):
                pending.append(name)
        except Exception:
            continue
    if not pending:
        return

    eligible = [name for name in pending if name not in ambiguous]
    if not eligible:
        return

    checked = []
    for name in eligible:
        allowed, _ = poke_allowed(chat_dir, name, deps)
        if allowed:
            checked.append(name)
    if not checked:
        return  # silent: cooldown/cap are mitigations, not failures

    ctl = resolve_ctl()
    if ctl is None:
        return  # silent: config problems are never agent-facing

    try:
        own_token = deps.chat_token(chat_dir)
    except Exception:
        own_token = None

    deadline = time.monotonic() + POKE_BUDGET
    window_trees = {}

    i = 0
    while i < len(checked):
        name = checked[i]
        if _remaining(deadline) < CTL_CALL_TIMEOUT:
            return  # budget exhausted, silent
        mapping = mappings[name]
        window_id = mapping.get("window_id")

        if window_id not in window_trees:
            if _remaining(deadline) < CTL_CALL_TIMEOUT:
                return  # budget exhausted, silent
            ok, out, _ = run_ctl(
                ctl, ["tree", "--json", "--window", str(window_id)],
                min(CTL_CALL_TIMEOUT, max(0.0, _remaining(deadline))))
            # A failed call (e.g. window closed) records an empty tree:
            # every receiver in this window fails the gate below,
            # fail-closed, silently.
            window_trees[window_id] = parse_tree(out) if ok else {}

        sessions = window_trees.get(window_id, {})
        ok, _ = gate(sessions, mapping, own_token)
        if not ok:
            i += 1
            continue

        timeout = min(CTL_CALL_TIMEOUT, max(0.0, _remaining(deadline)))
        node = sessions.get(mapping.get("session_id"))
        delivered, _ = deliver_nudge(ctl, mapping, name, node, timeout)
        if delivered:
            record_poke(chat_dir, name, deps)
        i += 1


# --------------------------------------------------------------------------
# wake — loud by design; explicit human intent gets explicit answers

def wake(chat_dir, name, force, deps):
    """Returns the exit code (0 poked / 3 nothing pending / 5 refused).
    Cooldown and cap are not consulted, but the record is written; the
    full gate always applies."""
    mappings = load_mappings(chat_dir)
    mapping = mappings.get(name)
    if mapping is None:
        print(f"error: no agterm mapping for {name}", file=sys.stderr)
        return 5

    ambiguous = ambiguous_panes(mappings)
    if name in ambiguous:
        others = sorted(n for n in ambiguous if n != name)
        print(f"error: ambiguous pane: also mapped by {', '.join(others)}",
              file=sys.stderr)
        return 5

    msgs = deps.scan_messages(chat_dir)
    try:
        seen = deps.load_seen(chat_dir, name)
    except Exception:
        seen = set()
    if not force and not deps.deliverable_to(msgs, name, seen):
        print(f"nothing pending for {name} (use --force to poke anyway)",
              file=sys.stderr)
        return 3

    ctl = resolve_ctl()
    if ctl is None:
        print("error: agtermctl not found (set AGTERMCTL or PATH)",
              file=sys.stderr)
        return 5

    try:
        own_token = deps.chat_token(chat_dir)
    except Exception:
        own_token = None

    window_id = mapping.get("window_id")
    if _status_diagnostic(mapping):
        age = mapping.get("status_age_s")
        hours = f"~{int(age // 3600)}h" if isinstance(age, (int, float)) else "unknown"
        print(f"note: status age {hours} at last interaction "
              f"(plugin sync unverified)", file=sys.stderr)

    ok, out, _ = run_ctl(ctl, ["tree", "--json", "--window", str(window_id)],
                         CTL_CALL_TIMEOUT) if window_id else (False, "", "")
    sessions = parse_tree(out) if ok else {}
    ok, reason = gate(sessions, mapping, own_token)
    if not ok:
        print(f"error: poke refused: {reason}", file=sys.stderr)
        return 5

    node = sessions.get(mapping.get("session_id"))
    delivered, reason = deliver_nudge(ctl, mapping, name, node,
                                      CTL_CALL_TIMEOUT)
    if not delivered:
        print(f"error: poke refused: {reason}", file=sys.stderr)
        return 5

    record_poke(chat_dir, name, deps)  # wake counts toward the counter
    print(f"poked {name}")
    return 0


def _status_diagnostic(mapping):
    return mapping.get("status_sync") is False


# --------------------------------------------------------------------------
# unlink — remove one name's mapping; the human-facing poke off switch

def unlink(chat_dir, name):
    """Erase name's agterm session mapping. The automatic poke pass and
    `wake` then see no mapping for it: pokes to that name stop until
    the participant itself uses the chat again (record_mapping re-links
    it on its next send/wait/read — nothing to undo by hand). Chat
    membership is untouched: the reading position and messages stay.

    Loud and exit-coded like wake (explicit intent gets explicit
    answers): 0 unlinked / 3 no mapping (nothing to do, benign — the
    tool-wide meaning of 3) / 5 removal failed. Records no mapping,
    consults no gate, spawns no ctl call."""
    path = mapping_path(chat_dir, name)
    try:
        os.unlink(path)
    except FileNotFoundError:
        print(f"no agterm mapping for {name} (nothing to unlink)",
              file=sys.stderr)
        return 3
    except OSError as e:
        print(f"error: cannot unlink {name}: {e}", file=sys.stderr)
        return 5
    print(f"unlinked {name} (pokes stop until it uses the chat again)")
    return 0
