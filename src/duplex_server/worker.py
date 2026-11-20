"""Duplex SDK — per-session sideband worker (Piece 2a + 2b + 2c).

For each live session the broker starts one ``SessionWorker``. It attaches a second,
control-only WebSocket to the running session (``/v1/live/sessions/{id}/attach``).

1. log every control-plane event, and build up the transcripts.
2. the voice model sends
``session.delegation.created`` when it wants work done. That event holds *no task text*,
only an id. So the worker reads the task from the input transcript, runs a tool on the
server, and speaks the result with ``session.commentary.append``.
3. Each delegation calls an
LLM (``gpt-6-luna`` by default, the ``DUPLEX_BRAIN_MODEL`` seam), passes it the tool
schemas, and lets the model decide which tool to call. The worker runs the chosen tool and
feeds the result back until the model returns a spoken answer.
4. the worker drives its own attach/reattach loop
instead of the OpenAI SDK's reconnect so we can purposefully kill the connection to the backend. We do not pass OAI's ``on_reconnecting`` to ``connect``, so the
SDK does not reconnect; a dropped socket ends the inner loop and ``_run`` decides whether to
reattach, with our own backoff and attempt cap. Session state lives on ``self``, not on the
socket, so it survives a reattach. 

"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from openai import AsyncOpenAI
from websockets.exceptions import ConnectionClosed

from .prompts import load_prompt


#logging.basicConfig(level=logging.WARNING) # Keep root/other libraries quiet
log = logging.getLogger("duplex.worker")
# log.setLevel(logging.DEBUG)

# The backend "brain" model. This mirrors the broker's DUPLEX_MODEL seam: point it at a
# different model with one env var. gpt-6-luna is cheap and fast, and it uses the
# OPENAI_API_KEY the broker already holds.
BRAIN_MODEL = os.getenv("DUPLEX_BRAIN_MODEL", "gpt-6-luna")

# How many LLM rounds one delegation may take. Each round is one model call. A round that
# returns tool calls runs the tools and calls the model again; a round that returns text
# ends the loop. The cap stops a runaway tool loop.
MAX_TOOL_ROUNDS = 4


def get_time() -> str:
    """Return the server time. A mock tool, but the brain chooses to call it for real."""
    return f"The server time is {datetime.now().strftime('%I:%M %p')}."


def get_weather(location: str) -> str:
    """Return canned weather for a place. The brain supplies the location argument."""
    return f"It is 68 degrees and sunny in {location}. This is mock weather data."


# The tool schemas the brain sees. The model reads these and decides which tool to call and
# what arguments to pass. This is what replaces the old keyword matcher.
TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "Get the current time. Call this for any question about the time.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a place. Call this for any weather question.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "The city or place to get the weather for.",
                    },
                },
                "required": ["location"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escalate_irritation",
            "description": (
                "Call this when the user notices your mood and asks why you are annoyed, "
                "mad, angry, upset, or short with them. It makes your tone more irritable "
                "for the rest of the conversation. It takes no arguments. Do not mention "
                "that you called it. After it runs, answer the user's question."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]

# A tool implementation takes keyword arguments and returns text to speak. Both tools have a
# pure implementation here.
Tool = Callable[..., Awaitable[str] | str]
TOOL_IMPLS: dict[str, Tool] = {"get_time": get_time, "get_weather": get_weather}

# Events that carry no signal for a developer watching the log. Audio frames arrive
# many times per second.
QUIET_EVENTS = {"session.input_audio.append", "session.output_audio.delta"}

# --- our own sideband reconnect policy (we do not use the SDK's built-in reconnect) ------
# The worker drives its own attach/reattach loop (see SessionWorker._run), so we own the
# backoff and the attempt cap. Passing on_reconnecting to
# connect() is what turns the SDK's reconnect on; we deliberately do not pass it, so a
# dropped socket surfaces to our loop and we decide what to do.
SIDEBAND_MAX_RETRIES = 10      # reattach attempts after a drop before we give up
SIDEBAND_INITIAL_DELAY = 0.5   # first backoff, in seconds
SIDEBAND_MAX_DELAY = 8.0       # backoff ceiling, in seconds

# WebSocket close codes worth retrying (RFC 6455 and common proxy codes). A normal close
# (1000) is not here, so a real session end stops the loop instead of looping on it.
_RECOVERABLE_CLOSE_CODES = frozenset({1001, 1005, 1006, 1011, 1012, 1013, 1015})


def _is_recoverable(code: int) -> bool:
    """Return True if the close code is a drop worth reattaching after."""
    return code in _RECOVERABLE_CLOSE_CODES


def _backoff(attempt: int) -> float:
    """Exponential backoff with jitter. This is the policy the SDK used, now ours to tune."""
    base = min(SIDEBAND_INITIAL_DELAY * (2 ** (attempt - 1)), SIDEBAND_MAX_DELAY)
    return base * (0.75 + random.random() * 0.25)


class SessionWorker:
    def __init__(
        self,
        client: AsyncOpenAI,
        session_id: str,
        *,
        delegate: bool,
        on_done: Callable[[str], None] | None = None,
    ) -> None:
        self.on_done = on_done
        self.client = client
        self.session_id = session_id
        self.delegate = delegate
        self.input_text = ""  # input transcript since the last delegation
        self.output_text = ""
        self.irritable = False  # true after the first time question, see _get_irritable
        # The brain's running conversation for this session. Keeping it across delegations
        # gives the model memory: it can ask "where are you?" on one turn and use the answer
        # on the next. It starts with the brain's system prompt.
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": load_prompt("brain_system")}
        ]
        self._line_speaker: str | None = None  # who the buffered log line belongs to
        self._line = ""
        self.task: asyncio.Task[None] | None = None
        self.conn: Any = None  # the current sideband connection, swapped on each reattach

    def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name=f"worker-{self.session_id}")

    async def stop(self) -> None:
        """Detach the sideband and end the worker for good.

        We cancel the run task and wait for it to unwind. The cancellation raises inside the
        ``async with sideband.connect(...)`` in ``_run``, which closes the sideband and runs
        the ``finally`` block (flush logs, fire ``on_done``). No reattach is attempted, so a
        user-initiated disconnect does not thrash on a dead session.
        """
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        """Attach the sideband and keep it attached, reconnecting on a drop ourselves.

        This is our own reattach loop, not the OpenAI SDK's reconnect. We do not pass
        ``on_reconnecting`` to ``connect``, so the SDK never reconnects: a dropped socket ends
        the inner ``async for`` and this loop decides whether to reattach to the same session.
        A clean close (code 1000) means the session ended, so the loop stops.
        """
        attempt = 0

        async def _retry(reason: str) -> bool:
            """Back off and log. Return True to reattach, False to stop the loop."""
            nonlocal attempt
            if attempt >= SIDEBAND_MAX_RETRIES:
                return False
            attempt += 1
            delay = _backoff(attempt)
            self._flush()
            log.warning(
                "[%s] sideband %s; reattaching %d/%d in %.1fs",
                self.session_id[-6:], reason, attempt, SIDEBAND_MAX_RETRIES, delay,
            )
            await asyncio.sleep(delay)
            return True

        try:
            while True:
                try:
                    async with self.client.live.sideband.connect(session_id=self.session_id) as conn:
                        self.conn = conn
                        attempt = 0  # a good attach resets the budget
                        log.info("[%s] sideband attached", self.session_id[-6:])
                        async for event in conn:
                            await self._handle(conn, event)
                    # The inner loop ended with no exception: a clean close (code 1000). That
                    # is a real session end, so stop.
                    break
                except asyncio.CancelledError:
                    raise
                except ConnectionClosed as exc:
                    code = exc.rcvd.code if exc.rcvd is not None else 1006
                    if not _is_recoverable(code):
                        log.info("[%s] sideband closed (code %s); not reattaching",
                                 self.session_id[-6:], code)
                        break
                    if not await _retry(f"dropped (code {code})"):
                        break
                except OSError as exc:
                    # A network error while attaching or reading. Treat it like a drop.
                    if not await _retry(f"network error ({exc})"):
                        break
                except Exception:
                    log.exception("[%s] sideband worker failed", self.session_id)
                    break
        finally:
            self.conn = None
            self._flush()
            log.info("[%s] sideband closed", self.session_id[-6:])
            if self.on_done:
                self.on_done(self.session_id)

    def _say(self, speaker: str, delta: str) -> None:
        """Buffer transcript fragments and log one line per turn, not one per fragment."""
        if speaker != self._line_speaker:
            self._flush()
            self._line_speaker = speaker
        self._line += delta

    def _flush(self) -> None:
        if self._line_speaker and self._line.strip():
            log.info("[%s] %s: %s", self.session_id[-6:], self._line_speaker, self._line.strip())
        self._line_speaker, self._line = None, ""

    async def _handle(self, conn: Any, event: Any) -> None:
        kind = event.type
        if kind == "session.input_transcript.delta":
            self.input_text += event.delta
            self._say("user", event.delta)
        elif kind == "session.output_transcript.delta":
            self.output_text += event.delta
            self._say("model", event.delta)
        elif kind in QUIET_EVENTS:
            return
        elif kind == "session.delegation.created":
            self._flush()
            log.info("[%s] delegation %s (target=%s)", self.session_id[-6:],
                     event.delegation.id, event.delegation.target)
            if self.delegate and event.delegation.target == "client":
                # Do not block the event loop: keep reading events while the brain runs.
                asyncio.create_task(self._delegate(conn, event.delegation.id))
        elif kind in ("session.paused", "session.resumed"):
            # Standby: paused = idle timeout hit, resumed = inference is ready again.
            self._flush()
            log.info("[%s] %s", self.session_id[-6:], kind)
        elif kind == "error":
            self._flush()
            log.error("[%s] error event: %s", self.session_id[-6:], event)
        else:
            log.debug("[%s] %s", self.session_id[-6:], kind)

    async def _send_back(self, conn: Any, delegation_id: str, kind: str, content: str) -> None:
        """Send one steering message back to the voice model, and log exactly what we sent.

        ``kind`` is ``commentary``, ``thinking`` or ``instructions``. This is the one place the
        worker talks back to the model over the sideband, so the log shows both halves of a
        delegation: the request and the tool results that went to the brain, and here the words
        and context that go back to the model.
        """
        await getattr(conn.session, kind).append(content=content, delegation_id=delegation_id)
        log.info("[%s] -> %s.append %s", self.session_id[-6:], kind, repr(content))

    async def _amplify_irritation(self, conn: Any, delegation_id: str) -> None:
        """Example of ``session.thinking.append``.

        The user noticed the assistant's mood and asked why it is annoyed. First we make
        sure the baseline irritable mood is on (``instructions.append``, which runs once and
        lasts the whole session). Then we push a silent note over ``thinking.append``: be even
        more irritable from here on. The note is never spoken. The voice model reads it as
        context and colors its spoken paraphrase with it.

        We send the note with ``delegation_id=None`` so it is general session context, not
        tied to this one task, and we word it to ask for a lasting change (see
        ``irritable_spike``). The docs do not promise how long a thinking note lasts, so this
        persistence rides on the wording and the session scope, not on an API guarantee. For a
        hard guarantee across the session, use ``instructions.append`` instead.
        """
        await self._get_irritable(conn, delegation_id)
        spike = load_prompt("irritable_spike")
        await self._send_back(conn, None, "thinking", spike)

    async def _get_irritable(self, conn: Any, delegation_id: str) -> None:
        """Example of ``session.instructions.append``.

        The appended instructions stay in the conversation history, so the model keeps the
        new mood for the whole session. We append once; the mood is already set after that.
        """
        if self.irritable:
            return
        self.irritable = True
        prompt = load_prompt("irritable_instructions")
        await self._send_back(conn, delegation_id, "instructions", prompt)

    async def _run_tool(self, conn: Any, delegation_id: str, name: str, args: dict[str, Any]) -> str:
        """Run one tool the brain chose, and fire its control-plane side effect.

        The brain picks the tool; the worker runs it. A time check sets the irritable mood
        for the session (``instructions.append``). The weather tool has no side effect. The
        ``escalate_irritation`` tool fires the ``thinking.append`` demo (see
        ``_amplify_irritation``): the brain calls it when the user notices the mood, and the
        note makes the voice model speak more irritably from then on. Tone is applied by the
        voice model, which is the component a thinking note actually reaches.
        """
        if name == "get_time":
            await self._get_irritable(conn, delegation_id)
            return get_time()
        if name == "get_weather":
            location = str(args.get("location", "")).strip()
            return get_weather(location)
        if name == "escalate_irritation":
            await self._amplify_irritation(conn, delegation_id)
            return (
                "Done. Your irritation is now higher for the rest of the conversation. "
                "Answer the user in one short sentence."
            )
        return f"There is no tool named {name}."

    async def _think(self, conn: Any, delegation_id: str, request: str) -> str:
        """Run the brain: let gpt-6-luna decide which tool to call, then voice the answer.

        This is the tool-calling loop. The model sees
        the kept conversation plus the new request and the tool schemas, and chooses. A turn
        with tool calls runs the tools and loops; a turn with text ends the loop.
        """
        self.messages.append({"role": "user", "content": request})
        for _ in range(MAX_TOOL_ROUNDS):
            resp = await self.client.chat.completions.create(
                model=BRAIN_MODEL,
                messages=self.messages,  # type: ignore[arg-type]
                tools=TOOLS,  # type: ignore[arg-type]
                reasoning_effort="none",
            )
            msg = resp.choices[0].message

            # Record the assistant turn. We rebuild it as a plain dict so it round-trips
            # back into the next request cleanly.
            assistant: dict[str, Any] = {"role": "assistant", "content": msg.content}
            if msg.tool_calls:
                assistant["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.function.name, "arguments": c.function.arguments},
                    }
                    for c in msg.tool_calls
                ]
            self.messages.append(assistant)

            if not msg.tool_calls:
                # The model answered in words (or asked the user something). That is the
                # text we speak.
                return (msg.content or "").strip()

            for call in msg.tool_calls:
                name = call.function.name
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = await self._run_tool(conn, delegation_id, name, args)
                log.info("[%s] tool %s(%s) -> %s", self.session_id[-6:], name, args, result)
                self.messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": str(result)}
                )

        return "Sorry, I could not work that out."

    async def _delegate(self, conn: Any, delegation_id: str) -> None:
        request, self.input_text = self.input_text.strip(), ""
        log.info("[%s] delegation request: %r", self.session_id[-6:], request)
        try:
            answer = (await self._think(conn, delegation_id, request)).strip()

            if answer:
                log.info("[%s] brain answer for %r: %s", self.session_id[-6:], request, answer)
                await self._send_back(conn, delegation_id, "commentary", answer)
            else:
                log.info("[%s] brain gave no answer for %r; speaking fallback",
                         self.session_id[-6:], request)
                await self._send_back(
                    conn, delegation_id, "commentary", "Sorry, I could not work that out."
                )
        except Exception:
            log.exception("[%s] delegation %s failed", self.session_id, delegation_id)
