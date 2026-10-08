# SSHDesk

Cliente SSH desktop para **Windows** e **Linux**, escrito em Python + PySide6 (Qt), que usa o seu `~/.ssh/config` como **fonte da verdade**. Inspirado na experiência de clientes SSH modernos, implementado do zero, com identidade visual própria.

> 📘 **Guia completo de como tudo funciona por dentro** (passo a passo de cada ação, threads, arquivos, como estender): [docs/COMO_FUNCIONA.md](docs/COMO_FUNCIONA.md)

```
┌──────────────────────────────────────────────────────────────┐
│ SSHDesk                                          ─ □ ×       │
├──────────────────────────────────────────────────────────────┤
│ File  Edit  View  SSH  Terminal  Help                        │
├───────────────┬──────────────────────────────────────────────┤
│ CONNECTIONS   │  [● server-prod ×] [● server-hml ×]     [+]  │
│ 🔍 Search     │                                              │
│ ★ FAVORITES   │  lucas@server-prod:~$ docker ps              │
│ RECENT        │  CONTAINER ID   IMAGE ...                    │
│ PRODUCTION    │                                              │
│ HOSTS         │                                              │
│ SSH DEFAULTS  │                                              │
│ + New Conn.   │                                              │
├───────────────┴──────────────────────────────────────────────┤
│ ● Connected  SSH  server-prod  lucas@10.0.0.10:22   120×40  UTF-8 │
└──────────────────────────────────────────────────────────────┘
```

## Features

**SSH config como fonte da verdade**
- Lê `~/.ssh/config` (Linux) / `%USERPROFILE%\.ssh\config` (Windows) automaticamente, incluindo arquivos de `Include`.
- Parser **sem perdas**: comentários, indentação, `Match`, `Include`, opções desconhecidas (`CustomOption something`), sintaxe `Key=Value`, aspas e CRLF são preservados byte a byte.
- Editar um host altera **só as linhas que mudaram** daquele bloco. Novos hosts entram **antes** de um `Host *` final (porque no OpenSSH o primeiro valor encontrado vence).
- Antes de qualquer escrita: backup (`config.bak` + cópia com timestamp na pasta do app), escrita em arquivo temporário + `fsync` + `os.replace` atômico, permissões preservadas (`0600` em arquivo novo), symlinks (dotfiles) seguidos.
- Configuração efetiva resolvida com `ssh -G <host>` quando o OpenSSH existe (wildcards, `Host *`, `Match`, `Include`), com fallback para um resolvedor próprio.
- Blocos `Host *` / `Host *.example.com` aparecem como **SSH Defaults**, separados dos servidores.
- **Compartilhamento em equipe (opcional):** Create Account / Sign In num SSHDesk Server próprio (`server/`), convites por email + código, hosts do time na sidebar e também no `ssh` do terminal (via `Include`). Chaves e senhas nunca são compartilhadas.
- Entradas que só servem ao Git (`User git`, `HostName github.com`/`gitlab.com`/...) vão para **Git & Services**: duplo clique testa a chave em vez de abrir um terminal sem shell. A classificação pode ser trocada no botão direito.
- File watcher: se o arquivo mudar por fora, aparece **Reload / Keep Current / View Differences** (diff colorido).

