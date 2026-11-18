# The connection core

The heart of the starter. This is the code that gets a browser from "a user opens the page"
to "a live, two-way audio call with the model." A real developer would want an SDK to hide
this plumbing. Here we write it by hand instead, so we can see every step the SDK would have
to own.

Because a browser voice app spans two trust zones, the core has **two halves**:

| Half | Language | File | Job |
|---|---|---|---|
| **Server broker** | Python (FastAPI) | `src/duplex_server/broker.py` (this repo) | Holds the API key; turns a browser SDP offer into a live session. |
| **Browser client** | TypeScript | `lib/duplex.ts` (in `duplex-frontend`) | One `connect()` call that runs the whole WebRTC handshake and gives you a live session. |

They meet at one HTTP call: the browser POSTs its SDP offer to the broker's `/session`, and
the broker returns the model's SDP answer. Everything else (the audio) is a direct
`browser ↔ model` WebRTC connection the core sets up but does not sit in the middle of.

---

## The core: initializing the connection

This is the most important code in the starter today — getting from "a user opens the page"
to "a live, two-way audio connection with the model is open." We call it **Piece 1**.

### The handshake, end to end

```
1. connect()  getUserMedia(audio)                      → mic stream
2. connect()  new RTCPeerConnection + createDataChannel("oai-events")
3. connect()  createOffer / setLocalDescription        → local SDP offer
4. connect()  wait for ICE gathering (≤10s)
5. connect() ──POST /session { sdp, instructions? }──▶ broker
6. broker    ──client.live.create(session, transport)─▶ OpenAI (holds API key)
7. broker    ◀── { session.id, transport.sdp (answer) } ── OpenAI
8. connect() ◀── { session_id, sdp } ── broker
9. connect()  setRemoteDescription(answer)
10. connect() wait for "session.started" on the data channel
11. ✅ live — audio now flows browser ↔ model directly
```

No ephemeral token step: the browser never receives the API key, and the key never leaves the
broker. This mirrors GPT-Live-1's documented browser flow.

### Browser client — `connect()`

```ts
import { connect, type DuplexSession, type DuplexStatus } from "@/lib/duplex";

const session = await connect({
  instructions: "You are a terse, friendly assistant.", // optional; overrides broker default
  onStatus: (s) => console.log(s),                       // optional; lifecycle updates
});
// ... later
await session.disconnect();
```

**`connect(options?) → Promise<DuplexSession>`**

Options:
- `instructions?: string` — system prompt for the session. If omitted, the broker's
  `DUPLEX_INSTRUCTIONS` default is used.
- `onStatus?: (status: DuplexStatus) => void` — called as the connection progresses.

Returns a `DuplexSession`:
- `sessionId: string` — the model's opaque session id (preserved verbatim; it's what a future
  sideband connection will attach to).
- `disconnect(): Promise<void>` — sends `session.close`, waits (≤3s) for `session.closed`,
  then stops the mic, closes the peer connection, removes the audio element, and calls
  `DELETE /session/{id}` on the broker so the server-side worker is torn down too.

**Status lifecycle** (`DuplexStatus`):

```
requesting-mic → creating-session → connecting → live → closing → closed
```

**What `connect()` handles for you:**
- Microphone capture (`getUserMedia`).
- The `RTCPeerConnection`, the `oai-events` data channel (created **before** the offer, as
  required), and ICE gathering with a 10s cap.
- **Playback:** the model's audio track is routed into a hidden autoplaying `<audio>` element
  appended to the page, and removed on `disconnect()`.
- A **race-free** wait for `session.started` (the waiter is armed before the answer is applied).
- Cleanup on both success and failure (a failed `/session` call stops the mic and closes the
  connection before throwing).

This one function is the clearest example of the SDK question. Everything it does by hand is
work the SDK would do for you. Reading it is how we learn what that surface should be.

**Config:** the broker URL is `process.env.NEXT_PUBLIC_BROKER_URL`, defaulting to
`http://localhost:8000`.

### Server broker — `POST /session`

`src/duplex_server/broker.py` is a small FastAPI app.

