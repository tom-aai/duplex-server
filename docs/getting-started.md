# Getting started

Run the starter and hold a live full-duplex conversation with the model. ~5 minutes.

## Prerequisites

- **Node.js** (18+) and **npm** — for the Next.js frontend.
- **[uv](https://docs.astral.sh/uv/)** — for the Python broker (manages its own Python 3.12).
- An **OpenAI API key** with GPT-Live access.
- A **microphone** and a modern browser (Chrome/Edge/Safari).

The frontend is a separate repo (`duplex-frontend`). This guide assumes it sits next to this
one, so the broker is `duplex-server/` and the frontend is `duplex-frontend/`.

## 1. Add your API key

In this repo (`duplex-server`), copy the template and add your key to `.env.local` (gitignored):

```bash
cp .env.example .env.local      # then edit it
```

```
OPENAI_API_KEY=sk-...
```

Optional overrides (defaults shown) — see the [config table](./connection-core.md#configuration):

```
# DUPLEX_MODEL=gpt-live-1.1
# DUPLEX_ALLOWED_ORIGINS=http://localhost:3000
```

## 2. Start the broker (terminal 1)

From this repo's root:

```bash
uv run uvicorn duplex_server.broker:app --reload --port 8000
```

Check it: `curl http://localhost:8000/health` → `{"status":"ok","model":"gpt-live-1.1"}`.

## 3. Start the frontend (terminal 2)

In the `duplex-frontend` repo:

```bash
cd ../duplex-frontend
npm install                     # first time only
cp .env.local.example .env.local  # default points at http://localhost:8000
npm run dev                     # serves http://localhost:3000
```

## 4. Talk

1. Open **http://localhost:3000**.
2. Click **Connect & talk**.
3. Allow microphone access.
4. When the status reads **live**, start talking — the model listens and replies in real time.
5. Click **Disconnect** when done.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `OPENAI_API_KEY is not set` (HTTP 500 from `/session`) | Key missing from `.env.local`, or broker started from the wrong directory. Restart the broker from `duplex-server/`. |
| `broker /session failed: 502 ...` | OpenAI rejected the handshake; the 502 detail carries the upstream message (bad model access, malformed SDP, etc.). |
| CORS error in the browser console | Frontend origin not in `DUPLEX_ALLOWED_ORIGINS`. Add it and restart the broker. |
| Mic prompt never appears / `getUserMedia` fails | Page must be on `localhost` or HTTPS; grant mic permission in the browser. |
| Stuck at `connecting` (no `session.started`) | Network/ICE issue reaching OpenAI, or the broker returned a non-WebRTC answer. Check the broker logs. |

## What you just ran

Two starter repos talking over one HTTP call. To see how the handshake works, read
**[The connection core](./connection-core.md)** for `connect()` and the broker, and
[frontend.md](./frontend.md) for the web app.
