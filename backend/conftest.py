"""Shared fixtures for the AEGIS pytest suite.

Spawn a throwaway ``aegis_pytest_<pid>`` Postgres database (in the already
running Docker Postgres), migrate it with alembic, and boot one or more
uvicorn servers against it with isolated environment. The dev ``aegis``
database is never touched. Teardown stops servers and drops the test DB.

In-process async tests share ONE event loop (suite-provided ``loop`` fixture)
because ``db/session.py`` builds its engine at import time; asyncpg pools are
loop-bound, so every in-process DB call must run on that same loop.
"""
import asyncio
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

import asyncpg
import httpx
import pytest

from testutils import unique_sid

BACKEND = Path(__file__).resolve().parent
VENV_PY = str(BACKEND / ".venv" / "bin" / "python")

POSTGRES_ADMIN_URL = os.getenv("TEST_PG_ADMIN_URL", "postgresql://aegis:aegis@localhost:5432/postgres")
DB_NAME = f"aegis_pytest_{os.getpid()}"
TEST_DB_URL = f"postgresql+asyncpg://aegis:aegis@localhost:5432/{DB_NAME}"

#: Env for the in-process REPO/MAIN (db/session builds the engine at import,
#: so DATABASE_URL must be in place before any ``db``/``main`` import).
os.environ["DATABASE_URL"] = TEST_DB_URL
os.environ["AEGIS_LLM_STUB"] = "0"
os.environ["HF_SIM_MODE"] = "svg"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerProc:
    def __init__(self, proc: subprocess.Popen, base_url: str, log_path: Path):
        self.proc = proc
        self.base_url = base_url
        self.log_path = log_path

    def logs(self) -> str:
        try:
            return self.log_path.read_text(errors="replace")
        except OSError:
            return "<no log captured>"


def _spawn_server(name: str, *, env_overrides: dict) -> ServerProc:
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env.update(env_overrides)
    env.update(
        {
            "DATABASE_URL": TEST_DB_URL,
            "HF_SIM_MODE": "svg",
            "AEGIS_LLM_RETRY_DELAY": "0.01",
        }
    )
    log_path = BACKEND / ".pytest-server-logs" / f"{name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as log:
        proc = subprocess.Popen(
            [VENV_PY, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
            cwd=str(BACKEND),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    sp = ServerProc(proc, base_url, log_path)
    deadline = time.monotonic() + 60
    with httpx.Client(timeout=2.0) as client:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"server {name} exited early rc={proc.returncode}\n{sp.logs()}"
                )
            try:
                r = client.get(f"{base_url}/health")
                if r.status_code == 200:
                    return sp
            except httpx.TransportError:
                pass
            time.sleep(0.2)
    proc.kill()
    raise RuntimeError(f"server {name} did not become healthy\n{sp.logs()}")


def _drop_database(dsn: str, name: str) -> None:
    async def _drop():
        conn = await asyncpg.connect(dsn=dsn)
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await conn.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_drop())
    finally:
        loop.close()


def _create_database(dsn: str, name: str) -> None:
    async def _create():
        conn = await asyncpg.connect(dsn=dsn)
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            await conn.execute(f'CREATE DATABASE "{name}"')
        finally:
            await conn.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_create())
    finally:
        loop.close()


@pytest.fixture(scope="session")
def loop() -> asyncio.AbstractEventLoop:
    """One shared event loop for all in-process async work."""
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    yield _loop
    _loop.close()


@pytest.fixture(scope="session")
def test_db() -> str:
    """A freshly migrated throwaway Postgres database."""
    _create_database(POSTGRES_ADMIN_URL, DB_NAME)
    env = dict(os.environ)
    env["DATABASE_URL"] = TEST_DB_URL
    subprocess.run(
        [VENV_PY, "-m", "alembic", "upgrade", "head"],
        cwd=str(BACKEND),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    yield TEST_DB_URL
    _drop_database(POSTGRES_ADMIN_URL, DB_NAME)


@pytest.fixture(scope="session")
def server_stub(test_db) -> ServerProc:
    """Backend server with the LLM stub on — used by HTTP/WS E2E tests."""
    sp = _spawn_server(
        "stub",
        env_overrides={"AEGIS_LLM_STUB": "1"},
    )
    try:
        yield sp
    finally:
        sp.proc.terminate()
        try:
            sp.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            sp.proc.kill()


@pytest.fixture(scope="session")
def server_dead_llm(test_db) -> ServerProc:
    """Backend server pointed at an unreachable LLM provider (stub OFF).

    Negotiation failing here must fall back to the neutral resolution, still
    park conflicts for human approval, persist the error events, and complete
    — without any hung websocket.
    """
    sp = _spawn_server(
        "dead_llm",
        env_overrides={
            "AEGIS_LLM_STUB": "0",
            "LLM_PROVIDER": "ollama",
            "OLLAMA_BASE_URL": "http://127.0.0.1:1",
            "GROQ_API_KEY": "",
        },
    )
    try:
        yield sp
    finally:
        sp.proc.terminate()
        try:
            sp.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            sp.proc.kill()