**Conexões**
- Um clique/duplo clique conecta: host, usuário, porta e chave vêm do SSH config.
- Autenticação: chave (inclusive chave com passphrase e certificados `-cert.pub`), ssh-agent / Pageant / agente do OpenSSH para Windows, keyboard-interactive (2FA/OTP) e senha.
- Senhas e passphrases ficam no cofre do sistema (Windows Credential Manager / Secret Service via `keyring`) e só se você marcar “Save password”. Sem cofre do sistema (WSL, servidor), vão para um cofre local criptografado (`credentials.vault`, Fernet/AES, chave `vault.key` só legível pelo usuário).
- `ProxyJump` nativo (multi-hop `a,b,c`, `user@host:port`), `ProxyCommand`, `ForwardAgent`, `Compression`, `ServerAliveInterval`, `IdentitiesOnly`, `PreferredAuthentications`, `StrictHostKeyChecking` (`ask`/`yes`/`no`/`accept-new`).
- `known_hosts`: verificação de host key, diálogo com fingerprint SHA256 para hosts novos, bloqueio com instrução de `ssh-keygen -R` quando a chave muda. O arquivo só recebe *append* (nunca é reescrito).
- **Test Connection** (DNS → TCP → autenticação SSH) em background.
- Port forwarding: Local (`-L`), Remote (`-R`) e Dynamic SOCKS4/4a/5 (`-D`) — pela janela *SSH › Port Forwarding* ou automaticamente a partir de `LocalForward/RemoteForward/DynamicForward` do config.
- Reconnect manual e auto-reconnect com backoff (configurável).

**Terminal**
- Emulação xterm real (via `pyte`) desenhada com QPainter: cores 16/256/truecolor, negrito/itálico/sublinhado/reverse, cursor (block/underline/bar, blink), UTF-8 (acentos, CJK, emojis), tela alternativa (vim, less, htop), bracketed paste, modo de cursor da aplicação, respostas DA/DSR, mouse SGR (1006).
- Teclado completo: Ctrl+C/D/L/Z, setas, Home/End, PgUp/PgDn, F1–F12, Alt como Meta, AltGr/teclas mortas (ABNT2) e IME.
- Scrollback configurável (Shift+PgUp/PgDn), seleção por caractere/palavra (duplo clique)/linha (triplo clique), copiar/colar, copy-on-select, paste com botão direito ou do meio (Linux).
- Terminal local: bash/zsh/fish no Linux (PTY real com controle de jobs); PowerShell 7, Windows PowerShell, cmd e WSL no Windows (ConPTY).
- Abas com status 🟢🟡🔴, reabrir aba fechada, split panes (direita/baixo, aninháveis). Cada split pode abrir **outro host** ou shell (menu ao clicar no botão de split, ou *Open to the Right/Below* no menu do host).
- **Arquivos (SFTP)** como no Termius: `+` › *Files (SFTP)* abre uma aba com *este computador | host*; no menu do host (*Files (SFTP)*) ou no botão de split dá para abrir a pasta numa nova aba, à direita ou abaixo, ao lado dos terminais. Cada painel tem um seletor de host no topo. **Arraste arquivos do Explorer/gerenciador de arquivos** para a pasta remota (upload), arraste entre painéis (upload/download/host→host) ou para uma subpasta no mesmo painel (mover). Também: nova pasta, renomear (F2), apagar (Del), pastas inteiras, barra de progresso com cancelar e aviso antes de sobrescrever. Usa a mesma autenticação/ProxyJump dos terminais.
- Botão na barra de abas (à esquerda) mostra/esconde a lista de hosts (`Ctrl+Shift+B`).
- **Broadcast input** (`Ctrl+Shift+I` ou ícone 📡 no cabeçalho): o que você digita em um terminal vai para todos os terminais da aba (borda amarela + selo BROADCAST).

**Interface**
- Tema Dark (padrão), Light e System; esquemas de cor do terminal: Default Dark, Dracula, Solarized Dark, Monokai, Nord, Gruvbox, Default Light.
- Sidebar redimensionável com busca instantânea (alias, aliases, hostname, usuário, grupo), Favoritos, Recentes, Grupos visuais e SSH Defaults.
- Menu de contexto do host: Connect, Open in New Tab, Test Connection, Edit, Duplicate, Rename, Favoritos, Move to Group, Copy SSH Command, Copy Full SSH Command, Open Config, Delete (com confirmação).
- Status bar (estado, tipo, host, `user@host:port`, tamanho, encoding), Settings (General, Appearance, Terminal, SSH, Keyboard, Security), Diagnostics, atalhos configuráveis.
- CLI: `sshdesk server-prod`, `sshdesk lucas@10.0.0.5:2222`, `sshdesk --list`.

## Requirements

