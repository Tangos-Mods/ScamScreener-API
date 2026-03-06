from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from ..config.settings import TrainingHubSettings


def _send_message(settings: TrainingHubSettings, message: EmailMessage) -> None:
    timeout_seconds = 15
    if settings.smtp_use_tls:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(
            settings.smtp_host,
            settings.smtp_port,
            timeout=timeout_seconds,
            context=context,
        ) as smtp:
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)
        return

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=timeout_seconds) as smtp:
        smtp.ehlo()
        if settings.smtp_use_starttls:
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)


def send_password_reset_email(
    settings: TrainingHubSettings,
    recipient_email: str,
    reset_link: str,
    expires_at: str,
) -> None:
    message = EmailMessage()
    message["From"] = settings.smtp_from_email
    message["To"] = recipient_email
    message["Subject"] = "ScamScreener Password Reset"
    message.set_content(
        (
            "A password reset was requested for your ScamScreener account.\n\n"
            f"Reset link: {reset_link}\n"
            f"Expires at (UTC): {expires_at}\n\n"
            "If you did not request this, you can ignore this message."
        )
    )

    _send_message(settings, message)


def send_admin_mfa_email(
    settings: TrainingHubSettings,
    recipient_email: str,
    code: str,
    expires_at: str,
) -> None:
    message = EmailMessage()
    message["From"] = settings.smtp_from_email
    message["To"] = recipient_email
    message["Subject"] = "ScamScreener Admin Verification Code"
    message.set_content(
        (
            "A login to the ScamScreener admin area requires verification.\n\n"
            f"Your one-time code: {code}\n"
            f"Expires at (UTC): {expires_at}\n\n"
            "If this was not you, change your password immediately."
        )
    )

    _send_message(settings, message)

