# SSHDesk Server

Backend opcional do SSHDesk para **compartilhar hosts entre equipes**: contas (email + senha), times, convites por email + código e hosts compartilhados.

O app continua funcionando 100% local sem ele. O servidor só entra quando alguém usa **Account › Sign In**.

## O que é (e o que não é) compartilhado

| Compartilhado | Nunca sai da máquina de cada pessoa |
|---|---|
| Alias, HostName, User, Port, ProxyJump | Chaves privadas |
| Caminho do IdentityFile (só como dica) | Senhas e passphrases |
| Grupo, descrição | `~/.ssh/config` pessoal |
| Opções seguras (`ServerAliveInterval`, `LocalForward`...) | |
| Fingerprint da host key (para ninguém aceitar "no escuro") | |

Opções que executam comandos na máquina dos membros (`ProxyCommand`, `LocalCommand`, `KnownHostsCommand`, `Include`, `Match`...) ou enfraquecem a verificação de host key são **recusadas pelo servidor e de novo pelo app** (allow-list nos dois lados).

## Rodando

### Docker Compose (PostgreSQL)

```bash
cd server
cp .env.example .env        # troque SECRET_KEY (openssl rand -base64 48) e POSTGRES_PASSWORD
docker compose up -d
curl http://localhost:8080/api/v1/info
```

Para usar pela internet, coloque atrás de HTTPS (Caddy, Nginx ou Traefik). O token de login trafega no header `Authorization`.

### Sem Docker (SQLite, bom para testar)

```bash
cd server
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
SECRET_KEY=$(openssl rand -base64 48) uvicorn sshdesk_server.main:create_app --factory --host 0.0.0.0 --port 8080
```

Sem `DATABASE_URL`, os dados ficam em `./data/sshdesk.db`. A documentação interativa da API fica em `http://localhost:8080/docs`.

## Configuração (variáveis de ambiente)

| Variável | Padrão | Para quê |
|---|---|---|
| `SECRET_KEY` | gerada em `DATA_DIR/secret.key` | assina os tokens (mín. 32 caracteres) |
| `DATABASE_URL` | SQLite em `DATA_DIR` | ex.: `postgresql+psycopg://user:pass@db:5432/sshdesk` |
| `DATA_DIR` | `./data` | SQLite + chave gerada |
| `PUBLIC_URL` | `http://localhost:8080` | aparece no email de convite |
| `ALLOW_REGISTRATION` | `true` | `false` fecha o cadastro público |
| `TOKEN_TTL_HOURS` | `720` (30 dias) | validade do login |
| `INVITE_TTL_DAYS` | `7` | validade do código de convite |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_TLS`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | vazio | envio do convite por email (`SMTP_TLS`: `starttls`, `ssl` ou `none`) |

Sem SMTP, nada quebra: ao convidar alguém, o app mostra o código para o admin mandar por Slack ou WhatsApp.

## Fluxo

1. **Create Account / Sign In** no app (*Account*). A senha é guardada com hash Argon2id, e o app guarda o token no keyring do sistema.
2. Quem cria um time vira **Owner**.
3. Owners e admins usam **Teams › Invites › Invite by Email**:
   - o servidor gera um código de uso único (ex.: `K7QH-M2XP-9TWA`), válido por 7 dias e preso àquele email;
   - o servidor guarda só o hash do código.
4. A pessoa entra com **aquele email** e usa **Teams › Accept Invite**.
5. Hosts compartilhados:
   - admins usam **botão direito no host › Share with Team**, ou **Edit in Team** num host que já é do time;
   - membros só visualizam.
6. O app sincroniza ao abrir, a cada 5 minutos e após qualquer mudança. O servidor responde `304 Not Modified` quando nada mudou.

### Papéis

| Ação | Member | Admin | Owner |
|---|:-:|:-:|:-:|
| Ver hosts e membros | ✓ | ✓ | ✓ |
| Criar, editar e remover hosts | | ✓ | ✓ |
| Convidar e revogar convites | | ✓ | ✓ |
| Remover membros | | ✓ | ✓ |
| Mudar papéis, excluir o time | | | ✓ |
| Sair do time | ✓ | ✓ | ✓ (se não for o último owner) |

## API

Todas as rotas ficam sob `/api/v1`. A referência completa está em `/docs` (OpenAPI).

```
POST   /auth/register            POST /auth/login           GET /auth/me
POST   /auth/password            POST /auth/logout-all
GET    /teams                    POST /teams                PATCH/DELETE /teams/{id}
GET    /teams/{id}/members       PUT/DELETE /teams/{id}/members/{user_id}
GET    /teams/{id}/invites       POST /teams/{id}/invites   DELETE /teams/{id}/invites/{invite_id}
GET    /invites                  POST /invites/accept
GET    /teams/{id}/hosts         POST /teams/{id}/hosts     PUT/DELETE /teams/{id}/hosts/{host_id}
GET    /sync                     (ETag / If-None-Match → 304)
```

## Segurança

- **Senhas:** Argon2id. O login tem custo constante quando o email não existe, para não revelar quais contas existem.
- **Tokens:** JWT HS256 com expiração. Trocar a senha ou chamar `/auth/logout-all` invalida todos os tokens da conta.
- **Força bruta:** rate limit em login, cadastro e aceite de convite.
- **Convites:** só o hash é guardado; o convite é de uso único, expira e é preso ao email convidado.
- **Times:** quem não é membro recebe 404, para não saber quais times existem.
- **Hosts:** validação estrita de cada campo e allow-list de opções.

## Testes

```bash
pip install -r requirements-dev.txt
python -m pytest
```

O teste ponta a ponta com o app desktop (servidor real numa thread) fica em `../tests/test_team_sync.py`.