- Python **3.12+** (para rodar do código-fonte; o executável não precisa de Python)
- Windows 10 1809+ (ConPTY) ou Linux com X11/Wayland
- Opcional, mas recomendado: cliente OpenSSH (`ssh`) no PATH — usado só para resolver a configuração efetiva (`ssh -G`). No Windows já vem em *Recursos opcionais › Cliente OpenSSH*.

Dependências Python: `PySide6-Essentials`, `paramiko`, `cryptography`, `pyte`, `keyring` e, só no Windows, `pywinpty`.

## Installation

**Windows (PowerShell):**

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

**WSL / Linux (bash ou zsh):**

```bash
sudo apt install python3-venv python3-full libxcb-cursor0 libxkbcommon0 libegl1
python3 -m venv ~/.venvs/sshdesk
source ~/.venvs/sshdesk/bin/activate
pip install -r requirements.txt
python app.py
```

**WSL / Linux (fish):** igual ao anterior, mas ative com `source ~/.venvs/sshdesk/bin/activate.fish`.

> No WSL o app lê o `~/.ssh/config` **do Linux**, não o do Windows. Para usar os hosts do Windows, rode no PowerShell (ou copie o arquivo).
> Crie o venv no home do Linux (`~/.venvs/...`), não em `/mnt/c/...`, que é muito lento.

Para instalar como pacote (cria os comandos `sshdesk` e `ssh-terminal`):

```bash
pip install .
sshdesk
```

> No Linux, o Qt precisa de algumas bibliotecas do sistema. Em Debian/Ubuntu: `sudo apt install libxcb-cursor0 libxkbcommon0 libegl1`.
> No Linux com Secret Service (GNOME Keyring/KWallet) as senhas vão para ele; sem ele (ex.: WSL) vão para o cofre local criptografado.

## Development

```bash
pip install -r requirements-dev.txt
ruff check src tests
python -m pytest                 # 126 testes, nenhum servidor SSH necessário
```

Testes ponta a ponta contra `sshd` reais (opcional, em VM/container descartável — o script altera a senha do root):

```bash
sudo scripts/start_test_sshd.sh /tmp/sshdesk-sshd
SSHDESK_LIVE_SSHD=/tmp/sshdesk-sshd python -m pytest tests/integration
```

Eles cobrem (12 testes): chave, chave com passphrase, senha, ProxyJump, ProxyCommand, rejeição/troca de host key, forwards `-L/-R/-D`, SFTP, shell interativo e Test Connection.

## Running

```bash
python -m ssh_terminal                       # abre a interface
python -m ssh_terminal server-prod           # abre e conecta em server-prod
python -m ssh_terminal server-prod database   # duas abas
python -m ssh_terminal lucas@10.0.0.5:2222   # conexão avulsa (fora do config)
python -m ssh_terminal --local               # abre com um terminal local
python -m ssh_terminal --list                # lista os hosts e sai
python -m ssh_terminal -F ~/outro/config     # usa outro arquivo de config
python -m ssh_terminal --debug               # log detalhado no console
```

### Atalhos padrão

