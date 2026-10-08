"""High level SSH facade used by the UI and services.

Glues together the SSH config (source of truth), the resolver, credentials
and the connector, and implements the "Test Connection" checks.
"""

from __future__ import annotations

import logging
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from ssh_terminal.errors import describe_exception
from ssh_terminal.models.ssh_host import SSHHost
from ssh_terminal.ssh.config_parser import SSHConfigSet
from ssh_terminal.ssh.resolver import ResolvedConfig, SSHConfigResolver
from ssh_terminal.ssh.ssh_auth import AuthPrompter, CredentialStore
from ssh_terminal.ssh.ssh_session import ConnectOptions, SSHConnection, SSHConnector

log = logging.getLogger(__name__)


class StepStatus(StrEnum):
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class TestStep:
    name: str
    status: StepStatus
    message: str = ""
    details: str = ""


@dataclass
class TestReport:
    steps: list[TestStep] = field(default_factory=list)
    duration: float = 0.0

    @property
    def success(self) -> bool:
        return bool(self.steps) and all(s.status in (StepStatus.OK, StepStatus.SKIPPED) for s in self.steps)


StepCallback = Callable[[TestStep], None]


class SSHManager:
    def __init__(
        self,
        config_loader: Callable[[], SSHConfigSet],
        resolver: SSHConfigResolver,
        credentials: CredentialStore,
        options: ConnectOptions,
    ) -> None:
        self._config_loader = config_loader
        self.resolver = resolver
        self.credentials = credentials
        self.options = options

    # ------------------------------------------------------------------ #
    def resolve(self, alias: str) -> ResolvedConfig:
        return self.resolver.resolve(alias, self._config_loader())

    def resolve_unsaved(self, host: SSHHost) -> ResolvedConfig:
        """Effective config for a host being edited (not yet in the file)."""
        return self.resolver.resolve_builtin(host.alias, self._config_loader(), override=host)

    def resolve_adhoc(self, user: str | None, hostname: str, port: int | None) -> ResolvedConfig:
        """``ssh user@host -p port`` for a host that is not in the config."""
        from dataclasses import replace

        resolved = self.resolve(hostname)
        if user:
            resolved = replace(resolved, user=user)
        if port:
            resolved = replace(resolved, port=port)
        return resolved

    def connector(
        self,
        prompter: AuthPrompter,
        password: str | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> SSHConnector:
        return SSHConnector(self.resolve, prompter, self.credentials, self.options, password=password, progress=progress)

    def connect(self, resolved: ResolvedConfig, prompter: AuthPrompter, progress: Callable[[str], None] | None = None) -> SSHConnection:
        return self.connector(prompter, progress=progress).connect(resolved)

    # ------------------------------------------------------------------ #
    def test_connection(
        self,
        resolved: ResolvedConfig,
        prompter: AuthPrompter,
        on_step: StepCallback | None = None,
        password: str | None = None,
    ) -> TestReport:
        """DNS -> TCP -> SSH handshake + authentication. Never raises."""
        report = TestReport()
        start = time.monotonic()
        emit = on_step or (lambda _s: None)

        def record(step: TestStep) -> TestStep:
            report.steps.append(step)
            emit(step)
            return step

        tunnelled = bool(resolved.proxy_jump or resolved.proxy_command)
        timeout = float(resolved.connect_timeout or self.options.timeout)

        if tunnelled:
            via = f"ProxyJump {resolved.proxy_jump}" if resolved.proxy_jump else "ProxyCommand"
            record(TestStep("DNS", StepStatus.SKIPPED, f"Resolved remotely through {via}"))
            record(TestStep("TCP", StepStatus.SKIPPED, f"Port {resolved.port} reached through {via}"))
        else:
            emit(TestStep("DNS", StepStatus.RUNNING, f"Resolving {resolved.hostname}"))
            try:
                infos = socket.getaddrinfo(resolved.hostname, resolved.port, type=socket.SOCK_STREAM)
                address = infos[0][4][0]
                record(TestStep("DNS", StepStatus.OK, f"Host reachable ({resolved.hostname} → {address})"))
            except OSError as exc:
                friendly = describe_exception(exc, resolved.hostname)
                record(TestStep("DNS", StepStatus.FAILED, friendly.message, friendly.details))
                report.duration = time.monotonic() - start
                return report

            emit(TestStep("TCP", StepStatus.RUNNING, f"Connecting to port {resolved.port}"))
            try:
                with socket.create_connection((resolved.hostname, resolved.port), timeout=timeout):
                    pass
                record(TestStep("TCP", StepStatus.OK, f"Port {resolved.port} reachable"))
            except OSError as exc:
                friendly = describe_exception(exc, f"{resolved.hostname}:{resolved.port}")
                record(TestStep("TCP", StepStatus.FAILED, friendly.message, friendly.details))
                report.duration = time.monotonic() - start
                return report

        emit(TestStep("SSH", StepStatus.RUNNING, f"Authenticating as {resolved.user}"))
        connection: SSHConnection | None = None
        try:
            connection = self.connector(prompter, password=password).connect(resolved)
            record(TestStep("SSH", StepStatus.OK, "SSH authentication successful"))
        except Exception as exc:  # noqa: BLE001 - reported to the user with details
            friendly = describe_exception(exc, resolved.hostname)
            record(TestStep("SSH", StepStatus.FAILED, friendly.as_text(), friendly.details))
        finally:
            if connection is not None:
                connection.close()
        report.duration = time.monotonic() - start
        return report
