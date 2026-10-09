# SSHDesk Server

Optional, self-hosted backend that lets an organisation **share SSH hosts with its teams**. It handles accounts (email + password), teams and roles, invites by email + one-time code, and shared hosts organised in groups.

The desktop app works 100% locally without it. The server only comes into play when someone uses *Account › Sign In*. Each company runs **its own** instance, so data never goes through a third party.

> 🇧🇷 Resumo em português: veja a seção *Compartilhamento em equipe* do [README.pt-BR.md](../README.pt-BR.md) e o guia [docs/COMO_FUNCIONA.md](../docs/COMO_FUNCIONA.md#13b-compartilhamento-em-equipe).

## What is (and isn't) shared

| Shared with the team | Never leaves each person's machine |
|---|---|
| Alias, HostName, User, Port, ProxyJump | Private keys |
| IdentityFile path (as a hint only) | Passwords and passphrases |
| Group and description | Personal `~/.ssh/config` |
| Safe options (`ServerAliveInterval`, `LocalForward`…) | |
| Host key fingerprints (so nobody accepts keys blindly) | |

Options that could run commands on members' machines (`ProxyCommand`, `LocalCommand`, `KnownHostsCommand`, `Include`, `Match`…) or weaken host key checking are **rejected by the server and again by the app**. The allow-list is enforced on both sides.

## Quick start

### Docker Compose (PostgreSQL), recommended

```bash
cd server
cp .env.example .env              # edit SECRET_KEY (openssl rand -base64 48) and POSTGRES_PASSWORD
docker compose up -d
curl http://localhost:8080/api/v1/info
```

### Without Docker (SQLite), for evaluation

```bash
cd server
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
SECRET_KEY=$(openssl rand -base64 48) uvicorn sshdesk_server.main:create_app --factory --host 0.0.0.0 --port 8080
```

Without `DATABASE_URL`, data is stored in `./data/sshdesk.db`. The interactive API reference is at `http://localhost:8080/docs`.

## Production checklist

1. **HTTPS is required** before exposing the server to the internet, because the login token travels in the `Authorization` header. A minimal [Caddy](https://caddyserver.com/) reverse proxy with automatic Let's Encrypt certificates:

   ```caddyfile
   sshdesk.example.com {
       reverse_proxy localhost:8080
   }
   ```

   Nginx or Traefik work the same way. Set `PUBLIC_URL=https://sshdesk.example.com`.
2. **Secrets.** Use a strong, unique `SECRET_KEY` (32+ characters) and `POSTGRES_PASSWORD`. Never commit `.env`.
3. **Registration.** Once your people have accounts, set `ALLOW_REGISTRATION=false`.
4. **Email (optional).** Configure `SMTP_*` to email invite codes. Without it, the app shows the code so an admin can send it over chat.
5. **Backups.** Back up the database regularly, for example with a daily cron job:

   ```bash
   docker compose exec -T db pg_dump -U sshdesk sshdesk | gzip > backup-$(date +%F).sql.gz
   ```
6. **Monitoring.** `GET /healthz` returns `200` when the service is up; point your uptime checker at it.
7. **Updates.** `git pull && docker compose up -d --build`. The schema is created automatically on start (versioned migrations are on the roadmap, so back up before upgrading).

Then each user opens the app, goes to *Account › Create Account / Sign In* and enters your server URL.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `SECRET_KEY` | generated in `DATA_DIR/secret.key` | signs the tokens (min. 32 characters) |
| `DATABASE_URL` | SQLite in `DATA_DIR` | e.g. `postgresql+psycopg://user:pass@db:5432/sshdesk` |
| `DATA_DIR` | `./data` | SQLite file and generated key |
| `PUBLIC_URL` | `http://localhost:8080` | shown in invite emails |
| `ALLOW_REGISTRATION` | `true` | `false` closes public sign-up |
| `TOKEN_TTL_HOURS` | `720` (30 days) | login validity |
| `INVITE_TTL_DAYS` | `7` | invite code validity |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_TLS`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | empty | invite emails (`SMTP_TLS`: `starttls`, `ssl` or `none`) |

## How it works

1. **Create Account / Sign In** in the app. Passwords are hashed with Argon2id; the app keeps the token in the OS keyring (or the encrypted local vault).
2. Whoever creates a team becomes its **Owner**.
3. Owners and admins invite people in *Teams › Invites › Invite by Email*:
   - the server creates a single-use code (e.g. `K7QH-M2XP-9TWA`), bound to that email and valid for 7 days;
   - only a hash of the code is stored.
4. The invited person signs in with **that email** and uses *Teams › Accept Invite*.
5. Sharing hosts (admins and owners):
   - right-click a host › **Share with Team**, choosing the **group** it belongs to in the team;
   - right-click a **group title** › **Share Group with Team** to publish the whole group at once (existing hosts are updated);
   - **Edit in Team** / **Remove from Team** on hosts that already belong to the team.
6. The app syncs on start, every 5 minutes and after each change. The server answers `304 Not Modified` when nothing changed (ETag). Members see `TEAM · <team> › <group>` sections, and the hosts also work in plain `ssh`, `scp` and `git` through a managed `Include`.

### Roles

| Action | Member | Admin | Owner |
|---|:-:|:-:|:-:|
| View hosts and members | ✓ | ✓ | ✓ |
| Create, edit and remove hosts | | ✓ | ✓ |
| Invite and revoke invites | | ✓ | ✓ |
| Remove members | | ✓ | ✓ |
| Change roles, delete the team | | | ✓ |
| Leave the team | ✓ | ✓ | ✓ (unless last owner) |

## API

All routes live under `/api/v1`; the complete OpenAPI reference is served at `/docs`.

```
POST   /auth/register            POST /auth/login           GET /auth/me
POST   /auth/password            POST /auth/logout-all
GET    /teams                    POST /teams                PATCH/DELETE /teams/{id}
GET    /teams/{id}/members       PUT/DELETE /teams/{id}/members/{user_id}
GET    /teams/{id}/invites       POST /teams/{id}/invites   DELETE /teams/{id}/invites/{invite_id}
GET    /invites                  POST /invites/accept
GET    /teams/{id}/hosts         POST /teams/{id}/hosts     PUT/DELETE /teams/{id}/hosts/{host_id}
GET    /sync                     (ETag / If-None-Match → 304)
GET    /info                     (server version, registration open?)
```

Health check (outside the API prefix): `GET /healthz`.

## Security

- **Passwords:** Argon2id. Login takes constant time when the email does not exist, so accounts cannot be enumerated.
- **Tokens:** HS256 JWT with expiry. Changing the password or calling `/auth/logout-all` revokes every token of the account.
- **Brute force:** rate limits on login, registration and invite acceptance.
- **Invites:** stored as hashes, single-use, expiring and bound to the invited email.
- **Teams:** non-members get `404`, so they cannot discover which teams exist.
- **Hosts:** strict validation of every field plus the option allow-list described above.
- **No duplicates:** a team never holds the same host twice: not by name (case-insensitive) and not by server (same HostName, port, ProxyJump and user). The app also skips team hosts that duplicate a member's personal hosts or another team's.

Please report vulnerabilities privately; see [SECURITY.md](../SECURITY.md).

## Tech stack

FastAPI · SQLAlchemy 2 · PostgreSQL or SQLite · argon2-cffi · PyJWT · Uvicorn · Docker.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The end-to-end test with the desktop app (a real server started in a thread: register, invite, accept, share, sync, groups, sign out) lives in [`../tests/test_team_sync.py`](../tests/test_team_sync.py).