| Ação | Atalho |
|---|---|
| Nova aba (terminal local) | `Ctrl+T` |
| Fechar aba/painel | `Ctrl+W` |
| Reabrir aba fechada | `Ctrl+Shift+T` |
| Próxima / anterior | `Ctrl+Tab` / `Ctrl+Shift+Tab` |
| Buscar conexões | `Ctrl+Shift+F` |
| Settings | `Ctrl+,` |
| Copiar / Colar | `Ctrl+Shift+C` / `Ctrl+Shift+V` (também `Shift+Insert`) |
| Split direita / baixo (escolhe o host) | `Ctrl+Shift+\` / `Ctrl+Shift+-` |
| Broadcast input na aba | `Ctrl+Shift+I` |
| Fonte maior / menor / reset | `Ctrl+=` / `Ctrl+-` / `Ctrl+0` |
| Nova conexão | `Ctrl+Shift+N` |
| Recarregar SSH config | `F5` |
| Tela cheia / Sidebar | `F11` / `Ctrl+Shift+B` |

Todos podem ser alterados em *Settings › Keyboard*. Teclas que não são atalhos vão para o terminal (Ctrl+C, Ctrl+D, Ctrl+L, etc.).
**Dica:** `Ctrl+W` também é “apagar palavra” no bash; se você usa muito, troque “Close tab” para `Ctrl+Shift+W`.

## Building

O PyInstaller não faz cross-compile: gere o `.exe` no Windows e o binário Linux no Linux.

```bash
pip install -r requirements-dev.txt
pyinstaller --noconfirm --clean sshdesk.spec
```

Resultado: `dist/SSHDesk.exe` (Windows) ou `dist/SSHDesk` (Linux), arquivo único, sem console.

Scripts prontos (criam venv, rodam os testes e geram o executável):

- `scripts/build_windows.ps1`
- `scripts/build_linux.sh`

O workflow `.github/workflows/build.yml` faz lint, testes e build para Windows e Linux em cada push e publica os executáveis como artifacts.

## Windows

- Config: `%USERPROFILE%\.ssh\config` — por exemplo `C:\Users\<você>\.ssh\config`.
- Settings do app: `%APPDATA%\SSHDesk\app_config.json`; logs em `%APPDATA%\SSHDesk\logs\app.log`; backups em `%APPDATA%\SSHDesk\backups\`.
- Terminal local via **ConPTY** (`pywinpty`): PowerShell 7 (se instalado), Windows PowerShell, cmd e WSL.
- Agente: o serviço *OpenSSH Authentication Agent* do Windows e o Pageant são detectados pelo Paramiko.
- Senhas salvas vão para o **Windows Credential Manager** (entradas “SSHDesk”).
- Para `IdentityFile`, caminhos com espaços são gravados entre aspas automaticamente; `~/.ssh/...` é expandido para o seu perfil.

## Linux

- Config: `~/.ssh/config`. Settings: `~/.config/sshdesk/app_config.json` (respeita `$XDG_CONFIG_HOME`).
- Terminal local com PTY real (`pty.fork`), com `TERM=xterm-256color`; o shell padrão é o seu `$SHELL`.
- Senhas salvas via Secret Service (GNOME Keyring/KWallet).
- Atalho de menu: copie `scripts/sshdesk.desktop` para `~/.local/share/applications/` e o ícone `src/ssh_terminal/resources/icons/app-256.png` para `~/.local/share/icons/sshdesk.png`.

## SSH Configuration

Exemplo suportado:

```sshconfig
# Jump box
Host bastion
    HostName bastion.example.com
    User lucas

Host server-prod prod
    HostName 10.0.0.10
    User lucas
    Port 22
    IdentityFile ~/.ssh/id_ed25519

Host database
    HostName 10.0.0.20
    User postgres
    ProxyJump bastion
    LocalForward 5432 localhost:5432

Host *.example.com
    User lucas

Host *
    ServerAliveInterval 60
