#!/usr/bin/env bash
# Start two throw-away sshd instances on 127.0.0.1:2222 and :2223 for the live
# integration tests (tests/integration). Requires openssh-server and root
# (or a user allowed to run sshd). Usage:
#   sudo scripts/start_test_sshd.sh /tmp/sshdesk-sshd
#   SSHDESK_LIVE_SSHD=/tmp/sshdesk-sshd pytest tests/integration
# WARNING: sets the root password to "rootpw123" for password-auth tests; only
# run it in a disposable VM/container.
set -euo pipefail
D="${1:-/tmp/sshdesk-sshd}"
mkdir -p "$D" /run/sshd
[ -f "$D/hostkey" ] || ssh-keygen -q -t ed25519 -N '' -f "$D/hostkey"
[ -f "$D/client_key" ] || ssh-keygen -q -t ed25519 -N '' -f "$D/client_key"
[ -f "$D/client_enc" ] || ssh-keygen -q -t rsa -b 2048 -N 'secret123' -f "$D/client_enc"
cat "$D/client_key.pub" "$D/client_enc.pub" > "$D/authorized_keys"
chmod 600 "$D/authorized_keys"
echo 'root:rootpw123' | chpasswd
for port in 2222 2223; do
cat > "$D/sshd_$port.conf" <<CONF
Port $port
ListenAddress 127.0.0.1
HostKey $D/hostkey
PidFile $D/sshd_$port.pid
PermitRootLogin yes
PubkeyAuthentication yes
PasswordAuthentication yes
KbdInteractiveAuthentication no
AuthorizedKeysFile $D/authorized_keys
StrictModes no
AllowTcpForwarding yes
UsePAM yes
Subsystem sftp internal-sftp
LogLevel ERROR
CONF
/usr/sbin/sshd -f "$D/sshd_$port.conf"
done
echo "sshd running on 127.0.0.1:2222 and 127.0.0.1:2223 (state in $D)"