**`POST /session`**
- Request: `{ "sdp": "<browser SDP offer>", "instructions": "<optional>" }`
- Response: `{ "session_id": "live_...", "sdp": "<model SDP answer>" }`
- Internally calls:
  ```python
  client.live.create(
      session={"model": MODEL, "instructions": ...},
      transport={"type": "webrtc", "sdp": offer},
  )
  ```
  with an OpenAI client built as `OpenAI(max_retries=0)` (a WebRTC offer is single-shot — we
  don't silently retry a stale SDP). Errors from OpenAI surface as HTTP `502` with the
  upstream message, so handshake failures are debuggable.

**`DELETE /session/{session_id}`**
- Ends one session's server-side worker: it detaches the sideband and stops the reattach loop
  for good, so a disconnect does not thrash on a dead session.
- The browser calls this from `disconnect()`. The model call itself is ended by the browser's
  `session.close` over the data channel; a sideband detach is not a session end (serving
  contract §4).
- Idempotent: an unknown or already-closed session returns `200 { "status": "closed" }`, so a
  double disconnect is safe.

**`GET /health`** → `{ "status": "ok", "model": "<DUPLEX_MODEL>" }` — a quick liveness/config check.

**Run it** (from this repo's root):
```bash
uv run uvicorn duplex_server.broker:app --reload --port 8000
```

### The swap seam

The broker reads the model id from `DUPLEX_MODEL` (default `gpt-live-1`). When our own
full-duplex model is ready, point `DUPLEX_MODEL` at it. This single seam is how we keep the
GPT-Live-1 stand-in from leaking through the rest of the code, and it is one of the things a
real SDK would formalize.

---

## The sideband worker: reconnect and the disconnect tool

For each session the broker starts one `SessionWorker` (`src/duplex_server/worker.py`). It attaches
a control-only WebSocket to the running session (`/v1/live/sessions/{id}/attach`). Audio never
passes through it. The worker reads transcripts, and on a client delegation it runs the brain
and speaks the result.

The brain has two tools, `get_time` and `get_weather`, which return data. The worker also
demonstrates the steering primitives from the [serving contract](./MODEL_SERVING_CONTRACT.md#6-steering-what-the-server-sends-back):
a weather lookup records the place with `thinking.append` as silent context, and a time check
sets an irritable mood with `instructions.append`. Every message the worker sends back over the
sideband is logged to the `duplex.worker` logger. The broker mirrors that logger to the browser
over `GET /logs` (Server-Sent Events), so the app's left pane shows the same lines as the
console. The log line format is the same in both places (`-> commentary.append ...`,
`-> thinking.append ...`), next to the delegation request and the tool results, so one delegation
reads top to bottom in the log.

### We drive the reconnect, not the SDK

The OpenAI SDK has its own reconnect. You turn it on by passing `on_reconnecting` to
`sideband.connect(...)`; the SDK then re-attaches a dropped socket with its own backoff. **We
do not use it.** The worker leaves `on_reconnecting` out, so the SDK does not reconnect, and
drives its own attach loop in `SessionWorker._run`:

```python
while True:
    async with self.client.live.sideband.connect(session_id=self.session_id) as conn:
        self.conn = conn
        async for event in conn:
            await self._handle(conn, event)
    break  # clean close (code 1000): session ended
```

A drop raises out of the `async for`; the loop backs off and re-attaches to the **same**
session id, so the voice call keeps running across the gap. We own the policy:

- `SIDEBAND_MAX_RETRIES` (10): reattach attempts after a drop before we give up.
- `SIDEBAND_INITIAL_DELAY` / `SIDEBAND_MAX_DELAY`: the backoff window.
- `_RECOVERABLE_CLOSE_CODES`: which close codes are a drop worth retrying. A normal close
  (1000) is not one, so a real session end stops the loop at once.

Session state (`input_text`, the brain's `messages`, `irritable`) lives on the worker, not on
the socket, so it survives a reattach. The cost of leaving the SDK's version: a fresh
`connect()` starts an empty send queue, so a `commentary.append` queued *during* a drop is
lost. We only send during a delegation, so a drop mid-delegation loses that one answer either
way.

Owning the reconnect by hand is deliberate. It shows exactly which part of the job a real SDK
would have to take over, and which policy knobs it would have to expose.

---

## Configuration

Loaded by the broker from `.env.local` at the repo root (and the process env):

| Variable | Side | Default | Purpose |
|---|---|---|---|
| `OPENAI_API_KEY` | broker | — (required) | Auth for the Live endpoint. Stays server-side. |
| `DUPLEX_MODEL` | broker | `gpt-live-1` | The voice model id — the swap seam. |
| `DUPLEX_BRAIN_MODEL` | worker | `gpt-6-luna` | The backend "brain" LLM that picks tools. |
| `DUPLEX_INSTRUCTIONS` | broker | friendly-assistant prompt | Default system prompt. |
| `DUPLEX_ALLOWED_ORIGINS` | broker | `http://localhost:3000` | Comma-separated CORS allowlist. |
| `NEXT_PUBLIC_BROKER_URL` | browser | `http://localhost:8000` | Where `connect()` posts the offer. |

---

## What's not built yet

Deliberately out of scope for Piece 1 — tracked for the next steps:

- **Sideband control (Piece 2).** A server-side WebSocket attached to the running session
  (`wss://.../v1/live/sessions/{session_id}/attach`) so **tool calls and verify-before-speak
  run on a server you control**, not in the browser. `sessionId` from `connect()` is the hook.
- **Delegation to the brain model** _(code in place in `src/duplex_server/worker.py`; not yet run
  against the live API)._ Each delegation calls our own LLM brain, **OpenAI `gpt-6-luna`**
  (the `DUPLEX_BRAIN_MODEL` seam), passes it the tool schemas, and lets the model choose which
  tool to call. The worker runs the chosen tool server-side and speaks the result.
- **Event surface.** Exposing transcripts (`session.{input,output}_transcript.delta`) and
  tool-call events to the application.
- **Ergonomics (Piece 3).** Packaging the browser client as its own module, typed errors, etc.

Each of these is also a line in [`MODEL_SERVING_CONTRACT.md`](./MODEL_SERVING_CONTRACT.md):
something our model's serving endpoint must expose, and so something our SDK will have to
handle.