```

- `server-prod prod`: o primeiro nome é o alias exibido; os demais aparecem em “Aliases” e também funcionam na busca.
- `Host *` e padrões aparecem em **SSH Defaults** (editáveis, mas nunca tratados como servidor).
- O comentário logo acima de um `Host` é exibido no tooltip e é removido junto se você excluir o host.
- Campos editados pelo app: `HostName, User, Port, IdentityFile, ProxyJump, ProxyCommand, ForwardAgent, ForwardX11, Compression, ServerAliveInterval, ServerAliveCountMax, StrictHostKeyChecking, PreferredAuthentications, IdentitiesOnly, AddKeysToAgent, LocalForward, RemoteForward, DynamicForward`. Qualquer outra opção aparece em *Advanced › Other options* e é preservada.
- Caixas tri-state no diálogo (meio marcado) = “não definido” — o app não escreve a linha e o OpenSSH usa o default/herança.
- **Separação de responsabilidades:** `~/.ssh/config` guarda conexões; `app_config.json` guarda só metadados de UI (tema, fonte, favoritos, recentes, grupos, atalhos, abas abertas). Grupos e favoritos **nunca** são escritos no SSH config.

## Security

- Nenhuma senha/passphrase é gravada em arquivo, impressa ou logada. Elas ficam na memória da sessão ou, se você pedir, no cofre do sistema operacional.
- Chaves privadas são lidas do lugar onde estão; nunca são copiadas, exibidas ou incluídas em diagnóstico.
- O log (`app.log`) tem filtro de redação que remove qualquer coisa parecida com `password=...` ou blocos de chave privada.
- Hosts desconhecidos pedem confirmação com fingerprint; troca de host key bloqueia a conexão.
- O app funciona 100% local: não há telemetria nem chamadas a serviços externos.
- *Settings › Security* permite desativar o keyring, mudar a política de host keys e apagar todas as credenciais salvas.

## Architecture

```
src/ssh_terminal/
├── main.py                 # CLI + bootstrap do Qt
├── errors.py               # exceções e mensagens amigáveis (FriendlyError)
├── models/                 # dataclasses puras: SSHHost, AppSettings, ConnectionSpec, PortForwardSpec
├── ssh/
│   ├── config_parser.py    # parser sem perdas (SSHConfigDocument / SSHConfigSet + Include)
│   ├── config_writer.py    # edição cirúrgica + escrita atômica com backup
│   ├── resolver.py         # SSHConfigResolver (ssh -G + fallback), find_ssh_binary()
│   ├── host_keys.py        # known_hosts (somente append)
│   ├── ssh_auth.py         # chave/agent/senha/2FA + AuthPrompter
│   ├── ssh_session.py      # SSHConnector/SSHConnection (ProxyJump, ProxyCommand)
│   ├── ssh_manager.py      # fachada + Test Connection
│   ├── forwarding.py       # -L / -R / -D (SOCKS)
│   └── sftp_session.py     # SFTPSession (API pronta para um navegador de arquivos)
├── terminal/
│   ├── terminal_emulator.py  # pyte + scrollback + tela alternativa
│   ├── terminal_widget.py    # QPainter, teclado, mouse, seleção, clipboard
│   ├── terminal_session.py   # ponte Qt (threads → sinais), reconnect
│   ├── keymap.py, color_schemes.py
│   └── backends/            # base.py | ssh_backend.py | local_unix.py (PTY) | local_windows.py (ConPTY)
├── services/               # app_context, config/settings/credential/connection/diagnostics
├── ui/                     # main_window, connection_panel, connection_dialog, terminal_tabs, settings...
├── utils/                  # paths (get_ssh_config_path), platform, logging
└── resources/icons/        # ícone próprio (SVG/PNG/ICO)
```

O terminal é desacoplado do transporte:

```
TerminalWidget  ←→  TerminalSession  ←→  TerminalBackend (ABC)
                                         ├── SSHBackend      (Paramiko channel)
                                         ├── UnixPtyBackend  (pty.fork)
                                         └── WinPtyBackend   (ConPTY)
