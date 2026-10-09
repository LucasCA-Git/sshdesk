# Changelog

All notable changes to SSHDesk. The newest version is at the top.
Format: `## [version] - YYYY-MM-DD`, then `### Section` and `- item` lines.

## [1.1.0] - 2026-10-08

### New
- Split view with any host: put two different terminals side by side (or below) — another SSH host, a local shell or the same connection.
- Broadcast input (Ctrl+Shift+I): type once and send it to every terminal of the tab.
- Files (SFTP): browse this computer and your servers side by side, like a file manager.
- Drag and drop files from Explorer / your file manager to upload, between panels to download or copy host to host, or onto a folder to move.
- Save password: tick "Save password" once and stop retyping it. Stored in the system keyring, or in an encrypted local vault when there is none (WSL, servers).
- Themes panel with new color schemes (Midnight, Kanagawa, Everforest, Hacker…) and per-terminal colors.
- Share a whole group with your team, and pick the group when sharing a single host. Team members see the same groups.
- The hosts sidebar has a grip: drag it to resize, collapse it to the edge and pull it back anytime.
- "What's New" window, shown once after each update (also in Help › What's New).

### Improved
- New Termius-like look: terminal cards, pill tabs and a calmer dark palette.
- Arrange all terminals of a tab as a grid (Ctrl+Shift+G).
- No duplicate hosts: a team host is skipped when you already have it (same name or same server), the same server is never synced twice from two teams, and new or edited connections that would repeat an existing one are blocked with a clear message. Names are compared case-insensitively, like OpenSSH.

### Fixed
- Closing a tab while it was still connecting could crash the app on Windows.
- Splitting a pane down inside a pane already split to the right could lose terminals.

## [1.0.0] - 2026-10-08

### New
- First release: your `~/.ssh/config` is the source of truth — no import, no lock-in.
- Tabs, split panes, reconnect, ProxyJump, port forwarding and host key checks.
- Create, edit, duplicate and delete hosts with automatic backups and atomic writes.
- Git endpoints (github.com…) are recognised and kept apart from servers.
- Teams: create an account, invite people by email + code and share hosts from your own self-hosted server.
