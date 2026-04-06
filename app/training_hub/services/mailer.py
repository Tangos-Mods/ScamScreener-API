from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import smtplib
import ssl
from email.message import EmailMessage
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..config.settings import TrainingHubSettings
from ..core.common import _format_utc_timestamp


@lru_cache(maxsize=1)
def _email_template_environment() -> Environment:
    base_dir = Path(__file__).resolve().parents[3]
    return Environment(
        loader=FileSystemLoader(str(base_dir / "sites" / "emails")),
        autoescape=select_autoescape(enabled_extensions=("html", "xml")),
        auto_reload=False,
    )


def _site_label(settings: TrainingHubSettings) -> str:
    if settings.public_base_url:
        host = (urlsplit(settings.public_base_url).hostname or "").strip()
        if host:
            return host
    return "ScamScreener"


def _render_email_html(template_name: str, **context: str) -> str:
    template = _email_template_environment().get_template(template_name)
    return template.render(**context)


def _build_email_message(
    settings: TrainingHubSettings,
    *,
    recipient_email: str,
    subject: str,
    plain_text: str,
    html_template_name: str,
    html_context: dict[str, str],
    attachments: list[dict[str, object]] | None = None,
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = settings.smtp_from_email
    message["To"] = recipient_email
    message["Subject"] = subject
    message.set_content(plain_text)
    message.add_alternative(_render_email_html(html_template_name, **html_context), subtype="html")
    for attachment in attachments or []:
        message.add_attachment(
            bytes(attachment.get("content", b"")),
            maintype=str(attachment.get("maintype", "application")),
            subtype=str(attachment.get("subtype", "octet-stream")),
            filename=str(attachment.get("filename", "attachment.bin")),
        )
    return message


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
    subject = "ScamScreener Password Reset"
    formatted_expires_at = _format_utc_timestamp(expires_at)
    message = _build_email_message(
        settings,
        recipient_email=recipient_email,
        subject=subject,
        plain_text=(
            "A password reset was requested for your ScamScreener account.\n\n"
            f"Reset link: {reset_link}\n"
            f"Expires at (UTC): {formatted_expires_at}\n\n"
            "If you did not request this, you can ignore this message."
        ),
        html_template_name="password_reset_email.html",
        html_context={
            "subject": subject,
            "site_label": _site_label(settings),
            "site_url": settings.public_base_url,
            "reset_link": reset_link,
            "expires_at": formatted_expires_at,
            "support_email": settings.smtp_from_email,
        },
    )

    _send_message(settings, message)


def send_admin_mfa_email(
    settings: TrainingHubSettings,
    recipient_email: str,
    code: str,
    expires_at: str,
) -> None:
    subject = "ScamScreener Admin Verification Code"
    formatted_expires_at = _format_utc_timestamp(expires_at)
    message = _build_email_message(
        settings,
        recipient_email=recipient_email,
        subject=subject,
        plain_text=(
            "A login to the ScamScreener admin area requires verification.\n\n"
            f"Your one-time code: {code}\n"
            f"Expires at (UTC): {formatted_expires_at}\n\n"
            "If this was not you, change your password immediately."
        ),
        html_template_name="admin_mfa_email.html",
        html_context={
            "subject": subject,
            "site_label": _site_label(settings),
            "site_url": settings.public_base_url,
            "code": code,
            "expires_at": formatted_expires_at,
            "support_email": settings.smtp_from_email,
        },
    )

    _send_message(settings, message)


def send_account_data_export_email(
    settings: TrainingHubSettings,
    recipient_email: str,
    requested_at: str,
    archive_name: str,
    archive_bytes: bytes,
    size_bytes: int,
) -> None:
    subject = "ScamScreener Account Data Export"
    formatted_requested_at = _format_utc_timestamp(requested_at)
    message = _build_email_message(
        settings,
        recipient_email=recipient_email,
        subject=subject,
        plain_text=(
            "Your ScamScreener account data export is attached to this email.\n\n"
            f"Requested at (UTC): {formatted_requested_at}\n"
            f"Attachment: {archive_name} ({int(size_bytes)} bytes)\n\n"
            "For security reasons, secret hashes, internal storage paths, and full audit-log detail text are not included."
        ),
        html_template_name="account_data_export_email.html",
        html_context={
            "subject": subject,
            "site_label": _site_label(settings),
            "site_url": settings.public_base_url,
            "requested_at": formatted_requested_at,
            "archive_name": archive_name,
            "size_bytes": str(int(size_bytes)),
            "support_email": settings.smtp_from_email,
        },
        attachments=[
            {
                "content": archive_bytes,
                "maintype": "application",
                "subtype": "zip",
                "filename": archive_name,
            }
        ],
    )

    _send_message(settings, message)

