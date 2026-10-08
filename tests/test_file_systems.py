"""Local file system API and transfer engine (remote side faked with a local SFTP-like client)."""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path

import pytest

from ssh_terminal.services.file_systems import LocalFS, Transfer, TransferCancelled, human_size


class FakeSFTPClient:
    """Implements the two calls transfers use, on the local disk."""

    def put(self, local, remote, callback=None, confirm=True):
        shutil.copyfile(local, remote)
        if callback:
            size = os.path.getsize(remote)
            callback(size, size)

    def get(self, remote, local, callback=None):
        shutil.copyfile(remote, local)
        if callback:
            size = os.path.getsize(local)
            callback(size, size)


class FakeRemoteFS(LocalFS):
    remote = True
    label = "fake-host"

    def __init__(self) -> None:
        self.client = FakeSFTPClient()


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    (src / "app" / "conf").mkdir(parents=True)
    (src / "app" / "main.py").write_text("print('hi')\n")
    (src / "app" / "conf" / ".env").write_text("X=1\n")
    (src / "big.bin").write_bytes(os.urandom(700_000))
    return src


def test_listdir_sorts_folders_first_and_ops(tree: Path) -> None:
    fs = LocalFS()
    names = [e.name for e in fs.listdir(str(tree))]
    assert names == ["app", "big.bin"]
    fs.mkdir(fs.join(str(tree), "new"))
    fs.rename(fs.join(str(tree), "new"), fs.join(str(tree), "renamed"))
    assert fs.exists(fs.join(str(tree), "renamed"))
    fs.remove(fs.stat(fs.join(str(tree), "app")))
    assert [e.name for e in fs.listdir(str(tree))] == ["renamed", "big.bin"]
    assert fs.parent(str(tree)) == str(tree.parent)
    assert fs.parent("/") == "/"


@pytest.mark.parametrize("dst_cls", [LocalFS, FakeRemoteFS])
def test_transfer_copies_trees_with_progress(tree: Path, tmp_path: Path, dst_cls) -> None:
    src, dst = LocalFS(), dst_cls()
    out = tmp_path / "out"
    out.mkdir()
    updates = []
    entries = [src.stat(str(tree / "app")), src.stat(str(tree / "big.bin"))]
    result = Transfer(src, entries, dst, str(out)).run(lambda p: updates.append((p.done, p.total)))
    assert (out / "app" / "conf" / ".env").read_text() == "X=1\n"
    assert (out / "big.bin").read_bytes() == (tree / "big.bin").read_bytes()
    assert result.files == 3 and updates[-1][0] == updates[-1][1] == result.total


def test_transfer_from_remote_and_skip(tree: Path, tmp_path: Path) -> None:
    src, dst = FakeRemoteFS(), LocalFS()
    out = tmp_path / "out"
    out.mkdir()
    (out / "big.bin").write_text("keep me")
    entries = [src.stat(str(tree / "big.bin")), src.stat(str(tree / "app"))]
    Transfer(src, entries, dst, str(out), skip={"big.bin"}).run()
    assert (out / "big.bin").read_text() == "keep me"
    assert (out / "app" / "main.py").exists()


def test_transfer_cancel(tree: Path, tmp_path: Path) -> None:
    src = LocalFS()
    cancel = threading.Event()
    cancel.set()
    transfer = Transfer(src, [src.stat(str(tree / "big.bin"))], src, str(tmp_path), cancel_event=cancel)
    with pytest.raises(TransferCancelled):
        transfer.run()


def test_human_size() -> None:
    assert human_size(512) == "512 B"
    assert human_size(2048) == "2.0 KB"
    assert human_size(5 * 1024**3) == "5.0 GB"
