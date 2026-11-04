// agterm-a2a — receiver for a2a chat nudges over the agterm message bus.
//
// Contract (v1):
//   • The a2a poke pass sends an anonymous JSON nudge to this pane's
//     mailbox: {"type":"a2a-nudge","v":1,"chat_dir","a2a_dir","name",
//     "chat_token"}. The bus never wakes anyone — this plugin does.
//   • One watcher child per opencode instance (`agtermctl events
//     --kind message --json`); on an event for OUR pane, pull the
//     mailbox (`msg read --as`, no --ack), dispatch by (type, v),
//     ack each message exactly once at its disposition.
//   • Wake = client.session.prompt() into the opencode session bound
//     to (chat_dir, name) — the session that actually ran an a2a
//     chat command from this instance (binding recorded by the
//     tool.execute.before hook below, persisted next to the a2a
//     chat state). This is the fence against nudges landing on an
//     unrelated session that merely re-used the same terminal
//     (pane-scoped mailboxes survive a program swap).
//   • Visibility gate (user decision 2026-11-04, reverses the old
//     "no live-activity guard" for TUI instances): wake only when
//     the bound session's task tree is the one the user currently
//     views in this instance's TUI. The TUI runs in another thread,
//     so tui-plugin/agterm-a2a-tui.ts (registered in tui.json) pub-
//     lishes the displayed session to a per-pane route-state file
//     (src/agterm-route-state.ts); the gate reads it here. Same task
//     tree (same root via parentID — identical, parent vs sub-task,
//     siblings) counts as visible. Home/plugin route → drop; differ-
//     ent tree → drop. Missing, stale, or foreign-pid route state
//     (headless serve, mini mode, TUI plugin absent/disabled) →
//     fail open: deliver as before.
//   • Busy target (session.status busy/loading) → drop+ack: a busy
//     receiver is inside its a2a loop and re-checks the chat itself.
//
// Everything is fail-closed and silent: this plugin must never throw
// into opencode, block the TUI, or fail the tool call it observes.

import type { Plugin } from "@opencode-ai/plugin"
import * as fs from "node:fs"
import * as os from "node:os"
import * as path from "node:path"
import { readRouteState, sameTaskTree, visibility } from "../src/agterm-route-state"

// opencode executes plugins under Bun; reach the global through
// globalThis so the file also typechecks standalone (no ambient Bun
// types required) and never conflicts with real @types/bun.
const gBun = (globalThis as { Bun?: any }).Bun ?? null

// ---------------------------------------------------------------------------
// Constants

const PLUGIN_SERVICE = "agterm-a2a"
const NUDGE_TYPE = "a2a-nudge"
const NUDGE_VERSION = 1
const DEDUPE_CAP = 256
const RESTART_BACKOFF_MS = 1_000
const RESTART_BACKOFF_MAX_MS = 30_000

export type Nudge = {
  type: string
  v: number
  chat_dir: string
  a2a_dir: string
  name: string
  chat_token: string
}

// ---------------------------------------------------------------------------
// Pure helpers (exported for tests)

