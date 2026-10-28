#!/usr/bin/env python3
"""a2a_chat.py — agent-to-agent broadcast chat over a shared directory.

Design: lock-free. Concurrency correctness relies on unique filenames and
atomic rename(2) only — no flock, no daemons, no locks. Safe for processes
on one machine sharing a local directory, including directories that are
bind-mounted into containers (VirtioFS-safe).

Python >= 3.9, stdlib only.

Commands:
    init    --dir Y [--topic TEXT]        create a chat in directory Y
    claim   --dir Y --name Q               take a name: create its state
                                           file before first read (exit 5
                                           if the name is taken: position
                                           exists or it has spoken)
    send    --dir Y --name Q --file F [--file F ...] [--to NAME]
                                           [--wait [S]]
                                            commit messages ('-' = stdin,
                                            single); --to makes them direct
                                            (delivered only to NAME); with
                                            --wait also wait
                                            for answers, skipping your own
    wait    --dir Y --name Q [--timeout S] [--ignore ID [ID ...]]
                                           [--ignore-own-through ID]
                                           block until new messages
                                           (default 300s); --ignore marks
                                           messages as read without
                                           printing; --ignore-own-through
                                           skips all your messages up to
                                           and including ID
    read    --dir Y --name Q [--all]      deliver new messages now / print history
    status  --dir Y                       chat summary (senders + known readers)

Exit codes:
    0  success   2  usage error   3  nothing new (wait timeout / empty read)
    4  not a chat dir   5  conflict / runtime error
"""

import argparse
import json
import os
import re
import sys
import time
import uuid
from collections import namedtuple
from datetime import datetime, timezone
from typing import NoReturn

# --------------------------------------------------------------------------
# Constants

EXIT_OK = 0
EXIT_USAGE = 2      # bad arguments, invalid name, unreadable body file
EXIT_TIMEOUT = 3    # wait/read found no new messages before the deadline
EXIT_NO_CHAT = 4    # directory is not an initialized chat
EXIT_ERROR = 5      # conflict (init on existing chat) or runtime error

CHAT_FILE = "chat.json"
MESSAGES_DIR = "messages"
STATE_DIR = "state"

POLL_INTERVAL = 0.5  # seconds between directory scans while waiting

# Burst coalescing (wait / send --wait only): once pending messages are
# found, keep extending the delivery window while new ones keep arriving,
# so an in-flight burst lands as a single delivery. COALESCE_QUIET is the
# silence that must pass for the burst to count as finished; COALESCE_MAX
# bounds the total extension so a firehose sender cannot stall delivery.
COALESCE_QUIET = POLL_INTERVAL
COALESCE_MAX = 5.0

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
MSG_RE = re.compile(r"^([A-Za-z0-9_-]{1,32})-(\d{6})-([0-9a-f]{4})\.md$")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}-\d{6}-[0-9a-f]{4}$")

MAX_SEQ = 999999

Msg = namedtuple("Msg", "id sender seq time time_ms path body to",
                 defaults=(None,))  # 'to' set only for direct messages

_warned = set()  # rate-limit repeat warnings across polling scans


# --------------------------------------------------------------------------
# Small helpers

def die(code, message) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(code)


def warn_once(message):
    if message not in _warned:
        _warned.add(message)
        print(f"warning: {message}", file=sys.stderr)


def valid_name(value):
    if not NAME_RE.match(value):
        raise argparse.ArgumentTypeError(
            f"invalid name {value!r}: must match {NAME_RE.pattern}")
    return value


def valid_message_id(value):
    if not ID_RE.match(value):
        raise argparse.ArgumentTypeError(
            f"invalid message id {value!r}: expected <name>-<6 digits>-"
            f"<4 hex>, e.g. Q-000001-a1f2")
    return value


def now_iso():
    """UTC ISO-8601 timestamp with millisecond precision."""
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_iso_ms(value):
    """Parse an ISO-8601 timestamp to epoch milliseconds; None if invalid.

    Lenient: accepts 'Z' or explicit offsets; naive values are treated as UTC.
    """
    try:
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, OverflowError):
        return None


