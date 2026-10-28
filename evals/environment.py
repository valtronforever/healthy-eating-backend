"""Everything a scenario needs: the app, its database, the MCP proxy and a real opencode.

Startup:
1. Create the eval database if needed, recreate its schema, add a user and an access token.
2. Start the app (uvicorn) against that database, in an empty folder so it reads no .env.
3. Start the recording proxy in front of the app's /mcp endpoint.
4. Start `opencode serve` with a generated config: the HealthyEating server (through the
   proxy, bearer token, no OAuth), the fake Google Sheets server, and one agent per persona
   that may only use MCP tools. Then send a warm-up message until both MCP servers are
   connected, because opencode connects them lazily and a first message would otherwise
   reach the model without the tools.

Every turn of a scenario is one `opencode run --server ... --format json` call.
"""

import asyncio
import contextlib
import json
import os
import secrets
import socket
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Self

import httpx
from sqlalchemy import select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

import healthy_eating.db.models  # noqa: F401  (registers all tables on Base.metadata)
from evals.personas import ALL as PERSONAS
from evals.proxy import McpProxy
from healthy_eating.auth.tokens import generate_token, hash_token
from healthy_eating.db import Base, Database
from healthy_eating.db.base import utcnow
from healthy_eating.db.models import (
    AccessTokenRecord,
    DayStatusRecord,
    Meal,
    OAuthClient,
    User,
    WeightEntry,
)
from healthy_eating.nutrition.micronutrients import Micronutrient
from healthy_eating.nutrition.types import NUTRIENT_FIELDS, NutrientField
from healthy_eating.services.settings import (
    SettingsChanges,
    TargetRange,
    get_settings,
    update_settings,
)

EVALS_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = EVALS_DIR / "fixtures"
DEFAULT_DATABASE_URL = "postgresql+asyncpg://healthy:healthy@localhost:5432/healthy_eating_eval"
EVAL_CLIENT_ID = "healthy-eating-eval"

# Built-in opencode tools. Denying them leaves the model with MCP tools only, like a chat app.
BUILTIN_TOOLS = [
    "browser",
    "edit",
    "execute",
    "glob",
    "grep",
    "list",
    "lsp",
    "patch",
    "question",
    "read",
    "shell",
    "skill",
    "subagent",
    "task",
    "todowrite",
    "webfetch",
    "websearch",
    "write",
]


@dataclass(frozen=True)
class ToolUse:
    """A tool call as reported by opencode."""

    tool: str
    status: str
    input: dict[str, Any]
    output: str


@dataclass
class Turn:
    """One user message and the assistant's reply."""

    message: str
    session_id: str | None
    reply: str
    tool_uses: list[ToolUse]
    exit_code: int
    stderr: str
    duration_s: float
    files: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EvalSettings:
    model: str
    database_url: str
    run_dir: Path
    opencode_bin: str = "opencode"
    turn_timeout_s: float = 300.0