/** shlex-style single-word quoting for paths embedded in the prompt. */
export function shellQuote(value: string): string {
  if (value !== "" && /^[A-Za-z0-9_@%+=:,./-]+$/.test(value)) return value
  return "'" + value.replace(/'/g, `'\\''`) + "'"
}

/** Expand a leading `~`/`~/` against the home directory. The shell already
 * expanded the tilde for the command we captured this value from; the raw
 * command text still has it, so replicate the expansion before resolving. */
export function expandTildeDir(value: string): string {
  if (value === "~") return os.homedir()
  if (value.startsWith("~/")) return os.homedir() + value.slice(1)
  return value
}

/** Parse one `agtermctl events --json` line; true when it is a message
 * event addressed to our own session+pane. Never throws. */
export function eventTargetsMe(
  line: string,
  ownSession: string,
  ownPaneId: string,
): boolean {
  try {
    const evt = JSON.parse(line)
    return (
      typeof evt === "object" && evt !== null &&
      evt.kind === "message" &&
      evt.session === ownSession &&
      typeof evt.payload === "object" && evt.payload !== null &&
      evt.payload.paneID === ownPaneId &&
      typeof evt.payload.messageID === "string"
    )
  } catch {
    return false
  }
}

export type MailMessage = { id: string; body: string }

/** Parse a `msg read --json` reply into its message list; null on any
 * parse problem (caller treats it as an empty pull). */
export function parseReadReply(stdout: string): MailMessage[] {
  try {
    const reply = JSON.parse(stdout)
    const msgs = reply?.result?.messages
    if (!Array.isArray(msgs)) return []
    const out: MailMessage[] = []
    for (const m of msgs) {
      if (typeof m?.id === "string" && typeof m?.body === "string") {
        out.push({ id: m.id, body: m.body })
      }
    }
    return out
  } catch {
    return []
  }
}

/** Parse and structurally validate an a2a-nudge body. Returns null for
 * anything other than exactly (type=a2a-nudge, v=1) with non-empty
 * string fields — the caller acks it as garbage. */
export function parseNudge(body: string): Nudge | null {
  let parsed: unknown
  try {
    parsed = JSON.parse(body)
  } catch {
    return null
  }
  if (typeof parsed !== "object" || parsed === null) return null
  const n = parsed as Record<string, unknown>
  if (n.type !== NUDGE_TYPE || n.v !== NUDGE_VERSION) return null
  const fields = [n.chat_dir, n.a2a_dir, n.name, n.chat_token]
  if (fields.some((f) => typeof f !== "string" || f === "")) return null
  if (!/^[A-Za-z0-9_-]{1,32}$/.test(n.name as string)) return null
  return {
    type: n.type as string,
    v: n.v as number,
    chat_dir: n.chat_dir as string,
    a2a_dir: n.a2a_dir as string,
    name: n.name as string,
    chat_token: n.chat_token as string,
  }
}

/** The injected prompt — a bare notification, matching the a2a skill's
 * nudge trigger phrase. The read command line was dropped from the a2a
 * skill (its nudge clause no longer says "run the read command it
 * shows"); how to read is the receiver's a2a skill's business, so the
 * payload's a2a_dir is no longer rendered. */
export function renderPrompt(n: Nudge): string {
  const dir = shellQuote(n.chat_dir)
  return `You have unread messages in a2a chat ${dir}`
}

/** Extract (--dir, --name) from an a2a_chat.py command line; null when
 * the command does not reference a2a_chat.py or lacks either flag. */
export function parseChatCommand(command: string): { dir: string; name: string } | null {
  if (!command.includes("a2a_chat.py")) return null
  const flag = (name: string): string | null => {
    // Unquoted values stop at whitespace or any shell-chain punctuation,
    // so `--name W; other` / `--name W&&other` yield exactly "W".
    const m = command.match(
      new RegExp(`--${name}(?:=|[ \\t]+)('([^']*)'|"([^"]*)"|([^\\s;&|()]+))`),
    )
    if (!m) return null
    return m[2] ?? m[3] ?? m[4] ?? null
  }
  const dir = flag("dir")
  const name = flag("name")
  if (!dir || !name) return null
  if (!/^[A-Za-z0-9_-]{1,32}$/.test(name)) return null
  return { dir, name }
}

/** Bounded FIFO used as the messageID dedupe set (re-deliveries of an
 * already-handled batch are swallowed). */
export class Dedupe {
  private readonly items = new Set<string>()
  constructor(private readonly cap: number) {}
  has(id: string): boolean {
    return this.items.has(id)
  }
  push(id: string): void {
    if (this.items.has(id)) return
    this.items.add(id)
    if (this.items.size > this.cap) {
      const oldest = this.items.values().next().value
      if (oldest !== undefined) this.items.delete(oldest)
    }
  }
}

/** Read chat.json's `created` token from a chat dir; null on any problem. */
export function readChatToken(chatDir: string): string | null {
  try {
    const data = JSON.parse(fs.readFileSync(path.join(chatDir, "chat.json"), "utf8"))
    return typeof data?.created === "string" ? data.created : null
  } catch {
    return null
  }
}

/** Read the opencode session id bound to (chatDir, name); null when no
 * binding file exists or it is malformed. */
export function readBinding(chatDir: string, name: string): string | null {
  try {
    const data = JSON.parse(
      fs.readFileSync(path.join(chatDir, "state", `${name}.opencode.json`), "utf8"),
    )
    return typeof data?.session_id === "string" && data.session_id !== ""
      ? data.session_id
      : null
  } catch {
    return null
  }
}

// ---------------------------------------------------------------------------
// Plugin

type LogFn = (level: "info" | "warn", message: string) => Promise<void>

/** Resolve the agtermctl binary: $AGTERMCTL when it points at an
 * executable file, else `agtermctl` on PATH, else null (plugin inert). */
function resolveCtl(): string | null {
  const candidate = process.env.AGTERMCTL
  if (candidate) {
    try {
      fs.accessSync(candidate, fs.constants.X_OK)
      return candidate
    } catch {
      return null
    }
  }
  try {
    return gBun?.which("agtermctl") ?? null
  } catch {
    return null
  }
}

