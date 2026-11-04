// agterm-a2a-tui — publishes the opencode TUI's displayed session to the
// agterm-a2a route-state file (see src/agterm-route-state.ts for the
// channel contract).
//
// Why a TUI plugin: opencode v1.19.x runs the TUI in the main thread and
// server plugins in a worker thread — the agterm-a2a receiver (a server
// plugin) cannot see which session the user has picked. Only
// `api.route.current` knows, and only TUI plugins get it. A v1 plugin file
// may export either `server` or `tui`, never both, so this is a separate
// file cooperating with plugin/agterm-a2a.ts through the state file.
//
// Behavior: poll `api.route.current` every ROUTE_POLL_MS; when the route is
// a session, publish its sessionID; home screen and plugin routes publish
// null (no session picked). A heartbeat rewrite keeps the timestamp fresh
// while the user stares at one session, so the reader can distinguish that
// from a dead writer. Writes are atomic (tmp+rename), best-effort, silent.
//
// Completely inert without a valid AGTERM_PANE_ID (opencode outside agterm,
// remote TUIs): no timer, no files.
//
// Lives in `tui-plugin/`, NOT `plugin/`: the server auto-discovers
// `plugin/*.{ts,js}` and would reject a tui-only export ("must default
// export an object with server()") on every start, while the TUI loads
// plugins ONLY from the `plugin` key of `tui.json` — this file is
// registered there as `./tui-plugin/agterm-a2a-tui.ts`. Both sides load
// their plugin sets at startup; a change needs an instance restart.

import type { TuiPlugin, TuiPluginModule } from "@opencode-ai/plugin/tui"
import * as fs from "node:fs"
import {
  ROUTE_HEARTBEAT_MS,
  ROUTE_POLL_MS,
  pruneRouteStates,
  routeStatePath,
  writeRouteState,
} from "../src/agterm-route-state"

const PANE_ID_RE = /^[A-Za-z0-9_-]{1,64}$/

const tui: TuiPlugin = async (api) => {
  const paneId = process.env.AGTERM_PANE_ID ?? ""
  if (!PANE_ID_RE.test(paneId)) return // not inside agterm — inert
  const file = routeStatePath(paneId)
  pruneRouteStates() // sweep files from long-dead panes

  let last: string | null | undefined = undefined // undefined forces the first write
  let lastWrite = 0

  const tick = (): void => {
    let sid: string | null = null
    try {
      const current = api.route.current
      // The route union's catch-all member (plugin routes) also admits the
      // literal name "session" with optional params, so guard both.
      const candidate = current.name === "session" ? current.params?.sessionID : undefined
      if (typeof candidate === "string") sid = candidate
    } catch {
      return // route not readable this tick — keep the last state
    }
    const now = Date.now()
    if (sid === last && now - lastWrite < ROUTE_HEARTBEAT_MS) return
    if (writeRouteState(file, sid)) {
      last = sid
      lastWrite = now
    }
  }

  const timer = setInterval(tick, ROUTE_POLL_MS)
  timer.unref()

  api.lifecycle.onDispose(() => {
    clearInterval(timer)
    try {
      fs.unlinkSync(file)
    } catch {
      // already gone
    }
  })
}

export default { id: "agterm-a2a-tui", tui } satisfies TuiPluginModule
