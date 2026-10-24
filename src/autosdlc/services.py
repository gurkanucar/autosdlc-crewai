"""Throwaway PostgreSQL and Redis servers for the tests of a project that uses them.

compose.yaml only ships them for deployment; the tests run inside this application's own container, so without these
an API test of a Postgres project has nothing to connect to. Every run gets an empty database on a free local port.
"""

import shutil
import socket
import subprocess
import time
from glob import glob
from pathlib import Path


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(port: int, process: subprocess.Popen | None = None, timeout: float = 30) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def postgres_bin() -> Path | None:
    """Folder of initdb/pg_ctl: on PATH, or in Debian's versioned folder that is not on PATH."""
    found = shutil.which("pg_ctl")
    if found:
        return Path(found).parent
    candidates = sorted(glob("/usr/lib/postgresql/*/bin/pg_ctl"))
    return Path(candidates[-1]).parent if candidates else None


class LocalServices:
    """Starts the servers a project's decisions ask for, once per run; stop() shuts them down."""

    def __init__(self, workdir: Path, decisions: dict[str, str]):
        self.workdir = workdir
        self.decisions = decisions
        self.env: dict[str, str] | None = None
        self.notes: list[str] = []
        self._postgres_data: Path | None = None
        self._postgres_bin: Path | None = None
        self._redis: subprocess.Popen | None = None

    def start(self) -> dict[str, str]:
        """The environment variables pointing the tests at the servers; started on the first call only."""
        if self.env is None:
            self.env = {}
            if self.decisions.get("database") == "postgres":
                self._start_postgres()
            if self.decisions.get("cache") == "redis":
                self._start_redis()
        return self.env

    def _start_postgres(self) -> None:
        bin_dir = postgres_bin()
        if bin_dir is None:
            self.notes.append("PostgreSQL is not installed here; DATABASE_URL is not set for the tests")
            return
        data = self.workdir / "postgres"
        shutil.rmtree(data, ignore_errors=True)
        data.parent.mkdir(parents=True, exist_ok=True)
        port = _free_port()
        log = self.workdir / "postgres.log"
        try:
            subprocess.run([str(bin_dir / "initdb"), "-D", str(data), "-U", "app", "--auth=trust", "-E", "UTF8"],
                           capture_output=True, text=True, timeout=120, check=True)
            options = f"-p {port} -c listen_addresses=127.0.0.1 -k {data}"
            subprocess.run([str(bin_dir / "pg_ctl"), "-D", str(data), "-o", options, "-l", str(log), "-w", "start"],
                           capture_output=True, text=True, timeout=120, check=True)
            self._postgres_data, self._postgres_bin = data, bin_dir
            subprocess.run([str(bin_dir / "createdb"), "-h", "127.0.0.1", "-p", str(port), "-U", "app", "app"],
                           capture_output=True, text=True, timeout=60, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            self.notes.append(f"PostgreSQL could not be started for the tests: {exc}")
            return
        # trust authentication: any password is accepted, so apps that require one in the URL work too.
        self.env["DATABASE_URL"] = f"postgresql://app:app@127.0.0.1:{port}/app"
        self.env.update({"PGHOST": "127.0.0.1", "PGPORT": str(port), "PGUSER": "app", "PGPASSWORD": "app", "PGDATABASE": "app"})

    def _start_redis(self) -> None:
        server = shutil.which("redis-server")
        if server is None:
            self.notes.append("Redis is not installed here; REDIS_URL is not set for the tests")
            return
        port = _free_port()
        try:
            self._redis = subprocess.Popen([server, "--port", str(port), "--bind", "127.0.0.1", "--save", "",
                                            "--appendonly", "no"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            self.notes.append(f"Redis could not be started for the tests: {exc}")
            return
        if not _wait_for_port(port, self._redis):
            self.notes.append("Redis did not start for the tests")
            return
        self.env["REDIS_URL"] = f"redis://127.0.0.1:{port}/0"

    def stop(self) -> None:
        if self._postgres_data is not None:
            subprocess.run([str(self._postgres_bin / "pg_ctl"), "-D", str(self._postgres_data), "-m", "immediate", "stop"],
                           capture_output=True, timeout=60, check=False)
            self._postgres_data = None
        if self._redis is not None:
            self._redis.terminate()
            try:
                self._redis.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._redis.kill()
            self._redis = None
        self.env = None
