# Como o SSHDesk funciona

Guia completo: como rodar, o que acontece por dentro em cada ação e onde mexer no código para mudar cada coisa.

> Resumo em uma frase: o SSHDesk lê o seu `~/.ssh/config`, mostra cada `Host` na barra lateral e, quando você clica, abre uma conexão SSH (Paramiko) numa thread separada. Os bytes que chegam passam por um emulador de terminal (pyte) e são desenhados numa grade de células com QPainter.

---

## Sumário

1. [Rodando o projeto](#1-rodando-o-projeto)
2. [Visão geral da arquitetura](#2-visão-geral-da-arquitetura)
3. [O que acontece quando o app abre](#3-o-que-acontece-quando-o-app-abre)
4. [O que acontece quando você conecta num host](#4-o-que-acontece-quando-você-conecta-num-host)
5. [O que acontece quando você digita](#5-o-que-acontece-quando-você-digita)
6. [Como a tela do terminal é desenhada](#6-como-a-tela-do-terminal-é-desenhada)
7. [Terminal local (sem SSH)](#7-terminal-local-sem-ssh)
8. [Criar, editar e excluir conexões](#8-criar-editar-e-excluir-conexões)
9. [Quando o config muda por fora](#9-quando-o-config-muda-por-fora)
10. [Autenticação, senhas e host keys](#10-autenticação-senhas-e-host-keys)
11. [ProxyJump, ProxyCommand e port forwarding](#11-proxyjump-proxycommand-e-port-forwarding)
12. [Abas, splits e reconexão](#12-abas-splits-e-reconexão)
13. [Onde ficam os arquivos](#13-onde-ficam-os-arquivos)
13b. [Compartilhamento em equipe](#13b-compartilhamento-em-equipe)
14. [Mapa do código: quero mudar X, onde mexo?](#14-mapa-do-código-quero-mudar-x-onde-mexo)
15. [Como estender (novo tipo de conexão)](#15-como-estender-novo-tipo-de-conexão)
16. [Testes e build](#16-testes-e-build)
17. [Problemas comuns](#17-problemas-comuns)

---

## 1. Rodando o projeto

Escolha **um** ambiente. Cada ambiente usa o seu próprio `~/.ssh/config`:

| Ambiente | SSH config lido | Terminal local |
|---|---|---|
| Windows nativo (PowerShell) | `C:\Users\<você>\.ssh\config` | PowerShell / cmd / WSL (ConPTY) |
| WSL / Linux | `/home/<você>/.ssh/config` (o do Linux!) | bash / zsh / fish (PTY) |

### Windows (PowerShell) — recomendado para usar os hosts do Windows

```powershell
cd C:\Users\<você>\Desktop\Projetos\sshdesk
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Se o PowerShell bloquear o `Activate.ps1`, rode uma vez `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

### WSL / Linux com **fish**

```fish
sudo apt install python3-venv python3-full libxcb-cursor0 libxkbcommon0 libegl1
python3 -m venv ~/.venvs/sshdesk
source ~/.venvs/sshdesk/bin/activate.fish
pip install -r requirements.txt
python app.py
```

### WSL / Linux com **bash/zsh**

```bash
sudo apt install python3-venv python3-full libxcb-cursor0 libxkbcommon0 libegl1
python3 -m venv ~/.venvs/sshdesk
source ~/.venvs/sshdesk/bin/activate
pip install -r requirements.txt
python app.py
```

Por que esses detalhes:

- No Debian/Ubuntu o comando é `python3`, e o `pip` global é bloqueado (“externally-managed-environment”), por isso é obrigatório usar um venv.
- No fish, o arquivo de ativação é `activate.fish`, e `.venv\Scripts\activate` só existe no Windows.
- O venv fica em `~/.venvs/...`, e não dentro de `/mnt/c/...`, porque ler milhares de arquivos do disco do Windows pelo WSL é muito lento.
- O WSL precisa do **WSLg** (já vem no Windows 11) para abrir janelas. Se der erro de plugin `xcb`, rode `wsl --update` no PowerShell.

### Opções de linha de comando

```bash
python app.py                          # abre a interface
python app.py server-prod              # abre e já conecta em server-prod
python app.py server-prod database     # duas abas
python app.py lucas@10.0.0.5:2222      # conexão avulsa (fora do config)
python app.py --local                  # abre com um terminal local
python app.py --list                   # só lista os hosts do config e sai
python app.py -F ~/outro/config        # usa outro arquivo de config
python app.py --debug                  # log detalhado no console
```

Depois de `pip install .`, os mesmos comandos funcionam como `sshdesk ...` ou `ssh-terminal ...`. `python -m ssh_terminal` também funciona.

### Primeira execução

1. Aparece a tela **Welcome**: “We found your SSH configuration. N connections detected” ou, se o arquivo não existir, a opção **Create SSH Config**.
2. A barra lateral mostra os hosts. Para conectar, dê **duplo clique** (ou Enter) num host.
3. Para um terminal local, use **Ctrl+T**.

---

## 2. Visão geral da arquitetura

```
┌───────────────────────────── UI (PySide6) ─────────────────────────────┐
│ MainWindow ── ConnectionPanel (sidebar)   TerminalTabs ── TerminalPane │
│      │        ConnectionDialog, Settings,          │        ├ Banner   │
│      │        TestConnection, Diagnostics...       │        └ TerminalWidget
└──────┼─────────────────────────────────────────────┼───────────────────┘
       │ services/                                    │ terminal/
       ├─ AppContext (monta tudo)                     ├─ TerminalSession (ponte Qt)
       ├─ ConfigService (CRUD do ~/.ssh/config)       ├─ TerminalEmulator (pyte)
       ├─ SettingsService (app_config.json)           └─ backends/
       ├─ ConnectionService (spec → backend)               ├─ SSHBackend      ─┐
       └─ KeyringCredentialStore (senhas)                  ├─ UnixPtyBackend   │
                                                           └─ WinPtyBackend    │
       ssh/                                                                    │
       ├─ config_parser / config_writer  (ler/gravar sem perder nada)          │
       ├─ resolver  (ssh -G ou resolvedor próprio)                             │
       ├─ ssh_session (SSHConnector: socket, ProxyJump, handshake) ◄───────────┘
       ├─ ssh_auth (chave, agente, senha, 2FA)   host_keys (known_hosts)
       └─ forwarding (-L/-R/-D)   sftp_session   proxy_command
```

Regras que guiam o projeto:

- **O SSH config é a fonte da verdade.** O app não guarda cópia própria de host, usuário, porta ou chave. Ele sempre relê o arquivo.
- **`app_config.json` guarda só metadados de UI:** tema, fonte, favoritos, recentes, grupos, atalhos e abas abertas.
- **A rede nunca roda na thread da interface.** Conexão, leitura, escrita e testes acontecem em threads próprias e voltam para a UI por **sinais Qt**.
- **O terminal visual não sabe de onde vêm os bytes.** Tanto faz SSH, PTY ou ConPTY: tudo é um `TerminalBackend`.

---

## 3. O que acontece quando o app abre

Arquivo de entrada: `app.py` → `src/ssh_terminal/main.py::main()`.

```
main()
 ├─ argparse lê os argumentos (--list sai aqui, sem abrir Qt)
 ├─ setup_logging()          → <pasta do app>/logs/app.log (com filtro que apaga senhas)
 ├─ QApplication(...)
 ├─ AppContext.create()      → services/app_context.py
 │    ├─ SettingsService.load()          lê app_config.json (se corrompido: renomeia p/ .broken e usa padrão)
 │    ├─ get_ssh_config_path()           ~/.ssh/config ou %USERPROFILE%\.ssh\config
 │    ├─ ConfigFileWriter(backups)       quem faz backup + escrita atômica
 │    ├─ ConfigService(path, writer)
 │    ├─ KeyringCredentialStore()        cofre do SO (lazy)
 │    ├─ SSHConfigResolver(path)         decide entre ssh -G e resolvedor próprio
 │    └─ SSHManager(...)                 fachada de conexão/teste
 └─ MainWindow(ctx, startup).show()
      ├─ monta sidebar + abas + menus + status bar
      ├─ apply_settings()   → tema, opacidade, atalhos, fontes
      ├─ reload_config()    → ConfigService.reload() → SSHConfigSet.load()
      │                       → sidebar.populate(hosts, wildcards, settings)
      ├─ _watch_config()    → QFileSystemWatcher no config (+ arquivos de Include)
      └─ _after_show()      → Welcome (1ª vez), conexões do CLI ou abas da última sessão
```

### Como o config é lido (`ssh/config_parser.py`)

1. O arquivo é lido como bytes e decodificado em UTF-8. O BOM e o tipo de quebra de linha (LF/CRLF) são registrados.
2. Cada linha vira um `ConfigLine` com o texto **original** (`raw`) e o tipo: em branco, comentário, opção ou inválida.
3. As linhas são agrupadas em blocos (`ConfigBlock`): o bloco global (antes do primeiro `Host`), os blocos `Host ...` e os blocos `Match ...`.
4. Comentários colados logo acima de um `Host` passam a pertencer àquele host (`leading`). Eles aparecem no tooltip e são excluídos junto com o host.
5. As diretivas `Include` são seguidas: os arquivos incluídos viram documentos separados dentro de um `SSHConfigSet`.
6. Cada bloco `Host` vira um `SSHHost` (`models/ssh_host.py`):
   - campos conhecidos (`HostName`, `User`, `Port`, `IdentityFile`...) vão para atributos;
   - **qualquer opção desconhecida** vai para `extra_options`, na ordem original.
7. Blocos em que **todos** os padrões são curinga (`Host *`, `Host *.example.com`) recebem `is_wildcard=True` e aparecem em **SSH DEFAULTS**, nunca como servidor.

Garantia: `SSHConfigDocument.parse(texto).render() == texto`, byte a byte. Há testes só para isso.

### Como a barra lateral é montada (`ui/connection_panel.py`)

Seções, nesta ordem, cada uma só aparece se tiver itens:

- **★ FAVORITES**: `settings.favorites`
- **RECENT**: `settings.recent_connections` (até 6)
- **Grupos**: um por entrada de `settings.groups` (só visual, nunca vai para o SSH config)
- **HOSTS**: servidores SSH (com shell) que não estão em grupo
- **GIT & SERVICES**: entradas que só servem para autenticar no Git (veja abaixo)
- **SSH DEFAULTS**: os blocos curinga

#### Servidor SSH × Git/serviço (`ssh/host_classifier.py`)

Muitos configs têm entradas que existem só para o Git usar a chave certa:

```sshconfig
Host github-pessoal
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_github_pessoal
```

Esses hosts autenticam via SSH, mas **não dão shell**. Por isso o app classifica cada host:

- **Git/serviço**, se `User` for `git` (ou `aur`, `hg`, `gitea`, `forgejo`) **ou** se `HostName` for um provedor Git conhecido (`github.com`, `gitlab.com`, `bitbucket.org`, `ssh.dev.azure.com`, `codeberg.org`, ...). Isso cobre GitLab/Gitea próprios com `User git`.
- **Servidor**, em todos os outros casos.

Para hosts Git/serviço:

- O **duplo clique** (ou Enter) **testa a autenticação** da chave, em vez de abrir um terminal. O diálogo mostra como usar o alias num remote: `git@github-pessoal:<owner>/<repo>.git`.
- O menu de contexto tem **Copy Git Remote Example** e **Open Terminal Anyway**.

Se a classificação errar, use o botão direito → **Treat as SSH Server** / **Treat as Git / Service**. A escolha fica em `host_roles` no `app_config.json`, nunca no SSH config.

Cada linha é desenhada por um `HostDelegate`: ícone, alias, `user@host:porta`, estrela e bolinha de status (verde = conectado, amarelo = conectando, vermelho = caiu ou falhou).

A **busca** filtra em tempo real por alias, aliases, hostname, usuário e grupo. Enter na busca conecta no primeiro resultado.

---

## 4. O que acontece quando você conecta num host

Ação: duplo clique em `server-prod` na sidebar.

```
GUI thread                                         thread "ssh-session"
──────────                                         ────────────────────
ConnectionPanel.connect_requested("server-prod")
MainWindow.connect_alias()
MainWindow.open_spec(ConnectionSpec.ssh("server-prod"))
 ├─ TerminalSession(spec, factory)        (terminal/terminal_session.py)
 ├─ TerminalPane(spec, session)           (ui/terminal_tabs.py)
 │    └─ TerminalWidget                   (terminal/terminal_widget.py)
 ├─ tabs.add_pane_tab(pane)  → aba nova com bolinha amarela
 └─ pane.start() → session.start(cols, rows)
        └─ factory() → ConnectionService._ssh_backend() → SSHBackend
        └─ SSHBackend.start() ───────────────────────► _run()
                                                       ├─ estado CONNECTING (banner "Connecting…")
                                                       ├─ resolve():
                                                       │    confirma que o alias existe no arquivo
                                                       │    SSHConfigResolver.resolve("server-prod")
                                                       │      → `ssh -G server-prod` (se houver OpenSSH)
                                                       │      → senão: resolvedor próprio
                                                       │    = ResolvedConfig(hostname, user, port, keys, proxy...)
                                                       ├─ SSHConnector.connect(resolved)   (ssh/ssh_session.py)
                                                       │    1. abre o socket:
                                                       │       ProxyJump?    → conecta no(s) jump(s) e abre canal direct-tcpip
                                                       │       ProxyCommand? → ProxyCommandSocket (processo)
                                                       │       senão         → socket.create_connection(host, porta)
                                                       │    2. paramiko.Transport(socket)
                                                       │    3. start_client() = handshake SSH
                                                       │    4. verifica a host key no known_hosts (seção 10)
                                                       │    5. Authenticator.authenticate() (seção 10)
                                                       │    6. keepalive (ServerAliveInterval)
                                                       ├─ connection.open_shell("xterm-256color", cols, rows)
                                                       │    open_session → get_pty → (ForwardAgent) → invoke_shell
                                                       ├─ inicia a thread "ssh-writer"
                                                       ├─ inicia os forwards do config (LocalForward etc.)
                                                       ├─ estado CONNECTED
                                                       └─ _read_loop(): chan.recv(64k) em loop
                                                            └─ emit_data(bytes) ─┐
◄────────────── sinal Qt (enfileirado na GUI thread) ◄─────────────────────────────┘
TerminalSession.data_received(bytes)
TerminalWidget.feed(bytes) → pyte → repaint
```

Detalhes importantes:

- **Por que `ssh -G`?** O OpenSSH junta vários blocos: `Host server-prod`, `Host *.example.com`, `Host *`, `Match`, `Include`. Rodando `ssh -G alias`, recebemos **exatamente** a configuração que o `ssh` usaria. Se o OpenSSH não existir, o `resolver.py` aplica as mesmas regras (o primeiro valor encontrado vence; `IdentityFile` e `LocalForward` acumulam; padrões com `*`, `?` e `!`).
- **`-F` automático:** o OpenSSH procura o config na home do sistema (passwd), não no `$HOME`. Se o arquivo que o app gerencia for outro, o resolver passa `-F <arquivo>` para os dois enxergarem o mesmo.
- **Mensagens de progresso** (“Connecting to jump host…”, “SSH handshake…”, “Authenticating as…”) aparecem no banner amarelo da aba.
- **Erro na conexão:** a exceção vira um `FriendlyError` (`errors.py`, com título, mensagem, causas prováveis e detalhes técnicos) e a aba mostra um banner vermelho com os botões **Details**, **Edit Connection** e **Reconnect**.
- Quando conecta, o alias entra em **RECENT** e a bolinha fica verde.

---

## 5. O que acontece quando você digita

```
tecla
 └─ TerminalWidget.event(ShortcutOverride)
      ├─ é um atalho do app (Ctrl+T, Ctrl+Shift+C...)? → deixa o QAction tratar
      └─ senão → o terminal "segura" a tecla
 └─ TerminalWidget.keyPressEvent
      ├─ Shift+PgUp/PgDn/Home/End → rolagem local do histórico (não envia nada)
      └─ keymap.key_to_bytes(tecla, modificadores, texto, modo_cursor_app)
           Enter→"\r"  Backspace→"\x7f"  Ctrl+C→"\x03"  ↑→"\x1b[A" (ou "\x1bOA")
           Alt+x→"\x1bx"  F5→"\x1b[15~"  AltGr+2 (ABNT2)→"@"
 └─ sinal input_ready(bytes) → TerminalSession.write → SSHBackend.write
      └─ coloca na fila → thread "ssh-writer" → chan.sendall()
```

- **Por que uma fila e uma thread para escrever?** `sendall` bloqueia quando o servidor não está lendo (colar um texto enorme, por exemplo). Se isso rodasse na thread da UI, a janela congelaria.
- **Colar:** quebras de linha viram `\r`. Se o programa remoto ativou o bracketed paste (`ESC[?2004h`), o texto vai entre `ESC[200~ ... ESC[201~`, então o shell não executa linha por linha.
- **Acentos e teclas mortas:** quando o sistema usa um método de entrada (IBus, teclas mortas), o texto chega por `inputMethodEvent` e é enviado em UTF-8.
- **Mouse:** se o programa pediu eventos de mouse (vim, htop, tmux com `mouse on`, no modo SGR 1006), cliques e roda vão para ele. Segure **Shift** para selecionar texto mesmo assim.

---

## 6. Como a tela do terminal é desenhada

Arquivos: `terminal/terminal_emulator.py` e `terminal/terminal_widget.py`.

1. Os bytes recebidos entram num buffer. Um timer processa até 128 KB por ciclo do event loop, então um `cat arquivo-gigante` não trava a interface.
2. O **pyte** interpreta as sequências ANSI/xterm e atualiza uma grade `linhas × colunas` de `Char(data, fg, bg, bold, italics, underscore, reverse...)`.
3. A classe `_Screen` estende o pyte com:
   - **Scrollback:** a linha que sai pelo topo vai para uma `deque` com o tamanho configurado.
   - **Tela alternativa** (`?1049h`): vim, less e htop desenham numa tela separada, e ao sair o conteúdo anterior volta intacto.
   - **Redimensionamento inteligente:** encolher empurra linhas para o scrollback mantendo o cursor visível; crescer puxa de volta.
   - **Respostas ao programa** (posição do cursor, tipo de terminal), título da janela e bell.
4. `paintEvent` percorre só as linhas visíveis, agrupa células vizinhas com o mesmo estilo em “runs” e desenha cada run com um `fillRect` (fundo) e um `drawText` (texto). Caracteres não-ASCII (acentos, CJK, emojis, desenho de caixa) são desenhados célula a célula para não desalinhar a grade.
5. As cores vêm do esquema escolhido (`color_schemes.py`): 16 cores ANSI, mais 256 cores e truecolor em hexadecimal. Texto em negrito usa a variante “bright”.
6. Por cima vêm o cursor (bloco, sublinhado ou barra, piscando ou não; contorno quando sem foco), a seleção e o flash do bell visual.
7. Quando a janela muda de tamanho, o widget recalcula colunas e linhas, ajusta o pyte e, 40 ms depois, avisa o backend (`resize_pty` no SSH, `TIOCSWINSZ` no PTY, `setwinsize` no ConPTY). Assim o `vim` e o `htop` redesenham no tamanho certo.

---

## 7. Terminal local (sem SSH)

`Ctrl+T`, botão **Local Terminal** ou o menu **+** da barra de abas.

- **Linux/WSL** (`backends/local_unix.py`): `pty.fork()` cria um pseudo-terminal e o processo filho executa o shell (`$SHELL`, ou bash/zsh/fish detectados). Usamos `pty.fork` em vez de `subprocess` porque o shell precisa de **terminal de controle**; sem ele, Ctrl+C e Ctrl+Z não funcionam. Uma thread lê o PTY com `select`/`os.read`.
- **Windows** (`backends/local_windows.py`): o **ConPTY** é acessado via `pywinpty` (`PtyProcess.spawn`). Os perfis detectados são PowerShell 7, Windows PowerShell, cmd e WSL.
- O shell padrão pode ser escolhido em *Settings › General › Local terminal*.

---

## 8. Criar, editar e excluir conexões

### Criar (`+ New Connection`)

1. `ConnectionDialog` (abas **General**, **Advanced** e **Port Forwarding**) valida nome único sem espaços, porta entre 1 e 65535, existência das chaves e formato dos forwards.
2. O tipo de autenticação escolhido define o que é escrito:

   | Escolha | O que vai para o config |
   |---|---|
   | SSH Key | `IdentityFile` (uma ou mais) |
   | Password | `PreferredAuthentications password,keyboard-interactive` (a senha **não** vai para o arquivo; ela vai para o keyring se você marcar a opção) |
   | Agent / Automatic | nada de chave: usa o agente e as chaves padrão |

3. `ConfigService.add_host()`:
   - **relê o arquivo do disco**, para não perder edições externas;
   - insere o bloco **antes do `Host *` final** (no OpenSSH o primeiro valor vence, então um `Host *` antes anularia o host novo);
   - grava (veja “Como a gravação é segura”, abaixo) e recarrega a sidebar.

Resultado no arquivo:

```sshconfig
Host production-server
    HostName 10.0.0.10
    User lucas
    Port 22
    IdentityFile ~/.ssh/id_ed25519
```

### Editar

`config_writer.sync_block()` compara o que existe no bloco com o que o formulário pede:

- linha com o **mesmo valor** é mantida **exatamente igual** (indentação, maiúsculas, `=`);
- linha com valor diferente é reescrita no mesmo lugar;
- opção removida no formulário tem a linha removida;
- opção nova entra depois da última opção do bloco;
- opções desconhecidas (`CustomOption something`) passam pelo formulário em *Advanced › Other options* e **continuam no arquivo**;
- comentários e linhas em branco dentro do bloco ficam onde estavam.

Exemplo: mudar a porta de `server-prod` de 22 para 2222 altera **uma única linha** do arquivo.

### Excluir

Pede confirmação (“Are you sure you want to remove "server-prod" from your SSH configuration?”), remove o bloco e os comentários colados acima dele, e limpa favoritos, recentes e grupos daquele alias.

### Duplicate / Rename

- **Duplicate:** cria `alias-copy` e abre o editor.
- **Rename:** muda a linha `Host`, preserva os aliases extras e migra favoritos e grupos, inclusive nas abas abertas.

### Como a gravação é segura (`ConfigFileWriter.write`)

1. Faz backup em `config.bak` (ao lado do arquivo) e em `backups/config.<data-hora>.bak` na pasta do app. Mantém os últimos 20 (configurável).
2. Escreve o conteúdo novo num arquivo temporário **na mesma pasta**, com `flush` e `fsync`.
3. Aplica a mesma permissão do original (`0600` para arquivo novo; no Linux, a pasta `.ssh` fica com `0700`).
4. Usa `os.replace(temp, config)`, uma troca **atômica**: não existe momento com arquivo pela metade. No Windows tenta de novo se um antivírus ou editor estiver segurando o arquivo.
5. Se `config` for um symlink (dotfiles), o arquivo real é atualizado e o link continua link.

---

## 9. Quando o config muda por fora

- O `QFileSystemWatcher` observa o config, os arquivos de `Include` e as pastas deles. Observar a pasta é necessário porque editores salvam trocando o arquivo, e isso faz o watcher perder o arquivo antigo.
- Depois de 400 ms sem novos eventos, o app compara o **SHA-256** do disco com o da última leitura ou escrita do próprio app. Assim as gravações do próprio app não disparam aviso.
- Se mudou, aparece um diálogo com três opções:
  - **Reload:** relê o arquivo e atualiza a sidebar.
  - **Keep Current:** continua mostrando o que estava. Mesmo assim, a próxima edição pelo app relê o disco antes de alterar, então nada externo é perdido.
  - **View Differences:** mostra um diff colorido entre o que o app tinha e o que está no disco.
- Em *Settings › General* dá para desligar o watcher ou recarregar automaticamente sem perguntar. `F5` sempre recarrega.

---

## 10. Autenticação, senhas e host keys

### Host key (`ssh/host_keys.py`)

1. Lê o `known_hosts` do usuário e do sistema. Linhas com hash e marcadores são toleradas.
2. Monta o identificador do host: `host`, ou `[host]:porta` quando a porta não é 22, ou o `HostKeyAlias`.
3. Prioriza o algoritmo da chave que já conhecemos, como o OpenSSH faz.
4. Compara a chave recebida:
   - **conhecida e igual:** segue;
   - **conhecida e diferente:** **bloqueia** com o aviso “HAS CHANGED”, e *View technical details* mostra o `ssh-keygen -R` para corrigir;
   - **desconhecida:** depende de `StrictHostKeyChecking`:
     - `ask` (padrão): mostra diálogo com o fingerprint SHA256;
     - `accept-new` ou `no`: aceita e grava;
     - `yes`: recusa.
5. Para gravar, o app só **acrescenta uma linha** ao `known_hosts`. Ele nunca reescreve o arquivo.

### Autenticação (`ssh/ssh_auth.py`)

A ordem segue `PreferredAuthentications` (padrão: `publickey,keyboard-interactive,password`), e o app só tenta o que o servidor aceita.

1. **publickey**
   - Primeiro, as `IdentityFile` do config. Se o agente já tem a mesma chave (comparada pelo `.pub`), usa a do agente e não pede passphrase.
   - Chave com passphrase: procura no keyring; se não achar, pergunta (até 3 tentativas). Certificados `id-cert.pub` são carregados automaticamente.
   - Depois, as demais chaves do agente (exceto com `IdentitiesOnly yes`).
2. **keyboard-interactive:** se o prompt for só “Password:”, usa a senha salva ou pergunta. Se houver outros campos (código 2FA ou OTP), abre um formulário. O limite de espera é 5 minutos, para dar tempo de pegar o código.
3. **password:** usa a senha do keyring ou pergunta. Se a senha salva estiver errada, ela é apagada e o app pergunta de novo.
4. Se o servidor pedir **dois fatores** (chave e OTP), o app continua para o próximo método.

### Como a thread de conexão “pergunta” ao usuário (`ui/prompter.py`)

A autenticação roda numa thread de fundo, e diálogos só podem ser abertos na thread da UI. Por isso, o `QtAuthPrompter` funciona assim:

1. emite um sinal com a pergunta;
2. a UI abre o diálogo;
3. a thread de conexão fica esperando num `threading.Event`;
4. a resposta volta para a thread de conexão.

Um lock garante uma pergunta por vez, mesmo com várias abas conectando ao mesmo tempo.

### Onde as senhas ficam

- **Nunca** em arquivo, nunca no log.
- Só no cofre do sistema, e só se você marcar “Save in system keyring”:
  - Windows: Credential Manager, entradas “SSHDesk”;
  - Linux: Secret Service (GNOME Keyring ou KWallet).
- `credentials_index.json` guarda **só os nomes** das entradas, sem segredo, para permitir “Clear saved credentials” em *Settings › Security*.

---

## 11. ProxyJump, ProxyCommand e port forwarding

### ProxyJump (nativo, sem precisar do binário `ssh`)

```
Host database
    ProxyJump bastion
```

1. Resolve `bastion` (com a própria config dele) e conecta normalmente.
2. Pede ao `bastion` um canal `direct-tcpip` até `10.0.0.20:22`.
3. Usa esse canal como se fosse o socket da conexão com `database`.

Também funciona com vários saltos (`ProxyJump a,b,c`) e com o formato `user@host:porta`. Laços (A pula por B, que pula por A) são detectados com um limite de 8 saltos. Ao fechar a aba, as conexões intermediárias são fechadas junto.

### ProxyCommand

O comando (`%h`, `%p`, `%r` e `%n` são substituídos) é executado como processo, e seus stdin/stdout viram o “socket”. A implementação é própria (`ssh/proxy_command.py`) porque a do Paramiko não funciona no Windows. No Windows o processo roda sem abrir janela de console.

### Port forwarding (`ssh/forwarding.py`)

| Tipo | Equivale a | Como funciona |
|---|---|---|
| Local | `ssh -L 8080:localhost:80` | escuta em `127.0.0.1:8080`; cada conexão abre um canal `direct-tcpip` até o destino |
| Remote | `ssh -R 9000:localhost:3000` | pede ao servidor para escutar; conexões voltam por `forwarded-tcpip` e o app conecta no destino local |
| Dynamic | `ssh -D 1080` | proxy SOCKS 4/4a/5 local; o destino de cada conexão é lido do protocolo SOCKS |

- **Do config:** `LocalForward`, `RemoteForward` e `DynamicForward` sobem sozinhos ao conectar. Isso pode ser desligado em *Settings › SSH*.
- **Na mão:** menu *SSH › Port Forwarding…* (ou botão direito no terminal), com a lista dos ativos, contador de conexões e botão **Stop**.
- Cada conexão encaminhada usa duas threads (uma por direção) e respeita o *half-close*: quando um lado termina de enviar, o outro ainda pode responder.

---

## 12. Abas, splits e reconexão

- **Aba** = `TabPage`, uma árvore de `QSplitter` com um ou mais `TerminalPane`.
- **Split** (`Ctrl+Shift+\`, `Ctrl+Shift+-`): abre **outra sessão da mesma conexão** ao lado ou abaixo. Splits podem ser aninhados.
- **Fechar** (`Ctrl+W`): fecha o painel ativo; se for o último, fecha a aba. A conexão é encerrada de forma limpa.
- **Reabrir** (`Ctrl+Shift+T`): reabre a última aba fechada, guardada numa pilha com as 20 últimas.
- **Ícone da aba:** 🟢 conectado, 🟡 conectando, 🔴 caiu ou falhou.
- Como o app classifica o fim de uma sessão:

  | Situação | Classificação | Auto-reconnect? |
  |---|---|---|
  | `exit` no shell (o servidor manda um *exit status*) | “Session ended (exit status 0)” | não |
  | rede caiu, servidor reiniciou, processo morto | “Connection lost” | sim |
  | você fechou | silencioso | não |

- **Reconnect:** o botão no banner (ou *SSH › Reconnect*) cria um backend novo na **mesma aba**, mantendo o histórico.
- **Auto-reconnect** (*Settings › SSH*): só para “Connection lost”, com espera crescente (1 s, 2 s, 4 s... até 30 s) e número máximo de tentativas configurável.

---

## 13. Onde ficam os arquivos

| O quê | Linux / WSL | Windows |
|---|---|---|
| SSH config (fonte da verdade) | `~/.ssh/config` | `%USERPROFILE%\.ssh\config` |
| Backup ao lado | `~/.ssh/config.bak` | `%USERPROFILE%\.ssh\config.bak` |
| known_hosts | `~/.ssh/known_hosts` | `%USERPROFILE%\.ssh\known_hosts` |
| Settings do app | `~/.config/sshdesk/app_config.json` | `%APPDATA%\SSHDesk\app_config.json` |
| Backups com data | `~/.config/sshdesk/backups/` | `%APPDATA%\SSHDesk\backups\` |
| Log | `~/.config/sshdesk/logs/app.log` | `%APPDATA%\SSHDesk\logs\app.log` |
| Senhas | Secret Service | Credential Manager |

A variável `SSHDESK_HOME` muda a pasta do app (os testes usam isso para nunca tocar nos seus arquivos).

Exemplo de `app_config.json`:

```json
{
    "theme": "dark",
    "color_scheme": "Default Dark",
    "font_family": "JetBrains Mono",
    "font_size": 13,
    "sidebar_width": 280,
    "favorites": ["server-prod"],
    "recent_connections": ["server-hml"],
    "groups": {"Production": ["server-prod", "api-prod"]},
    "keybindings": {"close_tab": "Ctrl+W"}
}
```

---

## 13b. Compartilhamento em equipe

Opcional. Precisa de um **SSHDesk Server** (pasta `server/`, veja `server/README.md`). Sem servidor, nada muda.

### Como os hosts do time chegam no seu computador

```
Account › Sign In            → token no keyring do sistema
TeamService.sync()           → GET /api/v1/sync (ETag; 304 se nada mudou)
  ├─ valida cada host de novo (allow-list; nada que execute comando)
  ├─ pula alias que já existe no SEU config (o seu sempre vence) → aviso "conflict"
  ├─ escreve <pasta do app>/teams/<time>.conf   (sintaxe OpenSSH, arquivo gerenciado)
  ├─ escreve <pasta do app>/teams/known_hosts   (host keys publicadas pelo time)
  └─ garante no topo do ~/.ssh/config:
       # SSHDesk teams (managed ...)
       Include <pasta do app>/teams/*.conf
```

Consequências:

- O `~/.ssh/config` continua sendo a fonte da verdade, porque os hosts do time entram por `Include`. Por isso eles funcionam também no `ssh`, `scp` e `git` do terminal, não só no app.
- O `Include` fica **antes** do primeiro `Host`. Se ficasse no fim, pertenceria ao último bloco.
- Na sidebar, cada time vira uma seção **TEAM · NOME**, com o mesmo duplo clique, abas e splits dos hosts pessoais.
- Os arquivos do time são **reescritos a cada sync**. Não edite à mão: use **Edit in Team** (admins).
- **Sign Out** apaga os arquivos do time. A linha `Include` pode ficar: sem arquivos, ela não faz nada.

### Ações

| Onde | Ação |
|---|---|
| *Account › Create Account / Sign In* | conta no servidor (URL configurável no próprio diálogo) |
| *Account › Teams…* | criar time, aceitar convite, convidar por email, papéis, membros, sair/excluir |
| Botão direito num host pessoal › *Share with Team* | publica o host (sem segredos) num time em que você é admin |
| Botão direito num host do time | *Edit in Team* / *Remove from Team* (admin) ou *Managed by team* (membro); *Duplicate as Personal Host* para ter uma cópia sua |
| *Account › Sync Team Hosts* | força o sync (também roda ao abrir e a cada 5 min) |

### Código

- `services/team_service.py`: cliente HTTP (stdlib), sync, escrita dos `.conf`, `Include`, validação local.
- `ui/team_dialogs.py`: diálogos de conta, de times e de compartilhar host.
- `server/`: FastAPI, SQLAlchemy, Argon2, JWT e SMTP opcional.

---

## 14. Mapa do código: quero mudar X, onde mexo?

| Quero... | Arquivo |
|---|---|
| mudar cores e estilos da interface | `ui/theme.py` (paletas `DARK`/`LIGHT` + stylesheet) |
| adicionar um esquema de cores do terminal | `terminal/color_schemes.py`: só acrescentar um `ColorScheme` em `SCHEMES` |
| mudar atalhos padrão | `models/app_settings.py` → `DEFAULT_KEYBINDINGS` |
| mudar o que cada tecla envia | `terminal/keymap.py` |
| mudar o desenho do terminal (fonte, cursor, seleção) | `terminal/terminal_widget.py` |
| suportar uma sequência de escape nova | `terminal/terminal_emulator.py` (classe `_Screen`) |
| suportar um campo novo do SSH config no formulário | `models/ssh_host.py` (`KNOWN_KEYWORDS`, `to_options`, `from_options`) + `ui/connection_dialog.py` |
| mudar como o arquivo é gravado | `ssh/config_writer.py` |
| mudar a resolução de configuração | `ssh/resolver.py` |
| mudar a lógica de login | `ssh/ssh_auth.py` |
| mudar a conexão (timeout, ProxyJump) | `ssh/ssh_session.py` |
| mudar a sidebar | `ui/connection_panel.py` |
| mudar menus, status bar ou ações | `ui/main_window.py` (`_build_actions`, `_build_menus`) |
| mudar mensagens de erro | `errors.py` → `describe_exception` |
| mudar a tela de Settings | `ui/settings_dialog.py` + campo em `models/app_settings.py` |

---

## 15. Como estender (novo tipo de conexão)

O widget, as abas, os splits e o reconnect não sabem o que é SSH. Exemplo: um backend para `docker exec`.

```python
# src/ssh_terminal/terminal/backends/docker_backend.py
from ssh_terminal.terminal.backends.local_unix import UnixPtyBackend


class DockerExecBackend(UnixPtyBackend):
    """Shell dentro de um container: reaproveita o PTY local."""

    def __init__(self, container: str) -> None:
        super().__init__(["docker", "exec", "-it", container, "sh", "-lc", "exec bash || exec sh"])
        self.container = container

    @property
    def description(self) -> str:
        return f"docker:{self.container}"
```

Depois:

1. Acrescente `DOCKER = "docker"` em `ConnectionKind` (`models/connection.py`).
2. Em `ConnectionService.backend_factory` (`services/connection_service.py`), retorne `DockerExecBackend(spec.alias)` para esse tipo.
3. Para listar os containers, crie uma seção nova em `ConnectionPanel.populate`.

Um backend do zero (serial, telnet, `kubectl exec`) implementa só quatro métodos de `TerminalBackend`: `start`, `write`, `resize` e `close`. Ele avisa a UI chamando `emit_data`, `emit_state` e `emit_closed`. Pode chamá-los de qualquer thread, porque a `TerminalSession` cuida de levar tudo para a UI.

Para SFTP, `ssh/sftp_session.py` já oferece `SFTPSession(conexão)` com `listdir`, `upload`, `download`, `mkdir`, `remove` e `rename`, reaproveitando a conexão já autenticada da aba (`backend.connection`).

---

## 16. Testes e build

```bash
pip install -r requirements-dev.txt
ruff check src tests        # lint
python -m pytest            # 126 testes, sem servidor SSH
```

- Os testes usam um `HOME` temporário: seu `~/.ssh/config` real **nunca** é alterado.
- Os testes de interface rodam com `QT_QPA_PLATFORM=offscreen` (sem janela).
- **Teste ponta a ponta com sshd real** (em VM ou container descartável, porque o script troca a senha do root):

  ```bash
  sudo scripts/start_test_sshd.sh /tmp/sshdesk-sshd
  SSHDESK_LIVE_SSHD=/tmp/sshdesk-sshd python -m pytest tests/integration
  ```

### Gerar executável (o PyInstaller não faz cross-compile)

```bash
pyinstaller --noconfirm --clean sshdesk.spec
```

- No Windows gera `dist\SSHDesk.exe`. Há um script pronto: `scripts\build_windows.ps1`.
- No Linux gera `dist/SSHDesk`. Há um script pronto: `scripts/build_linux.sh`.
- O GitHub Actions (`.github/workflows/build.yml`) gera os dois a cada push.

---

## 17. Problemas comuns

| Sintoma | Causa / solução |
|---|---|
| `Command 'python' not found` (WSL) | Use `python3`, ou rode `sudo apt install python-is-python3`. |
| `externally-managed-environment` | O Debian bloqueia `pip` global. Crie o venv: `python3 -m venv ~/.venvs/sshdesk`. |
| `.venvScriptsctivate: command not found` no fish | Esse é o comando do Windows. No fish use `source ~/.venvs/sshdesk/bin/activate.fish`. |
| `ensurepip is not available` | `sudo apt install python3-venv python3-full` |
| `Could not load the Qt platform plugin "xcb"` | `sudo apt install libxcb-cursor0 libxkbcommon0 libegl1`; no WSL, rode `wsl --update` (WSLg). |
| No WSL os hosts não aparecem | O WSL lê o config do Linux (`~/.ssh/config`). Copie o do Windows (`cp /mnt/c/Users/<você>/.ssh/config ~/.ssh/`) ou rode o app no Windows nativo. |
| “Host key … has CHANGED” | O servidor foi reinstalado ou o IP mudou. Confira e rode o `ssh-keygen -R` mostrado nos detalhes. |
| “Authentication failed” | Clique em **Details**: mostra os métodos tentados e os que o servidor aceita. Confira o `User`, o `authorized_keys` e o agente (*Help › Diagnostics*). |
| Passphrase pedida toda vez | Marque “Save in system keyring” ou carregue a chave no agente (`ssh-add`). |
| Ctrl+W fecha a aba em vez de apagar a palavra | Troque em *Settings › Keyboard* para `Ctrl+Shift+W`. |
| Terminal local não abre no Windows | `pip install pywinpty` (requer Windows 10 1809+). |
| Quero ver o que aconteceu | Rode com `--debug` e abra *Help › Open Log Folder*. |
