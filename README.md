# Duplex (server)

Duplex is a starter app for building a full-duplex voice app. Full-duplex means the model
listens and speaks at the same time, the way people do in a real conversation.

This is a learning project, not an SDK. There is no library to install and no `import`. You
run this app, read it, and change it. We write the hard parts by hand in plain Python and
TypeScript so the shape of the problem stays easy to see: the WebRTC and WebSocket plumbing,
the session handshake, the reconnect loop, and the hand-off to a backend "brain."

The goal is to learn what our own SDK will need later. Each piece we build here shows one
more job a real SDK would have to do for the developer. We write those findings down in the
[model serving contract](./docs/MODEL_SERVING_CONTRACT.md). So read this repo as a worked
example of the problem, and as the source material for the SDK we have not designed yet.

We build each piece one at a time, on purpose, so every part stays readable.

## How it fits together

```
  ┌──────────────┐                                   ┌──────────────┐
  │    CLIENT    │ ◀═══════ live audio (duplex) ════▶ │    MODEL     │
  │  (browser)   │                                   │              │
  └──────┬───────┘                                   │ duplex voice │
         │                                           │    model.    │
         │ start                                     │  served by   │
         │ the call                                  │  GPT-Live-1  │
         ▼                                           │   for now    │
  ┌────────────────────────────┐                     │              │
  │           SERVER           │ ◀══ control ══════▶ │              │
  │     (our Python app)       │      channel        │              │
  │                            │                     └──────────────┘
  │  1. sets up the call,      │
  │     and reconnects         │
  │  2. the worker: the brain  │
  │     that does the work     │
  │  3. holds the prompt       │
  └────────────────────────────┘
```

There are three pieces.

**The client.** This is the browser. It captures the microphone. It plays the model's
voice. It sends one request to the server to start the call. After that, its audio goes
straight to the model.

**The model.** This is the duplex voice model. It listens and it speaks. It does nothing
else. When it needs real work done, it hands that work off. Right now we use OpenAI
GPT-Live-1 as a stand-in for our own model. The model runs on the provider's servers, not
on ours.

**The server.** This is our own Python app. It does three jobs:

1. **It sets up the call, and it reconnects.** The browser cannot hold the API key, so the
   server does the handshake and keeps the key. The server also runs its own reconnect
   loop. A dropped control link comes back, and the call keeps running across the gap.
2. **It runs the worker.** The worker is the brain. The voice model hands every real task
   to it. The worker picks the tool, runs the tool, and gives the model the words to speak.
3. **It holds the prompt.** The system prompt that shapes the model lives on the server,
   not in the browser.

The audio never passes through the server. Audio is a direct link between the browser and
the model. The server sets the call up, and then it steers the call over a separate control
link.

## The two starter repos

The frontend lives in its own repo so you can clone and deploy it on its own. Both repos
are starters: working code you run and edit, not packages you depend on.

- **`duplex-server`** (this repo) — the Python server: the broker and the worker.
- **`duplex-frontend`** — the Next.js app and the browser client (`lib/duplex.ts`).

They meet at one HTTP call and two environment variables:

| Variable | Set on | Purpose |
|---|---|---|
| `NEXT_PUBLIC_BROKER_URL` | frontend | Where the browser posts its WebRTC offer. |
| `DUPLEX_ALLOWED_ORIGINS` | this server | The CORS allowlist. Must include the frontend's origin. |

Locally the defaults match (`:3000` → `:8000`), so there is nothing to set. When you deploy,
each app needs the other's real URL. See [Deploy on Render](#deploy-on-render).

## Run it

You need two terminals.

First, put your API key in `.env.local` at the repo root. Copy the template:

```bash
cp .env.example .env.local      # then edit it and add your key
```

**Terminal 1 — the server** (the broker and the worker), from this repo's root:

```bash
uv sync                                          # first time only, installs deps
uv run uvicorn duplex_server.broker:app --reload --port 8000
```

Check it: `curl http://localhost:8000/health` returns `{"status":"ok","model":"gpt-live-1"}`.

**Terminal 2 — the web app**, in the `duplex-frontend` repo:

```bash
cd ../duplex-frontend
npm install    # first time only
npm run dev    # serves http://localhost:3000
```

Then open **http://localhost:3000**, click **Connect & talk**, allow the microphone, and
start talking. The model listens and replies in real time.

## Deploy on Render

A [`render.yaml`](./render.yaml) blueprint ships in each repo. In Render, pick
**New > Blueprint** for each. Then set the two URLs so the apps find each other:

- On this server, set `DUPLEX_ALLOWED_ORIGINS` to the frontend's deployed URL.
- On the frontend, set `NEXT_PUBLIC_BROKER_URL` to this server's deployed URL (it is inlined at
  build time, so set it before the build runs).

## Where the code lives

| Piece | Language | File |
|---|---|---|
| Server broker (starts the session) | Python | `src/duplex_server/broker.py` |
| Sideband worker (the brain, and reconnect) | Python | `src/duplex_server/worker.py` |
| Prompts | Markdown | `src/duplex_server/prompts.md` |
| Browser client (`connect()`) | TypeScript | `lib/duplex.ts` (in `duplex-frontend`) |

## Learn more

- [`docs/connection-core.md`](./docs/connection-core.md) — the connection core: the broker and the browser client.
- [`docs/getting-started.md`](./docs/getting-started.md) — run the app end to end.
- [`docs/MODEL_SERVING_CONTRACT.md`](./docs/MODEL_SERVING_CONTRACT.md) — what we have learned
  the model's serving endpoint must expose, and so what our SDK must handle.
