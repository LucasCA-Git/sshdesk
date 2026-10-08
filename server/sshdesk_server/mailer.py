"""Invite emails over SMTP (optional: without SMTP the admin shares the code manually)."""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from sshdesk_server.config import Settings

log = logging.getLogger(__name__)


def send_invite(settings: Settings, to: str, team_name: str, inviter: str, code: str) -> bool:
    if not settings.smtp_enabled:
        return False
    msg = EmailMessage()
    msg["Subject"] = f"Convite para o time {team_name} no SSHDesk"
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg.set_content(
        f"""Olá!

{inviter} convidou você para o time "{team_name}" no SSHDesk.

1. Abra o SSHDesk e use Account > Sign In (ou Create Account) com este email: {to}
   Servidor: {settings.public_url}
2. Vá em Account > Teams > Accept Invite e informe o código:

    {code}

O código vale {settings.invite_ttl_days} dias e só pode ser usado uma vez.
Se você não esperava este convite, ignore este email.
"""
    )
    try:
        if settings.smtp_tls == "ssl":
            server: smtplib.SMTP = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=ssl.create_default_context(), timeout=15)
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15)
        with server:
            if settings.smtp_tls == "starttls":
                server.starttls(context=ssl.create_default_context())
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)
        log.info("Invite email sent to %s", to)
        return True
    except (OSError, smtplib.SMTPException) as exc:
        log.error("Could not send invite email to %s: %s", to, exc)
        return False
