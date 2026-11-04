// Route-state channel between the agterm-a2a TUI plugin (writer, main
// thread) and the agterm-a2a server plugin (reader, worker thread).
//
// opencode v1.19.x runs the TUI and the server in separate threads with no
// shared memory, so the server plugin cannot see which session the user has
// picked in the TUI. The TUI plugin polls `api.route.current` and publishes
// the displayed session id to a per-pane file:
//
//   ${XDG_STATE_HOME:-~/.local/state}/opencode-agterm-a2a/route-<paneId>.json
//   { "pid": 12345, "session_id": "ses_x" | null, "updated": 1730000000000 }
//
// - `pid` fences against a previous opencode instance in the same pane:
//   Bun workers share the main process pid, so TUI and server compute the
//   same value, while pane ids (like pane mailboxes) survive program swaps.
// - `session_id` is null when the TUI shows the home screen or a plugin
//   route — no session is picked.
// - `updated` (Date.now() ms) is refreshed by a heartbeat rewrite so the
//   reader can distinguish "user staring at one session" from "writer died".
//
// Reader rule (`visibility`): missing file, foreign pid, or stale timestamp
// → unknown → fail open (deliver — headless serve / mini mode / TUI plugin
// absent or disabled keep the pre-gate behavior). Fresh knowledge → null
// session means drop; a session id means deliver only when it sits in the
// same task tree as the wake target (`sameTaskTree`, same root via parentID
// — covers the identical session, a parent viewed while its sub-task runs,
// a sub-task viewed while the parent idles, and task-tree siblings).

import * as fs from "node:fs"
import * as os from "node:os"
import * as path from "node:path"

export const ROUTE_POLL_MS = 250
export const ROUTE_HEARTBEAT_MS = 5_000
export const ROUTE_STALE_MS = 30_000
export const ROUTE_PRUNE_MS = 7 * 24 * 60 * 60 * 1000

export type RouteState = {
  pid: number
  session_id: string | null
  updated: number
}

const PANE_ID_RE = /^[A-Za-z0-9_-]{1,64}$/

type StateEnv = { XDG_STATE_HOME?: string }

export function routeStateDir(env: StateEnv = process.env): string {
  const base =
    env.XDG_STATE_HOME && env.XDG_STATE_HOME.trim() !== ""
      ? env.XDG_STATE_HOME
      : path.join(os.homedir(), ".local", "state")
  return path.join(base, "opencode-agterm-a2a")
}

export function routeStatePath(paneId: string, env: StateEnv = process.env): string {
  if (!PANE_ID_RE.test(paneId)) throw new Error(`invalid pane id: ${JSON.stringify(paneId)}`)
  return path.join(routeStateDir(env), `route-${paneId}.json`)
}

/** Parse a route-state file body; null on any structural problem. */
export function parseRouteState(raw: string): RouteState | null {
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return null
  }
  if (typeof parsed !== "object" || parsed === null) return null
  const s = parsed as Record<string, unknown>
  if (typeof s.pid !== "number" || !Number.isInteger(s.pid) || s.pid <= 0) return null
  if (s.session_id !== null && (typeof s.session_id !== "string" || s.session_id === "")) return null
  if (typeof s.updated !== "number" || !Number.isInteger(s.updated) || s.updated <= 0) return null
  return { pid: s.pid, session_id: s.session_id as string | null, updated: s.updated }
}

/** Atomic (tmp+rename) write; best-effort, never throws. */
export function writeRouteState(file: string, sessionId: string | null, pid: number = process.pid): boolean {
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true })
    const tmp = file + ".tmp-" + pid
    fs.writeFileSync(tmp, JSON.stringify({ pid, session_id: sessionId, updated: Date.now() }) + "\n")
    fs.renameSync(tmp, file)
    return true
  } catch {
    return false
  }
}

/** Read+parse this pane's route state; null when absent or malformed. */
export function readRouteState(paneId: string, env: StateEnv = process.env): RouteState | null {
  try {
    return parseRouteState(fs.readFileSync(routeStatePath(paneId, env), "utf8"))
  } catch {
    return null
  }
}

export type Visibility =
  | { kind: "unknown" } // no usable knowledge → deliver (fail open)
  | { kind: "home" } // TUI displays no session → drop
  | { kind: "session"; displayed: string } // compare task trees

/** Classify what this instance's TUI is displaying, from the route state. */
export function visibility(target: string, state: RouteState | null, now: number, ownPid: number): Visibility {
  if (state === null) return { kind: "unknown" }
  if (state.pid !== ownPid) return { kind: "unknown" }
  if (state.updated + ROUTE_STALE_MS < now) return { kind: "unknown" }
  if (state.session_id === null) return { kind: "home" }
  return { kind: "session", displayed: state.session_id }
}

/** True when both sessions belong to the same task tree (same root via
 * parentID). Covers identical ids, ancestor/descendant both directions,
 * and siblings. Cycle- and self-loop-guarded (corrupt parentage ends the
 * walk at the offending node). */
export function sameTaskTree(a: string, b: string, parentOf: (id: string) => string | undefined): boolean {
  return taskRoot(a, parentOf) === taskRoot(b, parentOf)
}

function taskRoot(id: string, parentOf: (id: string) => string | undefined): string {
  const seen = new Set<string>()
  let cur = id
  while (!seen.has(cur)) {
    seen.add(cur)
    const parent = parentOf(cur)
    if (parent === undefined || parent === cur) break
    cur = parent
  }
  return cur
}

/** Best-effort removal of route-state files whose writer is long dead. */
export function pruneRouteStates(maxAgeMs: number = ROUTE_PRUNE_MS, env: StateEnv = process.env): void {
  try {
    const dir = routeStateDir(env)
    const cutoff = Date.now() - maxAgeMs
    for (const name of fs.readdirSync(dir)) {
      if (!name.startsWith("route-") || !name.endsWith(".json")) continue
      const file = path.join(dir, name)
      try {
        if (fs.statSync(file).mtimeMs < cutoff) fs.unlinkSync(file)
      } catch {
        // racing writer — leave it
      }
    }
  } catch {
    // missing dir or unreadable — nothing to prune
  }
}
