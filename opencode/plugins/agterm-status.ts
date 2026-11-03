import { spawn } from "node:child_process"
import type { Plugin } from "@opencode-ai/plugin"

// ============================================================
// agterm Session Status Plugin
//
// Reads AGTERM_SESSION_ID from the environment and mirrors
// opencode session status into agterm's agent-status indicator
// via `agtermctl session status`.
//
// When AGTERM_SESSION_ID is unset, the plugin returns {} (no
// hooks) — a complete no-op with zero overhead.
//
// Design choices:
//
// 1. Serial queue + bounded spawn — setStatus calls are chained
//    onto a single promise queue so no two agtermctl subprocesses
//    run concurrently. This prevents out-of-order completion where
//    an "active" call finishes after a "completed" call. Each
//    spawn is bounded: after 10s the whole process group gets
//    SIGTERM (detached + kill(-pid)), so a hung agtermctl cannot
//    stall the queue forever.
//
// 2. Root session filter — only root sessions (no parentID)
//    are tracked and mirrored for session.status/session.error
//    events. Subagent sessions emit their own events through the
//    same pipeline; without filtering, a background subagent's
//    "busy" event could overwrite the root session's "completed"
//    status.
//
// 3. Turn-ending errors block — a session.error on a tracked root
//    session sets "blocked" and latches the session: the idle
//    published right after the error is swallowed, so a failed
//    turn does not end up reported as "completed". Aborts are not
//    failures (Esc routes through halt(DOMException AbortError),
//    parsed to MessageAbortedError — either name may surface) and
//    fall through to the following idle → "completed". The latch
//    clears on the next busy/retry.
//
// 4. Permission/question asks from ANY session in the tree (root
//    or subagent) set the status to "blocked". Pending asks are
//    tracked by request id; the status returns to "active" only
//    when the last one is answered. An interrupted turn drops its
//    asks without reply events, so a root session's idle sweeps
//    the pending set — an ask can never leak the "blocked" state
//    forever.
// ============================================================

// Interrupts are not failures: Esc routes through halt(DOMException AbortError),
// which is parsed to MessageAbortedError — either name may surface.
const ABORT_ERROR_NAMES = new Set(["MessageAbortedError", "AbortError"])

// A hung agtermctl must not stall the status queue: every spawn is bounded,
// and the whole process group is SIGTERMed when the bound expires.
const SPAWN_TIMEOUT_MS = 10_000

export default (async () => {
  const sessionId = process.env.AGTERM_SESSION_ID
  if (!sessionId) return {}

  const runAgtermctl = (state: string): Promise<void> =>
    new Promise((resolve) => {
      let settled = false
      const finish = () => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        resolve()
      }
      const child = spawn("agtermctl", ["session", "status", state, "--target", sessionId], {
        stdio: "ignore",
        env: process.env,
        detached: true,
      })
      child.on("error", finish)
      child.on("close", finish)
      const timer = setTimeout(() => {
        try {
          if (child.pid != null) process.kill(-child.pid, "SIGTERM")
        } catch {
          // already gone
        }
        finish()
      }, SPAWN_TIMEOUT_MS)
    })

  let queue: Promise<void> = Promise.resolve()

  const setStatus = (state: string): void => {
    // silent — never crash opencode
    queue = queue.then(() => runAgtermctl(state)).catch(() => {})
  }

  const rootSessions = new Set<string>()
  const erroredSessions = new Set<string>()
  // request ids of permission/question asks still waiting for an answer
  const pendingAsks = new Set<string>()

  return {
    event: async ({
      event,
    }: {
      event: { type: string; properties?: any }
    }) => {
      const type = event.type

      if (type === "session.created") {
        const info = event.properties?.info
        if (info && !info.parentID) {
          rootSessions.add(event.properties?.sessionID ?? info.id)
        }
        return
      }

      if (type === "session.status") {
        const sid = event.properties?.sessionID
        if (!sid) return
        // Fallback: if rootSessions is empty (session.created not received),
        // treat the first session.status event as a root session. This is safe
        // because the root session's first "busy" status always fires before
        // any subagent is spawned (subagents are created during the root
        // session's run loop, after status is set to "busy").
        if (rootSessions.size === 0) rootSessions.add(sid)
        if (!rootSessions.has(sid)) return
        const statusType = event.properties?.status?.type
        if (statusType === "busy" || statusType === "retry") {
          // work (re)started — a latched error no longer applies
          erroredSessions.delete(sid)
          setStatus(statusType === "busy" ? "active" : "blocked")
        } else if (statusType === "idle") {
          // an interrupted turn drops its asks without reply events, so idle
          // is what clears them (root idle ends the whole tree's turn)
          pendingAsks.clear()
          // swallow the idle published right after a latched error so the
          // "blocked" sticks instead of flipping to "completed"
          if (erroredSessions.delete(sid)) return
          setStatus("completed")
        }
        return
      }

      if (type === "session.error") {
        const sid = event.properties?.sessionID
        // unattributed errors and subagent errors don't drive the root
        // indicator (the root turn reports its own outcome)
        if (!sid || !rootSessions.has(sid)) return
        // interrupts are not failures — the following idle reports "completed"
        if (ABORT_ERROR_NAMES.has(event.properties?.error?.name)) return
        erroredSessions.add(sid)
        setStatus("blocked")
        return
      }

      // Permission/question asks from any session in the tree (root or
      // subagent) block the agterm indicator. The sessionID on these events
      // is the session that raised the ask — for subagent asks this is the
      // child session ID, so the rootSessions filter must NOT be applied
      // here. Replies are matched by request id; the status returns to
      // "active" only when all asks are resolved.
      if (type === "permission.asked" || type === "question.asked") {
        const id = event.properties?.id
        if (typeof id === "string") pendingAsks.add(id)
        setStatus("blocked")
        return
      }

      if (
        type === "permission.replied" ||
        type === "question.replied" ||
        type === "question.rejected"
      ) {
        pendingAsks.delete(event.properties?.requestID)
        if (pendingAsks.size === 0) setStatus("active")
        return
      }
    },
  }
}) satisfies Plugin
