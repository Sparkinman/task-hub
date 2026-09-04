"""Sending email, for the two things Task Hub has to say out loud.

Two features need it and neither is worth a dependency: a daily summary of what
is due, and pushing to-dos into Things, whose only supported remote write is an
email address. Both are one small message at a time, so the standard library's
``smtplib`` is the whole implementation and nothing new goes in the image.

**Task Hub sends mail; it never receives any.** There is no inbox, no listener
and no open port. That matters for what a compromise of these settings could do:
somebody with them could send mail as you, which is bad, and could not read
anything, which would be worse.

**The credentials are a real account password.** Unlike an OAuth token there is
usually nothing to revoke narrowly, so the guidance everywhere in the interface
is to use an application-specific password where the provider offers one --
Gmail and Fastmail both do -- rather than the password that opens the mailbox
itself. It is encrypted at rest with the same key as every other credential.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

logger = logging.getLogger(__name__)

#: How the connection is protected. Implicit TLS on 465 and STARTTLS on 587 are
#: the two every provider offers; "none" exists for a mail server on the same
#: machine and is not offered as a default anywhere.
SECURITY_NONE = "none"
SECURITY_STARTTLS = "starttls"
SECURITY_SSL = "ssl"
SECURITIES = (SECURITY_STARTTLS, SECURITY_SSL, SECURITY_NONE)

#: Long enough for a slow provider, short enough that a wrong host does not hold
#: a scheduled job open for minutes.
TIMEOUT_SECONDS = 30


class MailError(RuntimeError):
    """Sending failed, with a message written for the person who has to fix it."""


@dataclass(frozen=True)
class MailSettings:
    """Everything needed to send one message."""

    host: str
    port: int
    security: str
    username: str
    password: str
    from_address: str
    from_name: str = "Task Hub"

    @property
    def configured(self) -> bool:
        return bool(self.host and self.port and self.from_address)

    def sender(self) -> str:
        name, address = parseaddr(self.from_address)
        return formataddr((self.from_name or name or "Task Hub", address or self.from_address))


def _explain(exc: Exception, settings: MailSettings) -> str:
    """Turn an SMTP failure into something a person can act on.

    Mail servers report the same few problems in a dozen different ways, and the
    raw exception is almost never the sentence somebody needs. These four cover
    essentially every first-time failure.
    """
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return (
            "The mail server rejected that username and password. If you use "
            "Gmail, Yahoo or iCloud, your normal password will always be "
            "refused here — those providers require an app-specific password "
            "generated in your account's security settings."
        )
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "The mail server refused the recipient address."
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return (
            f"The mail server refused {settings.from_address!r} as a sender. "
            "It usually has to be an address that account is allowed to send as."
        )
    if isinstance(exc, (smtplib.SMTPConnectError, OSError)):
        return (
            f"Could not reach {settings.host}:{settings.port}. Check the server "
            "name and port, and that the security setting matches — 465 is "
            "normally SSL and 587 is normally STARTTLS."
        )
    return f"The mail server refused the message: {exc}"


def send(settings: MailSettings, to: str, subject: str, body: str) -> None:
    """Send one plain-text message, or raise MailError explaining why not.

    Plain text on purpose. The two things Task Hub sends are a list of tasks and
    a to-do for Things to parse, and Things reads the body as the note — HTML
    would arrive as markup in somebody's task.
    """
    if not settings.configured:
        raise MailError("Email is not set up yet. Add a mail server under Settings.")
    if not to.strip():
        raise MailError("No recipient address.")

    message = EmailMessage()
    message["From"] = settings.sender()
    message["To"] = to.strip()
    message["Subject"] = subject
    message.set_content(body)

    try:
        if settings.security == SECURITY_SSL:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(settings.host, settings.port,
                                  timeout=TIMEOUT_SECONDS, context=context) as server:
                _authenticate(server, settings)
                server.send_message(message)
        else:
            with smtplib.SMTP(settings.host, settings.port,
                              timeout=TIMEOUT_SECONDS) as server:
                server.ehlo()
                if settings.security == SECURITY_STARTTLS:
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                _authenticate(server, settings)
                server.send_message(message)
    except MailError:
        raise
    except Exception as exc:  # noqa: BLE001 - every failure becomes advice
        logger.warning("Sending mail through %s failed: %s", settings.host, exc)
        raise MailError(_explain(exc, settings)) from exc

    logger.info("Sent %r to %s", subject[:60], to)


def _authenticate(server: smtplib.SMTP, settings: MailSettings) -> None:
    """Sign in, when there is anything to sign in with.

    A server on the same machine often wants no credentials at all, and calling
    login with an empty username fails against those rather than being ignored.
    """
    if settings.username:
        server.login(settings.username, settings.password)