class EvalEnvironment:
    def __init__(self, settings: EvalSettings) -> None:
        self.settings = settings
        self._tmp = tempfile.TemporaryDirectory(prefix="he-eval-")
        tmp = Path(self._tmp.name)
        self.workspace = tmp / "workspace"
        self.backend_cwd = tmp / "backend"
        self.config_path = tmp / "opencode.json"
        self.sheets_log = tmp / "sheets.jsonl"
        self.db = Database(settings.database_url)
        self.proxy: McpProxy | None = None
        self.user_id: uuid.UUID | None = None
        self._processes: list[asyncio.subprocess.Process] = []
        self._log_files: list[Any] = []
        self._opencode_url = ""
        self._opencode_password = secrets.token_urlsafe(24)

    # Lifecycle

    async def __aenter__(self) -> Self:
        self.workspace.mkdir()
        self.backend_cwd.mkdir()
        self.settings.run_dir.mkdir(parents=True, exist_ok=True)
        try:
            await self._prepare_database()
            token = await self._create_user_and_token()
            app_url = await self._start_backend()
            proxy_port = _free_port()
            self.proxy = McpProxy(f"{app_url}/mcp")
            await self.proxy.start(proxy_port)
            self._write_opencode_config(f"http://127.0.0.1:{proxy_port}/mcp", token)
            await self._start_opencode()
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        for process in reversed(self._processes):
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except TimeoutError:
                    process.kill()
        if self.proxy is not None:
            await self.proxy.stop()
        for log in self._log_files:
            log.close()
        await self.db.dispose()
        self._tmp.cleanup()

    async def reset(self) -> None:
        """Forget everything from the previous scenario run."""
        async with self.db.session() as session:
            await session.execute(text("DELETE FROM meals"))
            await session.execute(text("DELETE FROM weights"))
            await session.execute(text("UPDATE users SET timezone = NULL"))
            await session.execute(
                update(User).values(
                    {
                        f"target_{name}_{bound}": None
                        for name in NUTRIENT_FIELDS
                        for bound in ("min", "max")
                    }
                    | {"calm_mode": False, "weight_tracking": False}
                )
            )
            await session.execute(text("DELETE FROM day_statuses"))
        self.sheets_log.unlink(missing_ok=True)
        assert self.proxy is not None
        self.proxy.reset()

    # What scenarios use

    async def chat(
        self,
        persona: str,
        message: str,
        *,
        session_id: str | None = None,
        files: list[Path] | None = None,
    ) -> Turn:
        args = [
            self.settings.opencode_bin,
            "run",
            "--server",
            self._opencode_url,
            "--format",
            "json",
            "--agent",
            persona,
            "--model",
            self.settings.model,
        ]
        if session_id:
            args += ["--session", session_id]
        for path in files or []:
            args += ["--file", str(path)]
        args.append(message)

        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=self.workspace,
            env=self._opencode_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.settings.turn_timeout_s
            )
        except TimeoutError:
            process.kill()
            stdout, stderr = await process.communicate()
        return _parse_turn(
            message,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
            process.returncode if process.returncode is not None else -1,
            time.monotonic() - started,
            [str(path) for path in files or []],
        )

    async def meals(self) -> list[Meal]:
        async with self.db.session() as session:
            rows = await session.scalars(select(Meal).order_by(Meal.created_at))
            return list(rows)

    async def weights(self) -> list[WeightEntry]:
        async with self.db.session() as session:
            rows = await session.scalars(select(WeightEntry).order_by(WeightEntry.created_at))
            return list(rows)

    async def time_zone(self) -> str | None:
        """The time zone stored for the eval user."""
        async with self.db.session() as session:
            time_zone: str | None = await session.scalar(
                select(User.timezone).where(User.id == self.user_id)
            )
            return time_zone

    async def targets(self) -> dict[NutrientField, TargetRange | None]:
        """The daily targets stored for the eval user."""
        assert self.user_id is not None
        async with self.db.session() as session:
            settings = await get_settings(session, self.user_id)
        assert settings is not None
        return settings.targets

    async def micronutrient_goals(self) -> dict[Micronutrient, TargetRange]:
        """The eval user's own vitamin and mineral goals."""
        assert self.user_id is not None
        async with self.db.session() as session:
            settings = await get_settings(session, self.user_id)
        assert settings is not None
        return settings.micronutrient_goals

    async def change_settings(self, changes: SettingsChanges) -> None:
        """Change the eval user's settings directly, as if they had done it in the app."""
        assert self.user_id is not None
        async with self.db.session() as session:
            await update_settings(session, self.user_id, changes)

    async def add_meal(
        self,
        dish_name: str,
        values: tuple[float, float, float, float],
        *,
        minutes_ago: int = 30,
        portion_grams: float | None = None,
        source_note: str | None = None,
    ) -> Meal:
        """Save a meal for the eval user directly, as if an earlier conversation had logged it."""
        calories, protein, fat, carbs = values
        async with self.db.session() as session:
            meal = Meal(
                user_id=self.user_id,
                dish_name=dish_name,
                source="from_conversation",
                eaten_at=utcnow() - timedelta(minutes=minutes_ago),
                calories_kcal=calories,
                protein_g=protein,
                fat_g=fat,
                carbohydrates_g=carbs,
                estimated_fields=[],
                confidence="high",
                portion_grams=portion_grams,
                source_note=source_note,
            )
            session.add(meal)
        return meal

    async def day_statuses(self) -> list[DayStatusRecord]:
        async with self.db.session() as session:
            rows = await session.scalars(select(DayStatusRecord))
            return list(rows)

    def sheet_rows(self) -> list[dict[str, Any]]:
        if not self.sheets_log.exists():
            return []
        lines = self.sheets_log.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    # Startup steps

    async def _prepare_database(self) -> None:
        url = make_url(self.settings.database_url)
        admin = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
        try:
            async with admin.connect() as conn:
                exists = await conn.scalar(
                    text("SELECT 1 FROM pg_database WHERE datname = :name"),
                    {"name": url.database},
                )
                if not exists:
                    await conn.execute(text(f'CREATE DATABASE "{url.database}"'))
        finally:
            await admin.dispose()

        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
                await conn.run_sync(Base.metadata.create_all)
        finally:
            await engine.dispose()

    async def _create_user_and_token(self) -> str:
        token = generate_token()
        async with self.db.session() as session:
            user = User(display_name="Eval User")
            session.add(user)
            session.add(
                OAuthClient(
                    client_id=EVAL_CLIENT_ID,
                    client_info={
                        "client_id": EVAL_CLIENT_ID,
                        "client_name": "opencode (evaluation)",
                        "redirect_uris": ["http://127.0.0.1/callback"],
                    },
                )
            )
            await session.flush()
            session.add(
                AccessTokenRecord(
                    token_hash=hash_token(token),
                    grant_id=uuid.uuid4(),
                    client_id=EVAL_CLIENT_ID,
                    user_id=user.id,
                    scopes=["nutrition"],
                    resource=None,
                    expires_at=utcnow() + timedelta(hours=12),
                )
            )
            self.user_id = user.id
        return token

    async def _start_backend(self) -> str:
        port = _free_port()
        base_url = f"http://localhost:{port}"
        env = {
            **os.environ,
            "ENVIRONMENT": "development",
            "BASE_URL": base_url,
            "DATABASE_URL": self.settings.database_url,
            "SECRET_KEY": secrets.token_urlsafe(48),
            "DEV_LOGIN_ENABLED": "false",
        }
        await self._spawn(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "--factory",
                "healthy_eating.main:create_app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=self.backend_cwd,
            env=env,
            log_name="backend.log",
        )
        async with httpx.AsyncClient() as client:
            for _ in range(150):
                with contextlib.suppress(httpx.TransportError):
                    response = await client.get(f"{base_url}/health")
                    if response.status_code == 200:
                        return base_url
                await asyncio.sleep(0.2)
        raise RuntimeError("The app did not start; see backend.log in the run folder")

    def _write_opencode_config(self, mcp_url: str, token: str) -> None:
        deny_builtins = [
            {"action": tool, "resource": "*", "effect": "deny"} for tool in BUILTIN_TOOLS
        ]
        mcp_servers = ("healthyeating", "sheets")
        config = {
            "$schema": "https://opencode.ai/config.json",
            "mcp": {
                "servers": {
                    "healthyeating": {
                        "type": "remote",
                        "url": mcp_url,
                        "headers": {"Authorization": f"Bearer {token}"},
                        "oauth": False,
                        "codemode": False,
                        "timeout": {"startup": 30000},
                    },
                    "sheets": {
                        "type": "local",
                        "command": [sys.executable, str(EVALS_DIR / "fake_sheets.py")],
                        "environment": {"FAKE_SHEETS_LOG": str(self.sheets_log)},
                        "codemode": False,
                        "timeout": {"startup": 30000},
                    },
                }
            },
            "agents": {
                persona.name: {
                    "mode": "primary",
                    "description": persona.description,
                    "system": persona.system,
                    "model": self.settings.model,
                    "permissions": deny_builtins
                    + [
                        {"action": f"{server}_*", "resource": "*", "effect": "deny"}
                        for server in mcp_servers
                        if server not in persona.connected_apps
                    ],
                }
                for persona in PERSONAS
            },
        }
        self.config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    async def _start_opencode(self) -> None:
        port = _free_port()
        self._opencode_url = f"http://127.0.0.1:{port}"
        log_path = self.settings.run_dir / "opencode-serve.log"
        await self._spawn(
            [
                self.settings.opencode_bin,
                "serve",
                "--hostname",
                "127.0.0.1",
                "--port",
                str(port),
                "--print-logs",
                "--log-level",
                "info",
            ],
            cwd=self.workspace,
            env=self._opencode_env(),
            log_name="opencode-serve.log",
        )
        await _wait_for_log(log_path, ["server listening"], timeout_s=60)

        # opencode connects MCP servers on first use; warm up until both are connected.
        needed = ["server=healthyeating", "server=sheets"]
        for _ in range(3):
            await self.chat("chat", "Reply with the single word OK.")
            if _log_has_connections(log_path, needed):
                return
            await asyncio.sleep(2)
            if _log_has_connections(log_path, needed):
                return
        raise RuntimeError("opencode did not connect the MCP servers; see opencode-serve.log")

    def _opencode_env(self) -> dict[str, str]:
        return {
            **os.environ,
            "OPENCODE_CONFIG": str(self.config_path),
            "OPENCODE_SERVER_PASSWORD": self._opencode_password,
        }

    async def _spawn(
        self, args: list[str], *, cwd: Path, env: dict[str, str], log_name: str
    ) -> asyncio.subprocess.Process:
        log = (self.settings.run_dir / log_name).open("wb")
        self._log_files.append(log)
        process = await asyncio.create_subprocess_exec(
            *args, cwd=cwd, env=env, stdout=log, stderr=asyncio.subprocess.STDOUT
        )
        self._processes.append(process)
        return process


