"""Local shell detection helpers and readable exit codes."""

from __future__ import annotations

from ssh_terminal.utils.platform import describe_exit_status, parse_wsl_distros


def test_parse_wsl_distros_utf16_and_utf8() -> None:
    raw = "Ubuntu-22.04\r\ndocker-desktop\r\nDebian\r\n\r\n".encode("utf-16-le")
    assert parse_wsl_distros(raw) == ["Ubuntu-22.04", "Debian"]
    assert parse_wsl_distros(b"Ubuntu\nfedora\n") == ["Ubuntu", "fedora"]
    assert parse_wsl_distros(b"") == []


def test_describe_exit_status() -> None:
    assert "console was closed" in describe_exit_status(3221225786)  # 0xC000013A, seen with PowerShell
    assert "0xC000013A" in describe_exit_status(3221225786)
    assert describe_exit_status(-1073741510) == describe_exit_status(3221225786)  # signed form
    assert describe_exit_status(1) == "Process exited (status 1)"
    assert describe_exit_status(None) == "Process exited"


class _FakeProc:
    def __init__(self, chunks: list[bytes], status: int) -> None:
        self.chunks, self.exitstatus = list(chunks), status

    def read(self, _n: int) -> bytes:
        if self.chunks:
            return self.chunks.pop(0)
        raise EOFError

    def isalive(self) -> bool:
        return bool(self.chunks)


def _run(backend_cls, spawns):  # noqa: ANN001, ANN202
    backend = backend_cls(["powershell.exe"])
    calls: list[bool] = []

    def spawn(legacy: bool = False):  # noqa: ANN202
        calls.append(legacy)
        return spawns.pop(0)

    backend._spawn = spawn
    out: dict = {"data": b"", "closed": None}
    backend.set_callbacks(lambda d: out.__setitem__("data", out["data"] + d), lambda *_: None,
                          lambda info: out.__setitem__("closed", info))
    backend.proc = spawn()
    backend._read_loop()
    return calls, out


def test_conpty_quick_exit_falls_back_to_winpty_once() -> None:
    from ssh_terminal.terminal.backends.local_windows import WinPtyBackend

    calls, out = _run(WinPtyBackend, [_FakeProc([], 3221225786), _FakeProc([b"PS C:\\> "], 0)])
    assert calls == [False, True] and out["data"] == b"PS C:\\> "
    assert out["closed"].reason == "Process exited (status 0)"

    calls, out = _run(WinPtyBackend, [_FakeProc([], 3221225786), _FakeProc([], 3221225786)])
    assert calls == [False, True]  # only one retry
    assert "console was closed" in out["closed"].reason and "try another shell" in out["closed"].reason


def test_normal_exit_is_not_retried() -> None:
    from ssh_terminal.terminal.backends.local_windows import WinPtyBackend

    calls, out = _run(WinPtyBackend, [_FakeProc([b"bye\r\n"], 0)])
    assert calls == [False] and out["closed"].reason == "Process exited (status 0)"
