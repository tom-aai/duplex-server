# Frontend

The `duplex-frontend` repo is a **Next.js** app: the browser half of the starter. It holds
the UI and the browser client (`lib/duplex.ts`), and it drives the server the way a real
developer's app would. It is also where we feel the shape of the problem: if the browser code
is awkward to use, building this app shows it at once, and that is a signal for the SDK we
have not designed yet. It lives in its own repo, so you can clone and deploy it on its own. It
talks to the broker in this repo over one HTTP call and the
`NEXT_PUBLIC_BROKER_URL` / `DUPLEX_ALLOWED_ORIGINS` pair.

- **Framework:** Next.js 16 (App Router) + React 19, TypeScript. Scaffolded with the official
  `create-next-app`.
- **Role:** the browser half of "connect & talk." It owns the UI; all the WebRTC logic lives
  in the `lib/duplex.ts` module it calls.

## Key files

| File | Purpose |
|---|---|
| `lib/duplex.ts` | The browser client — the `connect()` / `disconnect()` API. See [The connection core](./connection-core.md#browser-client--connect). |
| `app/page.tsx` | The UI: a **Connect & talk / Disconnect** button and status line, a left pane that streams the worker log from the broker's `/logs` endpoint, and a right pane that lists things to try. |

## How the page uses the browser client

```tsx
"use client";
import { connect, type DuplexSession } from "@/lib/duplex";

// on "Connect": session = await connect({ onStatus: setStatus })
// on "Disconnect": await session.disconnect()
```

That's the whole integration — the page never touches `RTCPeerConnection`, SDP, or the data
channel. It only reacts to the status callback and holds the returned session so it can
disconnect. This is the bar we want a future SDK to clear.

## Running it

In the `duplex-frontend` repo:

```bash
npm install                       # first time only
cp .env.local.example .env.local  # NEXT_PUBLIC_BROKER_URL, defaults to http://localhost:8000
npm run dev                       # serves http://localhost:3000
```

Requires the **broker** running too (it posts SDP offers to it) and microphone permission.
Full steps in **[Getting started](./getting-started.md)**.

## Notes / gotchas

- **Microphone + autoplay:** `connect()` is triggered by a user click, which satisfies browser
  autoplay rules — the model's audio plays without extra gestures.
- **HTTPS:** `getUserMedia` requires a secure context. `http://localhost` counts as secure, so
  local dev is fine; a deployed version needs HTTPS.
- **CORS:** the browser calls the broker cross-origin (`:3000` → `:8000`). The broker allows
  `DUPLEX_ALLOWED_ORIGINS` (default `http://localhost:3000`). Change ports or deploy → update
  that var on the broker, and `NEXT_PUBLIC_BROKER_URL` on this app.
- **Build-time URL:** `NEXT_PUBLIC_BROKER_URL` is inlined at build time, so a change to it needs
  a rebuild, not just a restart.
- **Next.js 16 is not the Next.js in your training data.** See the `AGENTS.md` in
  `duplex-frontend`; check the bundled docs under `node_modules/next/dist/docs/` before writing
  Next-specific code.
