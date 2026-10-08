"""Service-level tests: config CRUD on disk, settings persistence, credentials, CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import SAMPLE_CONFIG
from ssh_terminal.errors import DuplicateHostError, HostNotFoundError
from ssh_terminal.main import list_hosts, spec_for_argument
from ssh_terminal.models.ssh_host import SSHHost
from ssh_terminal.services.config_service import ConfigService
from ssh_terminal.services.credential_service import KeyringCredentialStore
from ssh_terminal.services.settings_service import SettingsService
from ssh_terminal.ssh.config_writer import ConfigFileWriter


@pytest.fixture
def service(ssh_config: Path, tmp_path: Path) -> ConfigService:
    return ConfigService(ssh_config, ConfigFileWriter(tmp_path / "backups"))


def test_service_lists_hosts(service: ConfigService) -> None:
    assert service.aliases() == ["server-prod", "server-hml", "bastion", "database", "server"]
    assert [h.alias for h in service.wildcard_hosts()] == ["*.example.com", "*"]


def test_service_crud_roundtrip(service: ConfigService, ssh_config: Path) -> None:
    service.add_host(SSHHost(alias="api-prod", hostname="10.1.1.1", user="deploy"))
    assert "Host api-prod" in ssh_config.read_text()
    assert (ssh_config.parent / "config.bak").read_text() == SAMPLE_CONFIG
    host = service.get_host("api-prod")
    host.port = 2222
    service.update_host("api-prod", host)
    assert service.get_host("api-prod").port == 2222
    service.rename_host("api-prod", "api")
    assert service.get_host("api") is not None and service.get_host("api-prod") is None
    dup = service.duplicate_host("api")
    assert dup.alias == "api-copy" and service.get_host("api-copy").hostname == "10.1.1.1"
    service.remove_host("api")
    service.remove_host("api-copy")
    assert ssh_config.read_text() == SAMPLE_CONFIG
    with pytest.raises(HostNotFoundError):
        service.remove_host("api")
    with pytest.raises(DuplicateHostError):
        service.add_host(SSHHost(alias="prod"))  # alias of server-prod


def test_service_preserves_external_edits(service: ConfigService, ssh_config: Path) -> None:
    service.reload()
    ssh_config.write_text(ssh_config.read_text() + "\nHost added-outside\n    HostName 1.2.3.4\n")
    assert service.changed_files() == [ssh_config]
    host = service.get_host("bastion")
    host.port = 2200
    service.update_host("bastion", host)  # re-reads the file before editing
    text = ssh_config.read_text()
    assert "Host added-outside" in text and "Port 2200" in text
    assert service.changed_files() == []


def test_service_acknowledge(service: ConfigService, ssh_config: Path) -> None:
    service.reload()
    ssh_config.write_text("Host x\n")
    assert service.changed_files()
    service.acknowledge_external_change()
    assert not service.changed_files()
    assert "bastion" in service.aliases()  # keep current = old data still shown


def test_create_config(isolated_home: Path, tmp_path: Path) -> None:
    path = isolated_home / ".ssh" / "config"
    svc = ConfigService(path, ConfigFileWriter(tmp_path / "b"))
    assert not svc.exists
    svc.create_config_file()
    assert path.exists() and svc.hosts() == []


def test_settings_service(tmp_path: Path) -> None:
    path = tmp_path / "app" / "app_config.json"
    svc = SettingsService(path)
    svc.load()
    svc.settings.favorites.append("server-prod")
    svc.settings.font_size = 16
    svc.save()
    data = json.loads(path.read_text())
    assert data["favorites"] == ["server-prod"] and data["font_size"] == 16
    assert not any("password" in k.lower() for k in data)
    assert SettingsService(path).load().font_size == 16


def test_settings_service_recovers_from_corruption(tmp_path: Path) -> None:
    path = tmp_path / "app_config.json"
    path.write_text("{not json")
    settings = SettingsService(path).load()
    assert settings.theme == "dark"
    assert (tmp_path / "app_config.json.broken").exists()


class MemoryKeyring:
    def __init__(self):
        self.data = {}

    def get_password(self, service, name):
        return self.data.get((service, name))

    def set_password(self, service, name, secret):
        self.data[(service, name)] = secret

    def delete_password(self, service, name):
        self.data.pop((service, name), None)

    def get_keyring(self):
        return self


def test_credential_store_with_fake_keyring(tmp_path: Path) -> None:
    store = KeyringCredentialStore(tmp_path / "index.json")
    fake = MemoryKeyring()
    store._available, store._keyring = True, fake
    store.set_password("lucas", "10.0.0.10", 22, "s3cret")
    store.set_passphrase("/home/u/.ssh/id", "pp")
    assert store.get_password("lucas", "10.0.0.10", 22) == "s3cret"
    index = (tmp_path / "index.json").read_text()
    assert "s3cret" not in index and "pp\"" not in index
    assert store.clear_all() == 2
    assert fake.data == {}


def test_cli_list(ssh_config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert list_hosts(None) == 0
    out = capsys.readouterr().out.split()
    assert out == ["server-prod", "server-hml", "bastion", "database", "server"]


def test_cli_list_missing(isolated_home: Path) -> None:
    assert list_hosts(None) == 1


def test_cli_spec_for_argument() -> None:
    assert spec_for_argument("server-prod", {"server-prod"}).alias == "server-prod"
    adhoc = spec_for_argument("lucas@10.0.0.5:2200", set())
    assert adhoc.adhoc == {"user": "lucas", "hostname": "10.0.0.5", "port": 2200}
