"""The mail servers people actually use, and the names they mistype them as.

Typing the server name by hand is where setting up email goes wrong, and the
failure it produces is unhelpful in a specific way: a hostname that does not
exist fails as a *connection* error, which reads as a network problem or a
firewall rather than as one wrong word. That happened here with
``smtp.google.com`` -- a name that looks exactly right and is not Gmail's.

So the interface offers a list to pick from, and anything typed by hand is
checked against the known wrong names before it is saved.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MailProvider:
    key: str
    name: str
    host: str
    port: int
    security: str
    #: What to sign in as, and where to get a password. Shown under the picker
    #: once a provider is chosen, because "which password?" is the next question
    #: and the answer is different for nearly every one of them.
    username_hint: str


PROVIDERS: tuple[MailProvider, ...] = (
    MailProvider(
        "gmail", "Gmail / Google Workspace", "smtp.gmail.com", 587, "starttls",
        "Sign in with your full Gmail address. Gmail always refuses your normal "
        "password here — make an app password at "
        "myaccount.google.com/apppasswords (two-factor authentication has to be "
        "on first, or the page will not appear).",
    ),
    MailProvider(
        "outlook", "Outlook.com / Microsoft 365", "smtp-mail.outlook.com", 587,
        "starttls",
        "Sign in with your full email address. A Microsoft account with "
        "two-step verification needs an app password from "
        "account.live.com/proofs/AppPassword.",
    ),
    MailProvider(
        "icloud", "iCloud Mail", "smtp.mail.me.com", 587, "starttls",
        "Sign in with your Apple ID. Apple requires an app-specific password, "
        "made at account.apple.com under Sign-In and Security.",
    ),
    MailProvider(
        "fastmail", "Fastmail", "smtp.fastmail.com", 465, "ssl",
        "Sign in with your full Fastmail address and an app password created "
        "under Password & Security, given the SMTP permission.",
    ),
    MailProvider(
        "yahoo", "Yahoo Mail", "smtp.mail.yahoo.com", 465, "ssl",
        "Sign in with your full Yahoo address and an app password generated "
        "under Account Security.",
    ),
    MailProvider(
        "zoho", "Zoho Mail", "smtp.zoho.com", 587, "starttls",
        "Sign in with your full Zoho address. An account with two-factor "
        "authentication needs an application-specific password.",
    ),
    MailProvider(
        "protonmail", "Proton Mail (Mail Bridge)", "127.0.0.1", 1025, "starttls",
        "Proton does not offer SMTP directly: it needs Proton Mail Bridge "
        "running, which listens on your own machine. The address and port come "
        "from the Bridge's own settings window.",
    ),
    MailProvider(
        "mailbox", "mailbox.org", "smtp.mailbox.org", 465, "ssl",
        "Sign in with your full mailbox.org address and your account password.",
    ),
    MailProvider(
        "posteo", "Posteo", "posteo.de", 465, "ssl",
        "Sign in with your full Posteo address and your account password.",
    ),
)

PROVIDERS_BY_HOST = {provider.host: provider for provider in PROVIDERS}

#: Server names that do not exist but look exactly right. Each of these is a
#: name somebody reasonably reaches for -- the company's own domain with "smtp"
#: in front -- and each fails as an unreachable host rather than as a mistake.
KNOWN_MISTAKES: dict[str, str] = {
    "smtp.google.com": "smtp.gmail.com",
    "smtp.googlemail.com": "smtp.gmail.com",
    "gmail.com": "smtp.gmail.com",
    "smtp.microsoft.com": "smtp-mail.outlook.com",
    "smtp.outlook.com": "smtp-mail.outlook.com",
    "smtp.hotmail.com": "smtp-mail.outlook.com",
    "outlook.com": "smtp-mail.outlook.com",
    "smtp.office365.com": "smtp-mail.outlook.com",
    "smtp.apple.com": "smtp.mail.me.com",
    "smtp.icloud.com": "smtp.mail.me.com",
    "icloud.com": "smtp.mail.me.com",
    "smtp.yahoo.com": "smtp.mail.yahoo.com",
    "yahoo.com": "smtp.mail.yahoo.com",
    "smtp.fastmail.fm": "smtp.fastmail.com",
    "fastmail.com": "smtp.fastmail.com",
}


#: Domains that are one keystroke, or one wrong assumption, away from a real
#: mailbox. ``google.com`` is the interesting one: it is a perfectly real domain
#: that simply has no mailbox for you, so the message leaves, is accepted, and
#: bounces minutes later with "Address not found" -- long after the page has
#: said the test succeeded, because SMTP cannot know at send time.
LOOKALIKE_DOMAINS: dict[str, str] = {
    "google.com": "gmail.com",
    "googlemail.co": "gmail.com",
    "gmial.com": "gmail.com",
    "gmai.com": "gmail.com",
    "gmail.co": "gmail.com",
    "hotmial.com": "hotmail.com",
    "hotmail.co": "hotmail.com",
    "outlook.co": "outlook.com",
    "outlok.com": "outlook.com",
    "icloud.co": "icloud.com",
    "iclould.com": "icloud.com",
    "yaho.com": "yahoo.com",
    "yahou.com": "yahoo.com",
    "fastmail.co": "fastmail.com",
}


def suggest_address(address: str) -> str:
    """A warning about a recipient that is probably a typo, or "".

    Deliberately a warning and not a correction, unlike the server name. A
    server name has one thing it can mean; an email address does not, and
    quietly rewriting where somebody's tasks are sent is a far worse fault than
    the typo it would fix.
    """
    _, _, domain = (address or "").strip().lower().partition("@")
    better = LOOKALIKE_DOMAINS.get(domain)
    if not better:
        return ""
    return (
        f"Check that address: mail to @{domain} will not reach you if you meant "
        f"@{better}. It is saved as you typed it."
    )


def correct_host(host: str) -> tuple[str, str]:
    """Fix a server name that is a known mistake, and say what was done.

    Returns the host to save and a sentence to show, empty when nothing was
    changed. Corrected rather than merely refused: every name in the table has
    exactly one thing the person meant, and making them retype it teaches them
    nothing they did not already believe.
    """
    cleaned = (host or "").strip().lower().rstrip("/")
    for prefix in ("https://", "http://", "smtp://"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
    fixed = KNOWN_MISTAKES.get(cleaned)
    if not fixed:
        return (host or "").strip(), ""
    return fixed, (
        f"{cleaned} is not a mail server that exists — the one you want is "
        f"{fixed}, and that is what has been saved."
    )
