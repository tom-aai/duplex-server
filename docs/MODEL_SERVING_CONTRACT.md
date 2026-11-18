# Model Serving Contract

We build the starter app against OpenAI GPT-Live-1 for now. GPT-Live-1 stands in for our own
full-duplex model, so the starter leans on GPT-Live-1 behavior. This doc writes down the
behaviors the app relies on. They are two things at once: what our own serving endpoint must
expose, and what our future SDK must handle on top of it. Any place our model behaves
differently is a place the serving endpoint and the SDK will have to absorb the difference.

This is the main thing the starter produces. Each piece we build by hand adds a line here, so
the contract grows into the first real spec for both the model's endpoint and the SDK around it.

**Reference shape:** OpenAI GPT-Live-1. The model id is `gpt-live-1`. The endpoint base is
`/v1/live`. 

## The shape we assume

```
Browser  ◀═══ duplex audio over WebRTC ═══▶  Model
Browser  ──── SDP offer ───▶  Server  ──── create session ───▶  Model
Server   ◀═══ control channel (sideband) ═══▶  Model
```

The browser and the model carry the audio. The server sets the call up. Then the server
steers the call over a separate control channel. The server never carries the audio.

## What the serving endpoint must expose

### 1. Start a session (server-side)

- The server sends one HTTP request. It carries the session config and a WebRTC SDP offer
  from the browser. A server-held project key authenticates it.
- The endpoint returns the session id and a WebRTC SDP answer.
- GPT-Live shape: `POST /v1/live/sessions` returns
  `201 { "session": {"id": ...}, "transport": {"type": "webrtc", "sdp": "<answer>"} }`.
- The browser never gets a long-lived secret. The server holds the key. The server does the
  handshake. There is no ephemeral-token step.
- The broker calls `client.live.create(session=config, transport={"type":"webrtc","sdp": offer})`.
  It does not retry a failed create. A WebRTC offer works only once. A stale offer must fail
  loud, not retry.

### 2. Direct duplex audio (the media plane)

- After the SDP exchange, the audio flows between the browser and the model directly over
  WebRTC.
- Both directions run at the same time. The model listens and speaks at the same time.
- The server never carries the audio.
- Our serving stack must terminate WebRTC and run the duplex model behind the endpoint.
- This media plane is the model team's work. We mock it with GPT-Live-1 for now.

### 3. The event and data channel

- The model and the browser share a JSON event channel next to the audio. GPT-Live labels
  its data channel `oai-events`.
- Lifecycle events the SDK needs:
  - `session.started`: the session is ready. The SDK waits for this before it reports "live".
  - `session.close`: the app asks to end the session.
  - `session.closed`: the session has ended.

### 4. A server-side control channel (the sideband)

- The server attaches a second connection to a running session. It carries control events
  only. No audio passes through it.
- GPT-Live shape: `wss://.../v1/live/sessions/{id}/attach`, with bearer auth (the project
  key).
- Over this channel the server reads events and sends steering.
- A detach is not a session end. When the server closes the sideband, the session keeps
  running. The model keeps listening and speaking. Only `session.close` ends the call.
- The serving side must accept a fresh attach to the same running session after the sideband
  drops. This is what lets the SDK run its own reconnect (see 8).
- A session with no sideband still emits delegation events. With nothing attached to answer
  them, those turns get no result.

### 5. Delegation (the hand-off to the brain)

The voice model does the talking only. It hands every real task to a backend. We call that
backend the brain. We run the brain ourselves on OpenAI `gpt-6-luna`.

- The model emits `session.delegation.created` when it wants work done. The event carries a
  delegation id and a target. In client-delegation mode the target is `client`.
- The SDK turns this mode on in the session config: `"delegation": {"type": "client"}`.
- The delegation event carries no task text. It carries only the
  id. The server must read the task some other way. Our worker reads it from the input
  transcript (see 7).
- After it delegates, the model pauses. It waits for the server to answer that delegation id.
- Client delegation has no function-call items. `response.item.create` and `response.create`
  belong to a different mode (Responses delegation). In client mode the server answers by
  appending commentary (see 6), tied to the delegation id.
- The model must hand off and wait. The server runs the tools. The server supplies the words
  to speak.

### 6. Steering (what the server sends back)

The server shapes the call over the sideband. Each call names the delegation id it belongs
to.

- `session.commentary.append`: give the model words to speak now. This is how the server
  answers a delegation.
- `session.instructions.append`: add a new instruction. It stays in the session history. The
  new behavior lasts for the rest of the call.
- `session.thinking.append`: add silent context. The model keeps the fact, but it does not
  say the fact out loud. Append empty content here to suppress the model's
  automatic "let me check" acknowledgement before the brain answers.
- `session.input_audio.mute`: mute the microphone input.
- **Ack control.** GPT-Live speaks an automatic acknowledgement when a delegation starts. The
  serving side must let the server delay it (`acknowledgement_delay_ms`) and suppress it.
  These primitives are what make verify-before-speak possible.
- Each delegation has exactly one owner. We make the server the owner. The browser does audio
  only.

### 7. Transcripts

- The model emits input and output transcript deltas as events:
  `session.input_transcript.delta` and `session.output_transcript.delta`.
- Each delta carries its text. GPT-Live also carries `start_ms` and `end_ms`.
- The server reads the input transcript to recover the task text for a delegation (see 5).

### 8. Reconnect to a running session

- The server runs its own reconnect loop. It re-attaches a dropped sideband to the same
  session id.
- The worker does not use the provider SDK's built-in reconnect. The worker owns the backoff
  and the attempt cap, so the session survives the gap. Session state lives on the server, not
  on the socket, so it survives a re-attach.
- The serving side must tell a drop from a real end by the WebSocket close code:
  - Code 1000 (normal close) means the session ended. The worker stops.
  - Other codes mean a drop worth a re-attach.
- The same session id must stay valid for a re-attach while the call runs.

### 9. Auth and limits

- A server-held project credential creates sessions and attaches the sideband.
- The serving side must publish its concurrency limits, so capacity is predictable. GPT-Live
  measures these in concurrent sessions (tiers 50 / 300 / 500).
- The serving side must return typed errors. The SDK relies on clear error types (for example
  `immutable_field_update`) and on a stated session expiry.

## Out of scope for the SDK (the serving side owns these)

- The media plane: WebRTC and WebSocket termination, NAT traversal and TURN, packet-loss and
  jitter recovery.
- Model inference: turn-taking, interruption, voice activity detection.
- Telephony and SIP. The SDK is browser-only for now.

## Decisions the serving team must make

These do not come free from GPT-Live. Our model can choose differently.

- **Output VAD.** GPT-Live does not detect when the model's own speech starts or stops. This
  shapes verify-before-speak. Decide whether our model provides output VAD. If it does, the
  SDK's verify design changes.
- **In-session delegation change.** GPT-Live forbids a switch from plain talk to delegation in
  the middle of a call. Decide whether our model allows it.
- **Event names and the delegation protocol.** Decide whether our model matches the GPT-Live
  event names and the delegation protocol one for one, or defines its own. This choice sets
  how clean the swap really is.
- **Interruption and barge-in.** The model must emit barge-in events, and it must honor "stop
  talking" in the middle of an utterance. This is the hardest full-duplex requirement. It is
  the one most likely to differ for our model.
