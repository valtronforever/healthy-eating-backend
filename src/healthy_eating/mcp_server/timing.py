"""Log how long each MCP request takes, without its arguments or results."""

import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from fastmcp.server.dependencies import get_access_token, get_http_headers
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from healthy_eating.db import Database
from healthy_eating.services.connections import get_client_name

logger = logging.getLogger("healthy_eating.mcp")

# Sessions and client names remembered per process; the oldest are forgotten first.
MAX_TRACKED_SESSIONS = 1000
MAX_CACHED_CLIENTS = 1000
# Ids are logged shortened: enough to follow one session or client through the log.
SHORT_ID_LENGTH = 8
# Client names come from client registration: any text, so it is cut and kept on one line.
MAX_CLIENT_NAME_LENGTH = 40


@dataclass
class _Session:
    requests: int
    started: float
    last_request: float


class RequestTimingMiddleware(Middleware):
    """One log line per MCP request: method, tool, duration, outcome and its session.

    Per session (one connection of an AI client, often one chat) it counts the requests and
    shows the time since the previous one: that gap is spent in the client (the model
    thinking, the user confirming an action), not on this server.

    Only names, ids and timings are logged, never arguments, results or error messages: they
    can contain what the user ate.
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        self._sessions: OrderedDict[str, _Session] = OrderedDict()
        self._client_names: OrderedDict[str, str] = OrderedDict()

    async def on_request(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        method = context.method or "unknown"
        if method == "tools/call":
            method = f"tools/call {getattr(context.message, 'name', 'unknown')}"
        started = time.perf_counter()
        outcome = "ok"
        try:
            return await call_next(context)
        except Exception as error:
            outcome = f"error {type(error).__name__}"
            raise
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            token = get_access_token()
            client_id = token.client_id if token is not None else None
            # The server gives the session its id in the answer to initialize.
            if context.method == "initialize":
                session = "new session"
            else:
                session = self._track_session(client_id, started)
            client = await self._client_label(client_id)
            logger.info(
                "MCP %s: %s in %.1f ms (%s, %s)", method, outcome, elapsed_ms, session, client
            )

    def _track_session(self, client_id: str | None, now: float) -> str:
        # Clients on older MCP versions send a session id; newer ones are stateless, so their
        # requests are followed per connection (OAuth client) instead.
        session_id = get_http_headers(include={"mcp-session-id"}).get("mcp-session-id")
        if session_id:
            key, kind = f"session:{session_id}", f"session {session_id[:SHORT_ID_LENGTH]}"
        elif client_id:
            key, kind = f"client:{client_id}", "no session"
        else:
            return "no session"
        state = self._sessions.pop(key, None)
        if state is None:
            state = _Session(requests=0, started=now, last_request=now)
        gap = now - state.last_request
        state.requests += 1
        state.last_request = now
        self._sessions[key] = state
        while len(self._sessions) > MAX_TRACKED_SESSIONS:
            self._sessions.popitem(last=False)
        label = f"{kind} request {state.requests}"
        if state.requests > 1:
            label += f", {gap:.1f} s after the previous, {now - state.started:.1f} s in"
        return label

    async def _client_label(self, client_id: str | None) -> str:
        if not client_id:
            return "client unknown"
        name = self._client_names.get(client_id)
        if name is None:
            try:
                async with self._db.session() as session:
                    registered = await get_client_name(session, client_id) or "unnamed"
                name = " ".join(registered.split())[:MAX_CLIENT_NAME_LENGTH]
            except Exception:
                # Logging must never fail the request; try again next time.
                logger.warning("Could not look up the MCP client name")
                return f"client {client_id[:SHORT_ID_LENGTH]}"
            self._client_names[client_id] = name
            while len(self._client_names) > MAX_CACHED_CLIENTS:
                self._client_names.popitem(last=False)
        return f"client {name} {client_id[:SHORT_ID_LENGTH]}"