def _parse_turn(
    message: str, stdout: str, stderr: str, exit_code: int, duration_s: float, files: list[str]
) -> Turn:
    session_id: str | None = None
    texts: list[str] = []
    tool_uses: list[ToolUse] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        session_id = session_id or event.get("sessionID")
        part = event.get("part") or {}
        if event.get("type") == "text" and part.get("text"):
            texts.append(part["text"])
        elif event.get("type") == "tool_use":
            state = part.get("state") or {}
            tool_uses.append(
                ToolUse(
                    tool=str(part.get("tool")),
                    status=str(state.get("status")),
                    input=dict(state.get("input") or {}),
                    output=str(state.get("output") or ""),
                )
            )
    return Turn(
        message=message,
        session_id=session_id,
        reply="\n".join(texts).strip(),
        tool_uses=tool_uses,
        exit_code=exit_code,
        stderr=stderr[-4000:],
        duration_s=round(duration_s, 1),
        files=files,
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _read_log(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def _log_has_connections(path: Path, needles: list[str]) -> bool:
    content = _read_log(path)
    connected = [line for line in content.splitlines() if 'message="mcp connected"' in line]
    return all(any(needle in line for line in connected) for needle in needles)


async def _wait_for_log(path: Path, needles: list[str], *, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        content = await asyncio.to_thread(_read_log, path)
        if all(needle in content for needle in needles):
            return
        await asyncio.sleep(0.2)
    raise RuntimeError(f"Timed out waiting for {needles} in {path.name}")