```

Para adicionar Docker exec, `kubectl exec`, serial, telnet ou distribuições WSL basta implementar um novo `TerminalBackend` (`start`, `write`, `resize`, `close`) e um tipo em `ConnectionKind` — widget, abas, splits e reconnect funcionam sem mudança.

Decisões principais:

| Decisão | Por quê |
|---|---|
| `pyte` para emulação | parser VT100/xterm maduro em Python puro; evita reimplementar centenas de sequências |
| QPainter em vez de QTextEdit | grade de células real (cursor, cores por célula, tela alternativa, seleção por caractere/palavra/linha) |
| `pty.fork()` no Linux | o shell precisa de terminal de controle para Ctrl+C/Ctrl+Z (job control); `subprocess` + `openpty` não dá isso |
| ConPTY via `pywinpty` no Windows | única forma de ter console interativo real (PowerShell, cores, resize) a partir de app GUI |
| ProxyJump nativo (canais `direct-tcpip`) | não depende do binário `ssh` e funciona igual nos dois sistemas |
| `ssh -G` com fallback | resolve exatamente como o OpenSSH (Match, Include, tokens); sem OpenSSH o app continua funcionando |
| Parser sem perdas próprio | `paramiko.SSHConfig` é só leitura e perde comentários/ordem; `configparser` não entende o formato |

## Testing

```bash
python -m pytest
```

- `test_ssh_config.py` — parser: round-trip byte a byte, CRLF/BOM/tabs, aliases, wildcards, Match, Include, aspas.
- `test_config_writer.py` — edição de uma linha só, opções desconhecidas preservadas, inserção antes de `Host *`, remoção com comentário, backup, escrita atômica, permissões, symlink.
- `test_resolver.py` — regras de matching, herança de `Host *`, `ssh -G` com `subprocess` mockado e fallback.
- `test_ssh_mock.py` — conexão com `Transport` falso: senha, host key (ask/yes/accept-new/mismatch), ProxyJump multi-hop, detecção de loop.
- `test_services.py` — CRUD em disco, edição externa preservada, settings, keyring falso, CLI.
- `test_terminal.py` / `test_gui.py` — emulador, teclas, e janela principal em modo offscreen (abas, split, fechar/reabrir).

Os testes usam um `HOME` temporário: o seu `~/.ssh/config` real **nunca** é tocado.

## Troubleshooting

| Sintoma | O que fazer |
|---|---|
| “Host key verification failed — has CHANGED” | O servidor foi reinstalado ou o IP mudou. Se confiar, rode o `ssh-keygen -R` mostrado em *View technical details*. |
| “Authentication failed” | Veja *Details*: lista métodos tentados e aceitos pelo servidor. Confira `User`, se a chave pública está no `authorized_keys`, e *Diagnostics › SSH Agent*. |
| Chave com passphrase pede sempre | Marque “Save password” no prompt, ou carregue a chave no agente (`ssh-add`). |
| Terminal local não abre no Windows | Instale `pywinpty` (`pip install pywinpty`); requer Windows 10 1809+. |
| `qt.qpa.plugin: Could not load the Qt platform plugin "xcb"` | `sudo apt install libxcb-cursor0 libxkbcommon0 libegl1`. |
| Onde a senha foi salva? | *Settings › Security › Stored in* mostra o keyring do sistema ou o cofre local criptografado. |
| Config editado por fora não aparece | `F5` (Reload) ou ative *Settings › General › Watch the SSH config*. |
| Precisa de mais detalhes | Rode com `--debug` e veja *Help › Open Log Folder*. |

### Limitações conhecidas (v1.0)

- `ForwardX11` é gravado no config mas o cliente embutido não faz X11 forwarding.
- `AddKeysToAgent` é gravado mas não é aplicado pelo app (use `ssh-add`).
- Blocos `Match` só são avaliados quando o OpenSSH está disponível (`ssh -G`); o resolvedor interno só entende `Match all`.
- Entradas `@cert-authority`/`@revoked` e padrões com curinga no `known_hosts` não são usados na verificação (o host é tratado como desconhecido e pede confirmação).
- Reporte de mouse apenas no modo SGR (1006), que é o usado por vim, htop, tmux e mc modernos.
- SFTP e Port Forwarding têm API completa; o SFTP ainda não tem navegador de arquivos na interface.
- macOS não foi testado (o código evita impedimentos, mas não há build oficial).

## Lançando uma nova versão

1. Suba a versão em `src/ssh_terminal/__init__.py` (`__version__`, semver).
2. Adicione a entrada no topo de `src/ssh_terminal/resources/CHANGELOG.md` (em inglês): `## [x.y.z] - AAAA-MM-DD` com seções `### New`, `### Improved`, `### Fixed`. Um teste falha se você esquecer.
3. Na primeira vez que cada pessoa abrir a nova versão, o app mostra a janela **What's New** (agradecimento + o que mudou desde a versão que ela tinha + link do GitHub). Ela não aparece de novo depois; dá para reabrir em *Help › What's New*.
