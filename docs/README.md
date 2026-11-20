# Duplex — documentation

Duplex is a **starter app** for building **full-duplex voice apps**, where the model listens
and speaks at the same time. It is a learning project, not an SDK. You run it, read it, and
change it. There is nothing to install and nothing to import.

We build against **OpenAI GPT-Live-1.1** for now. GPT-Live-1.1 stands in for our own full-duplex
model. When our model is ready it drops in where GPT-Live-1.1 is today.

Building this by hand is how we learn what our own SDK will need. Every piece we write (the
session handshake, the reconnect loop, the hand-off to a backend brain) shows one more job a
real SDK would have to handle for the developer. The
[model serving contract](./MODEL_SERVING_CONTRACT.md) is where we write those findings down.

> **New here?** Go to **[Getting started](./getting-started.md)** — run the app and talk to
> the model in a few minutes.

## Contents

| Doc | What's in it |
|---|---|
| **[The connection core](./connection-core.md)** ← most important | The server broker and the browser client, and a deep dive on how the connection starts (`connect()` ↔ `/session`). |
| [Frontend](./frontend.md) | The Next.js app that drives the server end to end. |
| [Getting started](./getting-started.md) | Prerequisites, env setup, and running both servers. |
| [Model serving contract](./MODEL_SERVING_CONTRACT.md) | What we have learned the serving endpoint must expose, and so what our SDK must handle. |

## The shape in one screen

```
BROWSER (duplex-frontend)    BROKER (duplex-server)        OpenAI
 connect() ──SDP offer──▶  POST /session ──live.create──▶ gpt-live-1.1
           ◀── SDP answer + session_id ──                 │
           ◀═══════════ direct WebRTC duplex audio ═══════╝
```

- The **broker** does session setup only (it holds the API key); it **never relays audio**.
- After the handshake, audio is a **direct `browser ↔ model` WebRTC** connection — OpenAI's
  media plane carries it, so we don't operate one yet.
- The model id is a **single swap seam** (`DUPLEX_MODEL`, default `gpt-live-1.1`).

## Repo layout

Two starter repos: a Python server and a Next.js frontend.

```
duplex-server/            this repo — the Python server
  src/duplex_server/             the session broker (FastAPI) + the sideband worker  → see connection-core.md
  docs/                this documentation
  .env.example         template for .env.local (OPENAI_API_KEY)
  render.yaml          Render blueprint for the broker

duplex-frontend/       separate repo — the Next.js app                     → see frontend.md
  lib/duplex.ts        the browser client (connect())
  app/page.tsx         the UI
  render.yaml          Render blueprint for the frontend
```

## Current status

**Piece 1 — connect & talk — is built.** You can open the app, click Connect, and hold a
live full-duplex conversation. The sideband brain (server-side tools, with OpenAI `gpt-6-luna`
choosing the tool) is wired in code but not yet run against the live API; verify-before-speak
is still to come. See [The connection core § What's not built yet](./connection-core.md#whats-not-built-yet).

## What this teaches the SDK

The starter is the research. As we build each piece against GPT-Live-1.1, we learn one more
thing a real SDK would have to carry for the developer: the no-key browser handshake, the
own-your-reconnect loop, the delegation hand-off, verify-before-speak. The
[model serving contract](./MODEL_SERVING_CONTRACT.md) collects those findings as a list of
what the serving endpoint must expose. That list is the first sketch of what our SDK must
handle, and of where a provider's model and our own could differ.
