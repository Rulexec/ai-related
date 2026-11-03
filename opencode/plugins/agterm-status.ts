import type { Plugin, PluginInput } from "@opencode-ai/plugin"

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
// Three design choices prevent stale "active" status:
//
// 1. Serial queue — setStatus calls are chained onto a single
//    promise queue so no two agtermctl subprocesses run
//    concurrently. This prevents out-of-order completion where
//    an "active" call finishes after a "completed" call.
//
// 2. Root session filter — only root sessions (no parentID)
//    are tracked and mirrored for session.status events.
//    Subagent sessions emit their own session.status events
//    through the same pipeline; without filtering, a background
//    subagent's "busy" event could overwrite the root session's
//    "completed" status.
//
// 3. Permission/question asks from ANY session in the tree
//    (root or subagent) set the status to "blocked". A
//    pendingAsks counter tracks unresolved asks across the
//    whole tree — the status returns to "active" only when
//    the counter reaches zero. This handles subagent asks
//    (whose sessionID is the child's, not the root's) and
//    concurrent asks from multiple subagents.
// ============================================================

export default (async ({ $ }: PluginInput) => {
  const sessionId = process.env.AGTERM_SESSION_ID
  if (!sessionId) return {}

  let queue: Promise<void> = Promise.resolve()

  const setStatus = (state: string): void => {
    queue = queue.then(async () => {
      try {
        await $`agtermctl session status ${state} --target ${sessionId}`
          .quiet()
          .nothrow()
      } catch {
        // silent — never crash opencode
      }
    })
    queue.catch(() => {})
  }

  const rootSessions = new Set<string>()
  let pendingAsks = 0

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
        if (statusType === "busy") setStatus("active")
        else if (statusType === "idle") setStatus("completed")
        else if (statusType === "retry") setStatus("blocked")
        return
      }

      // Permission/question asks from any session in the tree (root or
      // subagent) block the agterm indicator. The sessionID on these events
      // is the session that raised the ask — for subagent asks this is the
      // child session ID, so the rootSessions filter must NOT be applied
      // here. A pendingAsks counter tracks unresolved asks so the status
      // returns to "active" only when all asks are resolved.
      if (type === "permission.asked" || type === "question.asked") {
        pendingAsks++
        setStatus("blocked")
        return
      }

      if (
        type === "permission.replied" ||
        type === "question.replied" ||
        type === "question.rejected"
      ) {
        pendingAsks = Math.max(0, pendingAsks - 1)
        if (pendingAsks === 0) setStatus("active")
        return
      }
    },
  }
}) satisfies Plugin
