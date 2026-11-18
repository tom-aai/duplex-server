"""Duplex SDK — session broker (Piece 1).

The server-side broker that starts a full-duplex voice session.

This holds the project API key
and exchanges the browser's WebRTC SDP *offer* for an *answer* via the Live endpoint
(`POST /v1/live/sessions`, wrapped by the OpenAI SDK's ``client.live.create``). After
that exchange, audio flows **browser <-> model directly** over WebRTC. This server
never relays media.

Swap seam: the model id is read from ``DUPLEX_MODEL`` (default ``gpt-live-1``). When our
own model is ready, point that at it and the SDK shape should not change.

Run (from the repo root):

    uv run uvicorn duplex_server.broker:app --reload --port 8000
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import AsyncIterator, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from openai import AsyncOpenAI
from pydantic import BaseModel

from .logstream import LogStream
from .prompts import load_prompt
from .worker import SessionWorker

# --- config -----------------------------------------------------------------

# Load OPENAI_API_KEY from the repo-root .env.local (src/duplex_server/broker.py -> repo root).
REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env.local")

MODEL = os.getenv("DUPLEX_MODEL", "gpt-live-1")  # <- the clean-swap seam
# The prompts live in prompts.md. DUPLEX_INSTRUCTIONS overrides the default one.
DEFAULT_INSTRUCTIONS = os.getenv("DUPLEX_INSTRUCTIONS") or load_prompt("default_instructions")
# Added when delegation is on. Without it the model answers the time question itself.
DELEGATION_INSTRUCTIONS = " " + load_prompt("delegation_instructions")
# Browser origins allowed to call this broker (the Next.js dev server by default).
ALLOWED_ORIGINS = os.getenv("DUPLEX_ALLOWED_ORIGINS", "http://localhost:3000").split(",")

# --- app --------------------------------------------------------------------

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Duplex broker")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# Mirror the worker's log lines to any browser log pane. The handler attaches to the
# "duplex.worker" logger; the lines still reach the console too (the logger propagates).
log_stream = LogStream()
logging.getLogger("duplex.worker").addHandler(log_stream)

_client: AsyncOpenAI | None = None
workers: dict[str, SessionWorker] = {}


def client() -> AsyncOpenAI:
    """Lazily build the OpenAI client so import never fails without a key."""
    global _client
    if _client is None:
        if not os.getenv("OPENAI_API_KEY"):
            raise HTTPException(
                status_code=500,
                detail="OPENAI_API_KEY is not set. Add it to .env.local at the repo root.",
            )
        # max_retries=0: a WebRTC offer is single-shot; don't silently retry a stale SDP.
        _client = AsyncOpenAI(max_retries=0)
    return _client


class SessionRequest(BaseModel):
    sdp: str  # the browser's WebRTC SDP offer
    instructions: str | None = None
    # "client" = tools run in our sideband worker (2b). None = plain talk (Piece 1).
    delegation: Literal["client"] | None = None


class SessionResponse(BaseModel):
    session_id: str
    sdp: str  # the model's WebRTC SDP answer


def _dig(obj: object, *path: str) -> object | None:
    """Walk a nested SDK object or dict by key, returning None if any hop is missing."""
    cur: object | None = obj
    for key in path:
        if cur is None:
            return None
        cur = cur.get(key) if isinstance(cur, dict) else getattr(cur, key, None)
    return cur


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "model": MODEL}


@app.get("/logs")
async def logs() -> StreamingResponse:
    """Stream the worker's log lines to the browser log pane as Server-Sent Events.

    The browser opens one ``EventSource`` here. Each worker log line arrives as a ``data:``
    event, carrying the line as a JSON string. A new subscriber starts empty and receives
    only lines logged from now on, so opening or reloading the page reads as a fresh start.
    """
    queue = log_stream.subscribe()

    async def events() -> AsyncIterator[str]:
        try:
            yield ": connected\n\n"  # opening comment, so EventSource fires "open"
            while True:
                line = await queue.get()
                yield f"data: {json.dumps(line)}\n\n"
        finally:
            log_stream.unsubscribe(queue)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@app.post("/session", response_model=SessionResponse)
async def create_session(req: SessionRequest) -> SessionResponse:
    """Broker one browser session: SDP offer in, SDP answer out.
    """
    instructions = req.instructions or DEFAULT_INSTRUCTIONS
    config: dict[str, object] = {"model": MODEL}
    if req.delegation == "client":
        instructions += DELEGATION_INSTRUCTIONS
        config["delegation"] = {"type": "client"}
    config["instructions"] = instructions
    try:
        result = await client().live.create(
            session=config,  # type: ignore[arg-type]
            transport={"type": "webrtc", "sdp": req.sdp},
        )
    except HTTPException:
        raise
    except Exception as exc:  # surface the real upstream error to the dev
        raise HTTPException(status_code=502, detail=f"live.create failed: {exc}") from exc

    session_id = _dig(result, "session", "id")
    answer_sdp = _dig(result, "transport", "sdp")
    if not isinstance(session_id, str) or not isinstance(answer_sdp, str):
        raise HTTPException(
            status_code=502,
            detail=f"unexpected live.create response shape: {result!r}",
        )
    # 2a/2b: attach the sideband now, before the browser applies the answer.
    worker = SessionWorker(
        client(), session_id, delegate=req.delegation == "client",
        on_done=lambda sid: workers.pop(sid, None),
    )
    workers[session_id] = worker
    worker.start()
    return SessionResponse(session_id=session_id, sdp=answer_sdp)


@app.delete("/session/{session_id}")
async def close_session(session_id: str) -> dict[str, str]:
    """End one session's server-side worker.

    The browser calls this on disconnect. It detaches the sideband worker for good, so the
    worker does not thrash on reattach after the session ends (see ``SessionWorker.stop``).
    The browser's own ``session.close`` over the data channel ends the model call itself; a
    sideband detach is not a session end (see the serving contract, section 4).

    Idempotent: an unknown or already-closed session is not an error, so a double disconnect
    is safe.
    """
    worker = workers.pop(session_id, None)
    if worker is not None:
        await worker.stop()
    return {"status": "closed"}