def atomic_write_bytes(directory, final_name, data):
    """Write data to directory/final_name atomically: tmp file, fsync, rename.

    The tmp name starts with '.' so concurrent scanners never mistake it for
    a committed file. No locks are used or needed.
    """
    tmp = os.path.join(directory, f".tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, os.path.join(directory, final_name))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # Best-effort directory fsync (not supported everywhere; that is fine).
    try:
        dfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def atomic_write_json(directory, final_name, obj):
    payload = json.dumps(obj, indent=1, ensure_ascii=False) + "\n"
    atomic_write_bytes(directory, final_name, payload.encode("utf-8"))


def exclusive_write_json(directory, final_name, obj):
    """Create directory/final_name; raise FileExistsError if it exists.

    Unlike atomic_write_bytes there is no tmp+rename: for a fixed shared
    name (the chat.json sentinel) the create itself must be the
    synchronization point, so two racing inits cannot both succeed. If the
    write fails midway the half-written file is unlinked again — a crashed
    init must not leave a corrupt sentinel that makes the directory look
    like an initialized chat.
    """
    path = os.path.join(directory, final_name)
    payload = (json.dumps(obj, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def require_chat(chat_dir):
    """Exit with EXIT_NO_CHAT unless chat_dir looks like an initialized chat."""
    if not os.path.isfile(os.path.join(chat_dir, CHAT_FILE)):
        die(EXIT_NO_CHAT, f"not an initialized chat directory: {chat_dir}")


def read_chat_meta(chat_dir):
    path = os.path.join(chat_dir, CHAT_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError) as e:
        die(EXIT_ERROR, f"cannot read {CHAT_FILE}: {e}")
    if not isinstance(meta, dict):
        die(EXIT_ERROR, f"{CHAT_FILE} does not contain a JSON object")
    return meta


# --------------------------------------------------------------------------
# Message primitives

def parse_message(path, fname):
    """Parse one message file. Returns Msg or None if unparseable.

    sender/seq/id come from the filename (the pattern guarantees their form);
    the header must supply a parsable 'time'. An optional 'to:' header makes
    the message a direct message to that name. The body is everything after
    the first blank line.
    """
    m = MSG_RE.match(fname)
    if not m:
        return None
    sender, seq_s, _ = m.groups()
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except (OSError, UnicodeDecodeError):
        return None
    head, sep, body = raw.partition("\n\n")
    if not sep:
        return None
    time_str = None
    to = None
    for line in head.splitlines():
        key, _, value = line.partition(":")
        if key.strip().lower() == "time":
            time_str = value.strip()
        elif key.strip().lower() == "to":
            to = value.strip()
    if not time_str:
        return None
    time_ms = parse_iso_ms(time_str)
    if time_ms is None:
        return None
    if to is not None and not NAME_RE.match(to):
        # Deliverable names always match NAME_RE (readers are validated),
        # so this DM can never be delivered to anyone. Keep it parsed
        # (read --all still shows it) but say so once.
        warn_once(f"direct message 'to:' value {to!r} is not a valid "
                  f"name; it will not be delivered: {path}")
    return Msg(id=fname[:-len(".md")], sender=sender, seq=int(seq_s),
               time=time_str, time_ms=time_ms, path=path, body=body, to=to)


def scan_messages(chat_dir):
    """All parseable messages in the chat, in total order.

    Total order key: (time_ms, sender, seq, id) — computed from file
    contents only, never filesystem mtimes, so every reader derives the
    identical order. Files that do not match the message filename pattern
    (including all '.tmp-*' files) are ignored; files matching the pattern
    but unparseable are skipped with a one-time warning and are NOT marked
    seen, so they get delivered once fixed by hand.
    """
    msgs_dir = os.path.join(chat_dir, MESSAGES_DIR)
    result = []
    for fname in os.listdir(msgs_dir):
        if not MSG_RE.match(fname):
            continue
        msg = parse_message(os.path.join(msgs_dir, fname), fname)
        if msg is None:
            warn_once(f"skipping unparseable message file: "
                      f"{os.path.join(chat_dir, MESSAGES_DIR, fname)}")
            continue
        result.append(msg)
    result.sort(key=lambda m: (m.time_ms, m.sender, m.seq, m.id))
    return result


def next_seq_for(msgs_dir, name):
    """Lock-free per-sender sequence: scan the directory, take max + 1.

    If two processes send under the same name simultaneously they may pick
    the same seq; the unique hash in the filename keeps both messages safe
    (documented, accepted behavior).
    """
    seq = 0
    for fname in os.listdir(msgs_dir):
        m = MSG_RE.match(fname)
        if m and m.group(1) == name:
            seq = max(seq, int(m.group(2)))
    return seq + 1


def sender_message_count(chat_dir, name):
    """How many messages `name` has sent (filename-authoritative)."""
    msgs_dir = os.path.join(chat_dir, MESSAGES_DIR)
    count = 0
    for fname in os.listdir(msgs_dir):
        m = MSG_RE.match(fname)
        if m and m.group(1) == name:
            count += 1
    return count


def send_message(chat_dir, name, body, to=None):
    """Commit one message; returns its id. Raises ValueError on bad input.

    With `to` (a valid name) the message is a direct message: stored in the
    same messages/ dir and same per-sender sequence, but delivered only to
    that name. Delivery privacy is by filter, not by hiding: the file is
    world-readable and shows in read --all / status tallies.
    """
    if not NAME_RE.match(name):
        raise ValueError(f"invalid name: {name!r}")
    if to is not None and not NAME_RE.match(to):
        raise ValueError(f"invalid --to name: {to!r}")
    msgs_dir = os.path.join(chat_dir, MESSAGES_DIR)
    seq = next_seq_for(msgs_dir, name)
    if seq > MAX_SEQ:
        raise ValueError(f"per-sender sequence overflow for {name}")
    final_name = f"{name}-{seq:06d}-{uuid.uuid4().hex[:4]}.md"
    mid = final_name[:-len(".md")]
    if body and not body.endswith("\n"):
        body += "\n"
    to_line = f"to: {to}\n" if to else ""
    content = (f"from: {name}\n{to_line}seq: {seq}\n"
               f"time: {now_iso()}\nid: {mid}\n\n{body}")
    atomic_write_bytes(msgs_dir, final_name, content.encode("utf-8"))
    return mid


# --------------------------------------------------------------------------
# Delivery state

def seen_path(chat_dir, name):
    return os.path.join(chat_dir, STATE_DIR, f"{name}.seen.json")


def load_seen(chat_dir, name):
    """Set of delivered message ids for name; empty set if no state yet."""
    try:
        with open(seen_path(chat_dir, name), encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("delivered", []))
    except (OSError, ValueError):
        return set()


def save_seen_union(chat_dir, name, new_ids):
    """Persist seen = old union new, atomically.

    The union-merge makes writes monotonic: two processes acting under one
    name can cause a duplicate delivery print but never a lost entry.
    """
    merged = load_seen(chat_dir, name) | set(new_ids)
    os.makedirs(os.path.join(chat_dir, STATE_DIR), exist_ok=True)
    atomic_write_json(os.path.join(chat_dir, STATE_DIR),
                      f"{name}.seen.json", {"delivered": sorted(merged)})
    return merged


# --------------------------------------------------------------------------
# Delivery engine

def print_message(msg, reader=None):
    """One message in the delivery format. DMs are marked in the banner:
    'to you' when delivered to the addressee, 'to <name>' otherwise
    (read --all). The marker sits after 'seq' so the 'from X | seq N'
    prefix format is unchanged for broadcasts."""
    to_part = ""
    if msg.to is not None:
        target = "you" if msg.to == reader else msg.to
        to_part = f" | to {target}"
    print(f"=== msg {msg.id} | from {msg.sender} | seq {msg.seq}"
          f"{to_part} | {msg.time} ===")
    if msg.body:
        sys.stdout.write(msg.body)
        if not msg.body.endswith("\n"):
            sys.stdout.write("\n")
    print(f"=== end {msg.id} ===")


def deliverable_to(msgs, name, seen):
    """Pending messages for reader `name`: broadcasts plus DMs addressed
    to that name, minus what it has already seen. DMs to other names are
    never pending for anyone but their addressee."""
    return [m for m in msgs
            if m.id not in seen and m.to in (None, name)]


def deliver_messages(chat_dir, name, timeout, coalesce=False):
    """Print all messages not yet delivered to name, then advance state.

    With no pending messages, re-scan every POLL_INTERVAL seconds until the
    timeout (seconds, float) expires. Returns the number of messages
    delivered (0 = timed out with nothing new).

    With coalesce=True (wait / send --wait), a non-empty pending set is not
    printed immediately: coalesce_burst() lets an in-flight burst stabilize
    first. read uses coalesce=False and delivers instantly.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        require_chat(chat_dir)  # abort fast if the chat dir vanished
        seen = load_seen(chat_dir, name)
        pending = deliverable_to(scan_messages(chat_dir), name, seen)
        if pending:
            if coalesce:
                pending = coalesce_burst(chat_dir, name, pending)
            for msg in pending:
                print_message(msg, reader=name)
            save_seen_union(chat_dir, name, {m.id for m in pending})
            return len(pending)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 0
        time.sleep(min(POLL_INTERVAL, remaining))


def coalesce_burst(chat_dir, name, pending):
    """Let an in-flight burst stabilize; returns the settled pending list.

    Sleeps one quiet window (COALESCE_QUIET) and re-scans; if new pending
    messages appeared meanwhile the window is extended — but the total
    extension never exceeds COALESCE_MAX, so continuous senders cannot
    stall delivery forever. A shrinking pending set (a peer process under
    the same name delivered some messages) counts as stable.
    """
    cap = time.monotonic() + COALESCE_MAX
    while True:
        quiet_end = time.monotonic() + COALESCE_QUIET
        time.sleep(max(0.0, min(quiet_end, cap) - time.monotonic()))
        seen = load_seen(chat_dir, name)
        current = deliverable_to(scan_messages(chat_dir), name, seen)
        stable = len(current) <= len(pending)
        capped = time.monotonic() >= cap
        if stable or capped:
            return current
        pending = current


# --------------------------------------------------------------------------
# Commands

def init_chat(chat_dir, topic=None):
    """Create the chat structure; raises ValueError on conflicts."""
    if os.path.isfile(chat_dir):
        raise ValueError(f"path exists and is a file: {chat_dir}")
    if os.path.isfile(os.path.join(chat_dir, CHAT_FILE)):
        raise ValueError(f"chat already initialized: {chat_dir}")
    os.makedirs(os.path.join(chat_dir, MESSAGES_DIR), exist_ok=True)
    os.makedirs(os.path.join(chat_dir, STATE_DIR), exist_ok=True)
    try:
        exclusive_write_json(chat_dir, CHAT_FILE,
                             {"version": 1, "topic": topic, "created": now_iso()})
    except FileExistsError:
        # Lost the init race: another process created the sentinel first.
        raise ValueError(f"chat already initialized: {chat_dir}")


def cmd_init(args):
    try:
        init_chat(args.dir, args.topic)
    except ValueError as e:
        die(EXIT_ERROR, str(e))
    except OSError as e:
        die(EXIT_ERROR, str(e))
    print(f"initialized chat: {args.dir}")
    return EXIT_OK


def read_body_file(path):
    """Body text of one --file argument; dies with EXIT_USAGE if unreadable
    or not valid UTF-8."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        die(EXIT_USAGE, f"cannot read message file: {e}")
    except UnicodeDecodeError as e:
        die(EXIT_USAGE, f"cannot read message file: not valid UTF-8: {e}")


def resolve_body_path(chat_dir, path):
    """Resolve one --file argument. '-' and absolute paths pass through;
    relative paths resolve against the chat directory — the one path
    anchor every participant reliably knows (agents' cwds are arbitrary,
    and containers share exactly the chat dir)."""
    if path == "-" or os.path.isabs(path):
        return path
    return os.path.join(chat_dir, path)


def cmd_send(args):
    require_chat(args.dir)
    files = list(args.file)
    # Input phase (all-or-nothing): resolve and read every body before
    # committing anything, so one bad file leaves zero messages behind —
    # and the commits below run back-to-back for a tight batch window.
    if "-" in files:
        if len(files) > 1:
            die(EXIT_USAGE, "--file -: stdin cannot be mixed with other "
                            "files or repeated")
        try:
            bodies = [sys.stdin.read()]
        except UnicodeDecodeError as e:
            die(EXIT_USAGE, f"cannot read message from stdin: "
                            f"not valid UTF-8: {e}")
    else:
        paths = [resolve_body_path(args.dir, f) for f in files]
        bodies = [read_body_file(path) for path in paths]
    # Commit phase: back-to-back.
    mids = []
    try:
        for body in bodies:
            mids.append(send_message(args.dir, args.name, body, to=args.to))
    except ValueError as e:
        die(EXIT_USAGE, str(e))
    except OSError as e:
        die(EXIT_ERROR, str(e))
    for mid in mids:
        print(f"sent {mid}")
    if args.wait is None:
        return EXIT_OK
    # --wait: treat the whole just-sent batch as read, then wait.
    if args.wait < 0:
        die(EXIT_USAGE, "--wait must be >= 0")
    try:
        save_seen_union(args.dir, args.name, set(mids))
        count = deliver_messages(args.dir, args.name, args.wait,
                                 coalesce=True)
    except OSError as e:
        die(EXIT_ERROR, str(e))
    if count == 0:
        print(f"TIMEOUT: no new messages after {args.wait:g}s",
              file=sys.stderr)
        return EXIT_TIMEOUT
    return EXIT_OK


def own_through_ids(chat_dir, name, through_id):
    """Ids of all messages from name with seq <= seq(through_id).

    The id format (<name>-<6 digits>-<4 hex>) is re-parsed with MSG_RE so
    names containing '-' stay consistent with every other id operation.
    Dies with EXIT_USAGE if the id belongs to a different sender.
    """
    m = MSG_RE.match(through_id + ".md")
    if m is None:  # cannot happen: valid_message_id vetted the format
        die(EXIT_USAGE, f"invalid message id: {through_id}")
    sender, seq_s, _ = m.groups()
    if sender != name:
        die(EXIT_USAGE, f"--ignore-own-through: id {through_id} belongs "
                        f"to {sender}, not {name} — you can only skip "
                        f"your own messages")
    top = int(seq_s)
    return {msg.id for msg in scan_messages(chat_dir)
            if msg.sender == name and msg.seq <= top}


def cmd_wait(args):
    require_chat(args.dir)
    if args.timeout < 0:
        die(EXIT_USAGE, "--timeout must be >= 0")
    try:
        ignore_ids = {i for group in (args.ignore or []) for i in group}
        if args.ignore_own_through:
            ignore_ids |= own_through_ids(args.dir, args.name,
                                          args.ignore_own_through)
        if ignore_ids:
            # Mark the given ids as read without printing them. Persisted,
            # so they stay ignored on every later wait/read too.
            save_seen_union(args.dir, args.name, ignore_ids)
        count = deliver_messages(args.dir, args.name, args.timeout,
                                 coalesce=True)
    except OSError as e:
        die(EXIT_ERROR, str(e))
    if count == 0:
        print(f"TIMEOUT: no new messages after {args.timeout:g}s",
              file=sys.stderr)
        return EXIT_TIMEOUT
    return EXIT_OK


def cmd_read(args):
    require_chat(args.dir)
    if args.all:
        try:
            for msg in scan_messages(args.dir):
                print_message(msg)
        except OSError as e:
            die(EXIT_ERROR, str(e))
        return EXIT_OK
    if not args.name:
        die(EXIT_USAGE, "read: --name is required unless --all is given")
    try:
        count = deliver_messages(args.dir, args.name, 0.0)
    except OSError as e:
        die(EXIT_ERROR, str(e))
    if count == 0:
        # Not a timeout: read is a non-blocking empty poll. Distinguish a
        # fresh name in an empty chat from an existing reading position:
        # a ghost read under someone else's name silently no-ops (the
        # position is already drained) and used to look identical to
        # "nothing there" — the exit-3 ambiguity hid name collisions.
        if os.path.exists(seen_path(args.dir, args.name)):
            seen = load_seen(args.dir, args.name)
            print(f"no new messages: '{args.name}' already has a reading "
                  f"position ({len(seen)} delivered) — if this name is not "
                  f"yours, it is taken; see 'status' / 'claim'",
                  file=sys.stderr)
        elif sender_message_count(args.dir, args.name):
            # Send-only name: no position to distinguish, but the sender
            # exists — the read-side mirror of claim's spoken-name refusal.
            spoken = sender_message_count(args.dir, args.name)
            print(f"no new messages: '{args.name}' has spoken ({spoken} "
                  f"messages) but has no reading position — if this name "
                  f"is not yours, it is taken", file=sys.stderr)
        elif scan_messages(chat_dir=args.dir):
            print(f"no new messages for you: this chat holds only direct "
                  f"messages to others; '{args.name}' is a fresh name",
                  file=sys.stderr)
        else:
            print(f"no new messages: chat is empty and '{args.name}' is "
                  f"a fresh name", file=sys.stderr)
        return EXIT_TIMEOUT
    return EXIT_OK


def cmd_claim(args):
    """Eager presence: create the name's state file before any read.

    Makes the join race visible: `claim` atomically (O_EXCL) creates
    state/<name>.seen.json, so status lists the joiner immediately and
    two joiners racing for the same fresh name get exactly one winner.
    Advisory for peers that skip claim and read/send anyway.
    """
    require_chat(args.dir)
    # A send-only name has no state file but is taken all the same:
    # a successful claim here would hand a ghost a "fresh" name badge.
    # Refuse before the O_EXCL create.
    spoken = sender_message_count(args.dir, args.name)
    if spoken:
        die(EXIT_ERROR, f"name '{args.name}' has spoken ({spoken} "
                        f"messages) but has no reading position — it is "
                        f"taken; if it is your own sender name, claiming "
                        f"is unnecessary (status already shows you)")
    try:
        os.makedirs(os.path.join(args.dir, STATE_DIR), exist_ok=True)
        exclusive_write_json(os.path.join(args.dir, STATE_DIR),
                             f"{args.name}.seen.json",
                             {"delivered": []})
    except FileExistsError:
        seen = load_seen(args.dir, args.name)
        die(EXIT_ERROR, f"name '{args.name}' already has a reading "
                        f"position ({len(seen)} delivered) — in use now or "
                        f"used by a departed agent; pick another name, or "
                        f"proceed if this is your own position")
    except OSError as e:
        die(EXIT_ERROR, str(e))
    print(f"claimed '{args.name}': fresh name, no prior reading position")
    return EXIT_OK


def known_readers(chat_dir, spoken):
    """(name, last_seen) pairs for readers under state/ that never sent.

    Every name with a `state/<name>.seen.json` has (or had) a reading
    position, so its name is taken even though it never spoke. The file's
    mtime — set by `claim`, refreshed on each delivery to that name —
    serves as the last-seen time. Advisory presence info only, not a
    reservation: a joiner that neither claims nor reads stays invisible,
    and two joiners can still race past the same status.
    """
    readers = []
    state_dir = os.path.join(chat_dir, STATE_DIR)
    try:
        entries = os.listdir(state_dir)
    except OSError:
        return readers
    for entry in entries:
        m = re.fullmatch(r"(.+)\.seen\.json", entry)
        if not m or m.group(1) in spoken:
            continue
        try:
            seen_at = os.path.getmtime(os.path.join(state_dir, entry))
        except OSError:
            continue
        readers.append((m.group(1), seen_at))
    readers.sort()
    return readers


def cmd_status(args):
    require_chat(args.dir)
    meta = read_chat_meta(args.dir)
    try:
        msgs = scan_messages(args.dir)
    except OSError as e:
        die(EXIT_ERROR, str(e))
    per_sender = {}
    for msg in msgs:
        count, last_seq = per_sender.get(msg.sender, (0, 0))
        per_sender[msg.sender] = (count + 1, max(last_seq, msg.seq))
    print(f"dir: {args.dir}")
    print(f"topic: {meta.get('topic') or '(none)'}")
    print(f"created: {meta.get('created') or '(unknown)'}")
    print(f"messages: {len(msgs)}")
    for sender in sorted(per_sender):
        count, last_seq = per_sender[sender]
        print(f"{sender}: {count} messages, last seq {last_seq}")
    readers = known_readers(args.dir, per_sender)
    if readers:
        shown = ", ".join(
            f"{name} (seen "
            f"{datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')})"
            for name, ts in readers)
        print(f"known readers (not spoken): {shown}")
    return EXIT_OK


# --------------------------------------------------------------------------
# CLI

def build_parser():
    parser = argparse.ArgumentParser(
        prog="a2a_chat.py",
        description="Agent-to-agent broadcast chat over a shared directory "
                    "(lock-free, stdlib only).",
        epilog="examples:\n"
               "  a2a_chat.py init --dir /tmp/chat --topic 'fix the bug'\n"
               "  a2a_chat.py claim --dir /tmp/chat --name Q\n"
               "  a2a_chat.py send --dir /tmp/chat --name Q --file msg.md\n"
               "  a2a_chat.py send --dir /tmp/chat --name Q --file dm.md "
               "--to W\n"
               "  a2a_chat.py send --dir /tmp/chat --name Q --file m1.md --file m2.md\n"
               "  a2a_chat.py send --dir /tmp/chat --name Q --file msg.md --wait 120\n"
               "  a2a_chat.py send --dir /tmp/chat --name Q --file - < body.md\n"
               "  a2a_chat.py wait --dir /tmp/chat --name Q --timeout 300 --ignore Q-000001-a1f2\n"
               "  a2a_chat.py wait --dir /tmp/chat --name Q --ignore-own-through Q-000003-a1f2\n"
               "  a2a_chat.py read --dir /tmp/chat --name W --all\n"
               "  a2a_chat.py status --dir /tmp/chat\n",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create a chat in a directory")
    p.add_argument("--dir", required=True, help="chat directory (created)")
    p.add_argument("--topic", default=None, help="optional topic string")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("claim", help="take your name: create its reading "
                                     "position before your first read")
    p.add_argument("--dir", required=True, help="chat directory")
    p.add_argument("--name", required=True, type=valid_name,
                   help="your name in this chat")
    p.set_defaults(func=cmd_claim)

    p = sub.add_parser("send", help="commit a message to the chat")
    p.add_argument("--dir", required=True, help="chat directory")
    p.add_argument("--name", required=True, type=valid_name,
                   help="your name in this chat")
    p.add_argument("--file", required=True, action="append", metavar="PATH",
                   help="file with the message body, or '-' for stdin; "
                        "relative paths resolve against the chat directory; "
                        "repeat to commit several messages back-to-back "
                        "(stdin cannot be mixed or repeated)")
    p.add_argument("--to", default=None, type=valid_name, metavar="NAME",
                   help="send as a direct message: delivered only when "
                        "NAME waits/reads (still stored and visible in "
                        "'read --all' and status tallies)")
    p.add_argument("--wait", nargs="?", const=300.0, type=float,
                   default=None, metavar="SEC",
                   help="after sending, wait up to SEC seconds (default 300) "
                        "for new messages; your own messages are skipped")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("wait", help="block until new messages arrive")
    p.add_argument("--dir", required=True, help="chat directory")
    p.add_argument("--name", required=True, type=valid_name,
                   help="your name in this chat")
    p.add_argument("--timeout", type=float, default=300.0, metavar="SEC",
                   help="seconds to wait before giving up (default 300)")
    p.add_argument("--ignore", action="append", nargs="+",
                   type=valid_message_id, default=None, metavar="ID",
                   help="message id to treat as already read, without "
                        "printing it (repeatable; several ids per flag)")
    p.add_argument("--ignore-own-through", type=valid_message_id,
                   default=None, metavar="ID",
                   help="skip every message you sent up to and including "
                        "ID — one flag covers a whole burst of yours")
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("read", help="deliver new messages now, or print all")
    p.add_argument("--dir", required=True, help="chat directory")
    p.add_argument("--name", type=valid_name, default=None,
                   help="your name in this chat (not needed with --all)")
    p.add_argument("--all", action="store_true",
                   help="print the entire log; does not advance delivery state")
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("status", help="chat summary")
    p.add_argument("--dir", required=True, help="chat directory")
    p.set_defaults(func=cmd_status)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args) or EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        os._exit(EXIT_OK)