export const AgtermA2A: Plugin = async ({ client, directory }) => {
  const ownSession = process.env.AGTERM_SESSION_ID ?? ""
  const ownPaneId = process.env.AGTERM_PANE_ID ?? ""
  const ctl = resolveCtl()
  if (!ownSession || !ownPaneId || !ctl) return {}

  const log: LogFn = async (level, message) => {
    try {
      await client.app.log({ body: { service: PLUGIN_SERVICE, level, message } })
    } catch {
      // logging must never take anything down
    }
  }

  const identity = `${ownSession}:${ownPaneId}`
  const dedupe = new Dedupe(DEDUPE_CAP)
  const sessionStatus = new Map<string, string>() // sessionID -> "idle"|"busy"|"loading"

  // --- binding record -----------------------------------------------------

  async function writeBinding(chatDir: string, name: string, sessionID: string) {
    try {
      const stateDir = path.join(chatDir, "state")
      fs.mkdirSync(stateDir, { recursive: true })
      const target = path.join(stateDir, `${name}.opencode.json`)
      const tmp = target + ".tmp-" + process.pid
      fs.writeFileSync(
        tmp,
        JSON.stringify({ session_id: sessionID, updated: new Date().toISOString() }) + "\n",
      )
      fs.renameSync(tmp, target)
    } catch {
      // best effort; the a2a loop re-runs chat commands constantly
    }
  }

  // --- child processes ----------------------------------------------------

  function runCtl(args: string[], timeoutMs: number): Promise<{ ok: boolean; stdout: string }> {
    return new Promise((resolve) => {
      try {
        const proc = gBun.spawn([ctl, ...args], {
          stdin: "ignore",
          stdout: "pipe",
          stderr: "pipe",
        })
        let stdout = ""
        const timer = setTimeout(() => {
          try { proc.kill() } catch {}
          resolve({ ok: false, stdout })
        }, Math.max(200, timeoutMs))
        void (async () => {
          try {
            stdout = await new Response(proc.stdout).text()
            const exit = await proc.exited
            clearTimeout(timer)
            resolve({ ok: exit === 0, stdout })
          } catch {
            clearTimeout(timer)
            resolve({ ok: false, stdout })
          }
        })()
      } catch {
        resolve({ ok: false, stdout: "" })
      }
    })
  }

  // --- nudge handling -----------------------------------------------------

  /** sessionID → parentID for every session in this instance's directory;
   * null when the listing fails (caller fails the gate open). */
  async function fetchParentMap(): Promise<Map<string, string | undefined> | null> {
    try {
      const reply: unknown = await client.session.list({ query: { directory } })
      const sessions = Array.isArray(reply)
        ? reply
        : Array.isArray((reply as { data?: unknown } | null)?.data)
          ? ((reply as { data: unknown[] }).data)
          : null
      if (!sessions) return null
      const map = new Map<string, string | undefined>()
      for (const s of sessions as Array<Record<string, unknown>>) {
        if (typeof s?.id === "string") {
          map.set(s.id, typeof s.parentID === "string" ? s.parentID : undefined)
        }
      }
      return map
    } catch {
      return null
    }
  }

  /** Handle one validated nudge: wake the bound session. Returns ids to
   * ack (always — every pulled message is acked exactly once). */
  async function handleNudge(n: Nudge): Promise<void> {
    const token = readChatToken(n.chat_dir)
    if (token !== n.chat_token) {
      await log("warn", `nudge token mismatch for ${n.name}, dropped`)
      return
    }
    const target = readBinding(n.chat_dir, n.name)
    if (!target) {
      await log("warn", `no opencode session binding for ${n.name}, dropped`)
      return
    }
    const status = sessionStatus.get(target)
    if (status === "busy" || status === "loading") {
      return // busy receiver re-checks the chat itself
    }
    // Visibility gate: wake only the task tree the user is currently
    // viewing. No route knowledge (headless serve, mini, TUI plugin
    // absent/stale) → fail open, i.e. the pre-gate behavior.
    const verdict = visibility(target, readRouteState(ownPaneId), Date.now(), process.pid)
    if (verdict.kind === "home") {
      await log("info", `nudge for ${n.name}: TUI displays no session, dropped`)
      return
    }
    if (verdict.kind === "session" && verdict.displayed !== target) {
      const parentOf = await fetchParentMap()
      if (
        parentOf !== null &&
        parentOf.has(target) &&
        parentOf.has(verdict.displayed) &&
        !sameTaskTree(target, verdict.displayed, (id) => parentOf.get(id))
      ) {
        await log(
          "info",
          `nudge for ${n.name}: bound session ${target} is in background (viewing ${verdict.displayed}), dropped`,
        )
        return
      }
    }
    try {
      const result = await client.session.prompt({
        path: { id: target },
        body: { parts: [{ type: "text", text: renderPrompt(n) }] },
      })
      if (result && typeof result === "object" && "error" in result && result.error) {
        await log("warn", `session.prompt failed for ${n.name}: ${JSON.stringify(result.error)}`)
      }
    } catch (error) {
      await log("warn", `session.prompt threw for ${n.name}: ${String(error)}`)
    }
  }

  let pulling = false

  async function pullAndProcess(): Promise<void> {
    if (pulling) return
    pulling = true
    try {
      const { stdout } = await runCtl(
        ["msg", "read", "--json", "--as", identity],
        5_000,
      )
      const messages = parseReadReply(stdout)
      if (messages.length === 0) return

      // 1. Consume: mark every id seen; drop already-deduped ones.
      const fresh = messages.filter((m) => {
        if (dedupe.has(m.id)) return false
        dedupe.push(m.id)
        return true
      })

      // 2. Dispatch: garbage (non-nudge) is acked untouched; valid
      //    nudges are coalesced per (chat_dir, name) into one wake.
      const wakeGroups = new Map<string, { nudge: Nudge; ids: string[] }>()
      const ackNow: string[] = []
      for (const m of fresh) {
        const nudge = parseNudge(m.body)
        if (!nudge) {
          ackNow.push(m.id)
          continue
        }
        const key = `${nudge.chat_dir}\u0000${nudge.name}`
        const group = wakeGroups.get(key)
        if (group) group.ids.push(m.id)
        else wakeGroups.set(key, { nudge, ids: [m.id] })
      }
      if (ackNow.length > 0) await ack(ackNow)

      // 3. Wake, then ack the group after the prompt settles.
      for (const group of wakeGroups.values()) {
        await handleNudge(group.nudge)
        await ack(group.ids)
      }
    } finally {
      pulling = false
    }
  }

  async function ack(ids: string[]): Promise<void> {
    if (ids.length === 0) return
    await runCtl(["msg", "ack", ...ids, "--as", identity], 5_000)
  }

  // --- watcher child ------------------------------------------------------

  let disposed = false
  let child: any = null
  let backoff = RESTART_BACKOFF_MS

  function startWatcher(): void {
    if (disposed) return
    try {
      child = gBun.spawn([ctl, "events", "--kind", "message", "--json"], {
        stdin: "ignore",
        stdout: "pipe",
        stderr: "ignore",
      })
    } catch {
      scheduleRestart()
      return
    }
    void (async () => {
      let buffer = ""
      const reader = child.stdout?.getReader()
      if (!reader) return
      const decoder = new TextDecoder()
      try {
        for (;;) {
          const { done, value } = await reader.read()
          if (done) break
          if (value) {
            buffer += decoder.decode(value, { stream: true })
            let newline: number
            while ((newline = buffer.indexOf("\n")) >= 0) {
              const line = buffer.slice(0, newline).trim()
              buffer = buffer.slice(newline + 1)
              if (!line) continue
              backoff = RESTART_BACKOFF_MS // live stream resets backoff
              if (eventTargetsMe(line, ownSession, ownPaneId)) {
                void pullAndProcess()
              }
            }
          }
        }
      } catch {
        // stream ended or errored — fall through to restart
      }
      scheduleRestart()
    })()
  }

  function scheduleRestart(): void {
    if (disposed) return
    const delay = backoff
    backoff = Math.min(backoff * 2, RESTART_BACKOFF_MAX_MS)
    setTimeout(startWatcher, delay)
  }

  startWatcher()
  void pullAndProcess() // startup drain: nudges queued while we were down
                       // (or left by a previous program in this pane) emit
                       // no new event — pull them once unconditionally
  void log("info", `watching agterm bus mailbox for ${identity}`)

  return {
    "tool.execute.before": async (input, output) => {
      try {
        if (input.tool !== "bash" || !input.sessionID) return
        const args = output.args as { command?: string; workdir?: string } | undefined
        if (!args?.command) return
        const parsed = parseChatCommand(args.command)
        if (!parsed) return
        const dir = expandTildeDir(parsed.dir)
        const chatDir = path.isAbsolute(dir)
          ? dir
          : path.resolve(args.workdir ?? process.cwd(), dir)
        await writeBinding(chatDir, parsed.name, input.sessionID)
      } catch {
        // never fail the observed tool call
      }
    },
    event: async ({ event }) => {
      try {
        if (event.type === "session.status") {
          const { sessionID, status } = event.properties as {
            sessionID: string
            status: { type: string }
          }
          if (sessionID && status?.type) sessionStatus.set(sessionID, status.type)
        }
      } catch {
        // ignore
      }
    },
    dispose: async () => {
      disposed = true
      try {
        child?.kill()
      } catch {
        // already gone
      }
    },
  }
}

export default { id: "agterm-a2a", server: AgtermA2A }
