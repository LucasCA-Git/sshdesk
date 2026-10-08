# Security Policy

SSHDesk handles SSH connections, host keys and (optionally) saved passwords, so security reports are taken seriously.

## Supported versions

| Version | Supported |
|---|---|
| 1.1.x (latest) | ✅ |
| < 1.1 | ❌ — please update |

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

- Use GitHub's private reporting: **Security › Report a vulnerability** on the [repository page](https://github.com/LucasCA-Git/sshdesk/security/advisories/new).
- Include the affected version, your OS, steps to reproduce and the impact you expect.

You should get an acknowledgement within **7 days**. Once a fix is ready it will be released and credited to you (unless you prefer to stay anonymous).

## Security model (summary)

- Passwords and passphrases are never written in plain text or logged. They are kept in memory, or — only if the user ticks *Save password* — in the OS keyring (Windows Credential Manager / Secret Service) or, when there is none, in an encrypted local vault (Fernet, key file readable only by the user).
- Private keys are read where they are; they are never copied, displayed or uploaded.
- Unknown host keys require explicit confirmation of the SHA256 fingerprint; a changed host key blocks the connection.
- The desktop app has no telemetry and makes no network calls other than the SSH connections you open and, if you sign in, your own team server.
- The team server never receives secrets: only host metadata (alias, hostname, user, port, ProxyJump, safe options, host key fingerprints). Options that could run commands on members' machines (`ProxyCommand`, `LocalCommand`, `Include`, `Match`…) are rejected by the server **and** by the app.

See the *Security* sections of the [README](README.md#security) and [server/README.md](server/README.md#security) for details.
