"""Docker on a server, over SSH only (the ``docker`` CLI already on the server).

* Lists are read with ``--format '{{json .}}'`` and parsed line by line as JSON,
  never by splitting the human table.
* Every container/image reference is validated (:func:`safe_ref`) and quoted
  before it is put in a command, so a crafted name can never inject shell.
* Failures are classified (:class:`Problem`) so the UI can offer the right fix:
  install Docker, use sudo / the ``docker`` group, or start the daemon.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from ssh_terminal.services.remote_exec import CommandResult, RemoteRunner, quote

# Container names, IDs, image refs (repo[:tag][@sha256:digest]) and ``name:tag`` for pulls.
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-/:@+]{0,254}$")
_PORT = re.compile(r"(?:(?P<ip>[\d.]+|\[?[0-9a-f:]*\]?):)?(?P<host>\d+)->(?P<container>\d+)/(?P<proto>tcp|udp)")
ACTIONS = ("start", "stop", "restart", "pause", "unpause")
MAX_LOG_TAIL = 100_000


def safe_ref(value: str) -> str:
    """A container/image reference that is safe to pass to the docker CLI (raises ValueError otherwise)."""
    value = (value or "").strip()
    if not _REF.match(value) or ".." in value:
        raise ValueError(f"Invalid Docker reference: {value!r}")
    return value


# ---------------------------------------------------------------------- #
# Models + parsing
# ---------------------------------------------------------------------- #
@dataclass
class Container:
    id: str
    name: str
    image: str
    status: str
    state: str  # running | paused | exited | created | restarting | removing | dead
    ports: str = ""
    created: str = ""

    @property
    def short_id(self) -> str:
        return self.id[:12]

    @property
    def running(self) -> bool:
        return self.state in ("running", "restarting")


@dataclass
class Image:
    repository: str
    tag: str
    id: str
    size: str
    created: str

    @property
    def ref(self) -> str:
        """What to pass to ``docker rmi``: the name when it has one, the ID for dangling images."""
        if self.repository and self.repository != "<none>" and self.tag and self.tag != "<none>":
            return f"{self.repository}:{self.tag}"
        return self.id


def parse_json_lines(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue  # warnings printed by docker plugins, etc.
        if isinstance(value, dict):
            rows.append(value)
    return rows


def state_from_status(status: str) -> str:
    """Older Docker versions have no ``State`` field: derive it from the status text."""
    low = status.lower()
    if "(paused)" in low:
        return "paused"
    if low.startswith("up"):
        return "running"
    if low.startswith("restarting"):
        return "restarting"
    if low.startswith("created"):
        return "created"
    if low.startswith("removal"):
        return "removing"
    if low.startswith("dead"):
        return "dead"
    return "exited"


def parse_containers(text: str) -> list[Container]:
    out = []
    for row in parse_json_lines(text):
        status = str(row.get("Status", ""))
        out.append(Container(
            id=str(row.get("ID", "")),
            name=str(row.get("Names", "")).split(",")[0],
            image=str(row.get("Image", "")),
            status=status,
            state=str(row.get("State") or state_from_status(status)).lower(),
            ports=str(row.get("Ports", "")),
            created=str(row.get("CreatedAt", "")),
        ))
    return out


def parse_images(text: str) -> list[Image]:
    return [
        Image(str(r.get("Repository", "")), str(r.get("Tag", "")), str(r.get("ID", "")),
              str(r.get("Size", "")), str(r.get("CreatedSince") or r.get("CreatedAt", "")))
        for r in parse_json_lines(text)
    ]


def allowed_actions(state: str) -> set[str]:
    """Which buttons make sense for a container in ``state``."""
    if state == "running":
        return {"stop", "restart", "pause", "remove"}
    if state == "paused":
        return {"unpause", "stop", "remove"}
    if state == "restarting":
        return {"stop", "remove"}
    if state == "removing":
        return set()
    return {"start", "restart", "remove"}  # exited / created / dead


def action_command(action: str, ref: str, force: bool = False, volumes: bool = False) -> str:
    ref = quote(safe_ref(ref))
    if action == "remove":
        flags = (" -f" if force else "") + (" -v" if volumes else "")
        return f"docker rm{flags} {ref}"
    if action not in ACTIONS:
        raise ValueError(f"Unknown action {action!r}")
    return f"docker {action} {ref}"


def logs_command(ref: str, tail: int = 300, follow: bool = False, timestamps: bool = False) -> str:
    tail = max(0, min(int(tail), MAX_LOG_TAIL))
    flags = f"--tail {tail}" + (" -f" if follow else "") + (" -t" if timestamps else "")
    return f"docker logs {flags} {quote(safe_ref(ref))}"


# ---------------------------------------------------------------------- #
# Databases published by containers (suggested in the SQL console)
# ---------------------------------------------------------------------- #
@dataclass
class DatabaseCandidate:
    container: str
    kind: str  # "mysql" | "postgres"
    port: int  # published port on the server
    image: str
    host: str = "127.0.0.1"  # where it is published (as seen from the server)

    def describe(self) -> str:
        return f"{self.container} ({self.image}) → {self.host}:{self.port}"


_DB_IMAGES = (("postgres", "postgres", 5432), ("postgis", "postgres", 5432), ("timescale", "postgres", 5432),
              ("mariadb", "mysql", 3306), ("mysql", "mysql", 3306), ("percona", "mysql", 3306))


def database_candidates(containers: list[Container]) -> list[DatabaseCandidate]:
    found = []
    for c in containers:
        if not c.running:
            continue
        image = c.image.lower().rsplit("/", 1)[-1]
        match = next(((kind, port) for key, kind, port in _DB_IMAGES if image.startswith(key)), None)
        if match is None:
            continue
        kind, internal = match
        for m in _PORT.finditer(c.ports):
            if int(m.group("container")) == internal and m.group("proto") == "tcp":
                ip = (m.group("ip") or "").strip("[]")
                host = ip if ip and ip not in ("0.0.0.0", "::") else "127.0.0.1"  # all interfaces -> loopback
                found.append(DatabaseCandidate(c.name, kind, int(m.group("host")), c.image, host))
                break
    return found


# ---------------------------------------------------------------------- #
# Errors
# ---------------------------------------------------------------------- #
class Problem(Enum):
    NOT_INSTALLED = "not_installed"
    PERMISSION = "permission"
    DAEMON_DOWN = "daemon_down"
    SUDO_PASSWORD = "sudo_password"  # sudo asked for a (correct) password
    OTHER = "other"


def classify(result: CommandResult) -> Problem:
    err = (result.err or "").lower()
    if result.status == 127 or "docker: command not found" in err or "docker: not found" in err:
        return Problem.NOT_INSTALLED
    if "sudo:" in err and ("password" in err or "incorrect" in err or "a terminal is required" in err):
        return Problem.SUDO_PASSWORD
    if "permission denied" in err and "docker" in err and ".sock" in err:
        return Problem.PERMISSION
    if "cannot connect to the docker daemon" in err or "is the docker daemon running" in err \
            or "failed to connect to the docker api" in err or "if the daemon is running" in err \
            or ("docker.sock" in err and "no such file" in err):  # wording of Docker <= 28 and >= 29
        return Problem.DAEMON_DOWN
    return Problem.OTHER


class DockerError(Exception):
    def __init__(self, problem: Problem, message: str) -> None:
        super().__init__(message)
        self.problem = problem


# ---------------------------------------------------------------------- #
# Client
# ---------------------------------------------------------------------- #
@dataclass
class DockerSummary:
    version: str
    containers: list[Container] = field(default_factory=list)
    images: list[Image] = field(default_factory=list)

    @property
    def running(self) -> int:
        return sum(1 for c in self.containers if c.state == "running")


class DockerClient:
    def __init__(self, runner: RemoteRunner) -> None:
        self.runner = runner

    def _run(self, command: str, timeout: float = 60) -> str:
        result = self.runner.run(command, timeout=timeout)
        if not result.ok:
            message = (result.err or result.out).strip() or f"exit status {result.status}"
            raise DockerError(classify(result), message)
        return result.out

    def version(self) -> str:
        return self._run("docker version --format '{{.Server.Version}}'").strip()

    def containers(self) -> list[Container]:
        return parse_containers(self._run("docker ps -a --no-trunc --format '{{json .}}'"))

    def images(self) -> list[Image]:
        return parse_images(self._run("docker images --format '{{json .}}'"))

    def summary(self) -> DockerSummary:
        version = self.version()
        return DockerSummary(version, self.containers(), self.images())

    def action(self, action: str, ref: str, force: bool = False, volumes: bool = False) -> str:
        return self._run(action_command(action, ref, force, volumes), timeout=120)

    def remove_image(self, ref: str, force: bool = False) -> str:
        return self._run(f"docker rmi{' -f' if force else ''} {quote(safe_ref(ref))}", timeout=120)

    def prune_images(self) -> str:
        return self._run("docker image prune -f", timeout=300)

    def pull(self, image: str, on_text: Callable[[str], None], stop: threading.Event) -> None:
        result = self.runner.stream(f"docker pull {quote(safe_ref(image))}", on_text, stop)
        if result.status not in (0, -1):
            raise DockerError(classify(result), result.err.strip() or f"exit status {result.status}")

    def logs(self, ref: str, tail: int, follow: bool, timestamps: bool,
             on_text: Callable[[str], None], stop: threading.Event) -> CommandResult:
        return self.runner.stream(logs_command(ref, tail, follow, timestamps), on_text, stop)
