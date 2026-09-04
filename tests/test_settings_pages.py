"""The Settings page and the onboarding email step must actually render.

Both were assembled by hand from a view that builds a context dictionary and a
template that reads it, and that pairing has broken twice on this project
already -- each time by a template variable added before the view supplied it,
and each time reaching somebody as "Internal Server Error" with nothing else to
go on. The pages are behind a login, so a quick look in a browser is not the
casual check it sounds like.

So these render the real templates through the real context builders. They
assert almost nothing about the wording; what they prove is that every name the
markup reads is a name the view provides.
"""

from __future__ import annotations

import sys

from starlette.requests import Request

from app.db import settings_store
from app.db.session import init_db, session_scope
from app.web import deps, settings_view, setup

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


def fake_request(path: str) -> Request:
    """The least a Starlette request can be and still render a page."""
    return Request({
        "type": "http",
        "method": "GET",
        "path": path,
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "headers": [(b"host", b"localhost:8080")],
        "server": ("localhost", 8080),
        "client": ("127.0.0.1", 1234),
        "session": {},
        "app": None,
    })


def render(template: str, session, **context) -> str:
    """Render one template exactly as a route would, and return the HTML."""
    response = deps.templates.TemplateResponse(
        fake_request("/settings"),
        template,
        deps.build_template_context(fake_request("/settings"), session, **context),
    )
    return response.body.decode()


init_db()

print("\nThe Settings page renders before anything is configured")
with session_scope() as session:
    try:
        response = settings_view.index(fake_request("/settings"), db=session)
        html = response.body.decode()
        check("it renders at all", len(html) > 1000, f"{len(html)} bytes")
        check("the Email card is on it", "Send a test message" in html)
        check("the day picker is on it", 'name="digest_days"' in html)
        check("no test result is claimed before one is run",
              "Last tested" not in html)
    except Exception as exc:  # noqa: BLE001
        check("it renders at all", False, repr(exc))

print("\nAnd once email is set up and tested")
with session_scope() as session:
    settings_store.set_value(session, settings_store.SMTP_HOST, "smtp.example.com")
    settings_store.set_value(session, settings_store.SMTP_FROM, "hub@example.com")
    settings_store.set_value(session, settings_store.DIGEST_TO, "me@example.com")
    settings_store.set_value(session, settings_store.DIGEST_DAYS, "mon,wed,fri")
    settings_store.set_bool(session, settings_store.DIGEST_ENABLED, True)
    session.commit()
    settings_view._record_mail_test(
        session, ok=False, to="me@example.com", detail="The mail server said no."
    )

    try:
        html = settings_view.index(fake_request("/settings"), db=session).body.decode()
        check("a failed test is reported on the page", "Failed" in html)
        check("with the mail server's own words",
              "The mail server said no." in html)
        check("the chosen days are ticked",
              html.count('name="digest_days"') == 7,
              html.count('name="digest_days"'))
        check("and described in words", "Monday, Wednesday and Friday" in html)
    except Exception as exc:  # noqa: BLE001
        check("a failed test is reported on the page", False, repr(exc))

    settings_view._record_mail_test(session, ok=True, to="me@example.com", detail="")
    html = settings_view.index(fake_request("/settings"), db=session).body.decode()
    check("a passing test says so", "Working" in html)

print("\nThe onboarding email step renders, and offers a way past it")
with session_scope() as session:
    try:
        html = render(
            "onboarding/email.html", session,
            steps=setup.STEPS, step=setup.STEPS[-1],
            step_index=len(setup.STEPS) - 1, step_number=len(setup.STEPS),
            step_total=len(setup.STEPS),
            **setup._email_context(session),
        )
        check("it renders at all", len(html) > 1000, f"{len(html)} bytes")
        check("skipping is offered", 'name="skip"' in html)
        check("the mail server fields are there", 'name="smtp_host"' in html)
        check("so is the summary address", 'name="digest_to"' in html)
        check("and the guide is linked", "/docs/email" in html)
    except Exception as exc:  # noqa: BLE001
        check("it renders at all", False, repr(exc))

print("\nThe wizard ends on the email step, which is the skippable one")
check("email is last", setup.STEPS[-1].slug == "email")
check("sync comes before it", setup.STEPS[-2].slug == "sync")

print("\nA pasted app password is cleaned up before it is stored")
# Google, Yahoo and Apple all *display* an app password as four groups of four
# so it can be read off a screen; the spaces are presentation. Stored verbatim,
# the mail server rejects it as a wrong password and says nothing about
# whitespace, which is a failure nobody can find by looking.
from app.services.mail_providers import clean_password  # noqa: E402

check("the four-group display form is joined up",
      clean_password("abcd efgh ijkl mnop") == "abcdefghijklmnop",
      clean_password("abcd efgh ijkl mnop"))
check("a trailing newline from a copy goes",
      clean_password(" abcd efgh ijkl mnop\n") == "abcdefghijklmnop")
check("dashes are treated the same way",
      clean_password("abcd-efgh-ijkl-mnop") == "abcdefghijklmnop")
check("an already-joined one is untouched",
      clean_password("abcdefghijklmnop") == "abcdefghijklmnop")
# The line that must not be crossed: somebody's real passphrase.
check("a passphrase keeps its spaces",
      clean_password("correct horse battery staple") == "correct horse battery staple")
check("but still loses surrounding space",
      clean_password("  swordfish  ") == "swordfish")
check("nothing stays nothing", clean_password("") == "")

print("\nDays are described the way a person would say them")
check("all seven is 'every day'",
      settings_view._days_phrase(list(settings_store.DIGEST_DAY_CODES)) == "every day")
check("the working week has a name",
      settings_view._days_phrase(["mon", "tue", "wed", "thu", "fri"]) == "Monday to Friday")
check("one day reads as a habit",
      settings_view._days_phrase(["mon"]) == "every Monday")
check("a few are listed",
      settings_view._days_phrase(["fri", "mon"]) == "Monday and Friday",
      settings_view._days_phrase(["fri", "mon"]))

print()
if _failures:
    print(f"{len(_failures)} FAILURE(S): {', '.join(_failures)}")
    sys.exit(1)
print("All settings page tests passed.")
