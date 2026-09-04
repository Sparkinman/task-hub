"""The Services tab: one page per service, ten account slots each.

The catalogue below is the single place that describes what each service is,
what it can and cannot represent, and which of its quirks the user needs warning
about. The Overview cards, the per-service pages and the setup guides all read
from here, so a correction only has to be made once.

The ``caveats`` are not padding. Each one is a limitation confirmed against the
service's current API, and each is something that would otherwise look like a
bug in Task Hub when it is in fact the service refusing to store the data.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Account, AccountStatus, ServiceKind
from app.crypto import decrypt_json
from app.db.session import get_db
from app.web import deps
from app.web.disconnect import disconnect_accounts, wants_cleanup

router = APIRouter(prefix="/services")

#: Ten independent accounts per service, as specified. Slots are numbered rather
#: than free-form so an account keeps a stable identity in the UI even before
#: the service has told us which email address it belongs to.
MAX_SLOTS = 10


@dataclass(frozen=True)
class ServiceDefinition:
    key: str
    name: str
    #: Badge colour used in the task viewer and on the overview cards.
    colour: str
    supports_tasks: bool
    supports_calendar: bool
    auth_kind: str
    summary: str
    #: Which build phase makes this connector live. Anything above the phase
    #: currently shipped renders as "not yet available" instead of a dead form.
    phase: int
    docs_slug: str
    caveats: list[str] = field(default_factory=list)
    unofficial: bool = False
    #: Kept out of the services list. The connector, its page and its guide all
    #: still work and can be reached directly, so it can be tried deliberately.
    hidden: bool = False
    #: Written, but never run against a real account. Says so on the page,
    #: loudly, whether or not the service is listed -- the two were one flag
    #: until making a connector visible silently removed its only warning, which
    #: is the opposite of what the flag was for. Clear it on the day the
    #: connector completes a sync against a live account, and not before.
    untested: bool = False


#: Why a service reports no lists of a given kind, when the reason is known and
#: is not something the user can put right. Shown under the empty section, so
#: that "none found" does not read as a fault waiting to be fixed.
EMPTY_LIST_NOTES: dict[str, dict[str, str]] = {
    ServiceKind.APPLE.value: {
        "tasks": (
            "This account's Reminders were upgraded in Apple's own app at some "
            "point, so the reminders you already had moved to a store CalDAV "
            "cannot reach and no existing reminder list will ever appear here. "
            "That is Apple's decision and it cannot be undone. Calendars are "
            "unaffected, and so is syncing itself — Task Hub can create an "
            "iCloud task list and keep it in step perfectly well. What an "
            "upgraded account will not do is show that list in Apple's own "
            "Reminders app, which is usually the reason for wanting it."
        ),
        # Getting tasks onto an iPhone does not involve this page at all, and
        # nothing previously said so. Somebody wanting their tasks in Reminders
        # comes to the Apple page, finds only calendars, and reasonably concludes
        # it cannot be done -- when in fact it works today by a route that is not
        # mentioned anywhere near here.
        "tasks_alternative": (
            "You can still see your tasks in Apple's Reminders app, and this "
            "page is not where you set that up. Add Task Hub to the iPhone "
            "directly as a CalDAV account, then on the Sync page choose which "
            "services feed each collection. The lists appear in Reminders under "
            "a Task Hub account rather than under iCloud — same app, same "
            "ticking off, and no upgrade problem. Only Siri, the Apple Watch "
            "and family sharing need the lists to be iCloud's. "
            "Note that a device signs in to Task Hub as an account rather than "
            "as a list, so it sees every collection there is — an iPhone or "
            "iPad shows all of them in Reminders and Calendar, and there is no "
            "per-device selection to offer. To keep one off your devices, "
            "delete it under Collections."
        ),
    },
}


SERVICE_CATALOGUE: tuple[ServiceDefinition, ...] = (
    ServiceDefinition(
        key=ServiceKind.GOOGLE.value,
        name="Google",
        colour="green",
        supports_tasks=True,
        supports_calendar=True,
        auth_kind="OAuth 2.0 (Google Cloud Console)",
        summary="Google Tasks and Google Calendar.",
        phase=2,
        docs_slug="google",
        caveats=[
            "Google Tasks cannot store a time of day. It keeps only the date, "
            "and discards the time even when one is sent. Task Hub remembers "
            "the time separately, so editing a task's date in Google will not "
            "destroy a time you set in another service.",
            "Google Tasks has no priority field, no tags and no location.",
            "Your Google Cloud OAuth app must be published to Production "
            "(Google Auth Platform -> Audience -> PUBLISH APP). While it is in "
            "Testing mode Google expires the login every 7 days, and syncing "
            "stops until you reconnect.",
        ],
    ),
    ServiceDefinition(
        key=ServiceKind.TODOIST.value,
        name="Todoist",
        colour="red",
        supports_tasks=True,
        supports_calendar=False,
        auth_kind="OAuth 2.0 or personal API token",
        summary="Todoist projects and tasks.",
        phase=3,
        docs_slug="todoist",
        caveats=[
            "Todoist has no calendar. Only tasks sync.",
            "Todoist priorities run 1-4 (4 is most urgent), the reverse of the "
            "iCalendar scale. Task Hub converts between them automatically.",
        ],
    ),
    ServiceDefinition(
        key=ServiceKind.TICKTICK.value,
        name="TickTick",
        colour="yellow",
        supports_tasks=True,
        supports_calendar=False,
        auth_kind="OAuth 2.0 (TickTick Developer Center)",
        summary="TickTick lists and tasks.",
        phase=3,
        docs_slug="ticktick",
        caveats=[
            "TickTick's public API covers tasks and projects only. Its calendar "
            "is not exposed to third parties at all.",
            "TickTick has no webhooks, so changes are only noticed on the next "
            "scheduled sync rather than immediately.",
            "Completed tasks are awkward to enumerate through the public API, "
            "so completions may take one extra sync pass to be noticed.",
        ],
    ),
    ServiceDefinition(
        key=ServiceKind.OBSIDIAN.value,
        name="Obsidian",
        colour="purple",
        supports_tasks=True,
        supports_calendar=False,
        auth_kind="Obsidian account (Sync subscription required)",
        summary="Tasks written in your Obsidian vault, through Obsidian Sync.",
        phase=6,
        docs_slug="obsidian",
        caveats=[
            "An Obsidian Sync subscription is required. Obsidian Sync has no "
            "public API and its makers have said they do not intend to add one, "
            "so signing in as another device is the only way a server can read "
            "a vault. A vault kept in Dropbox or iCloud cannot be reached.",
            "This connector is read-only, and not merely by convention: "
            "Obsidian's own client is run in mirror-remote mode, which reverts "
            "local changes. Even a bug here could not alter your vault. Ticking "
            "a task off elsewhere therefore does not tick it off in Obsidian.",
            "A plain checkbox is never treated as a task. Vaults are full of "
            "checklists that are not tasks, so a line must carry the vault's "
            "global filter, or a due date, priority or recurrence, to be synced.",
            "Both the Obsidian Tasks plugin and TaskNotes are read, including "
            "Tasks' Dataview field syntax and TaskNotes' renamed properties.",
            "A task written on a line has no time of day and no timezone -- "
            "there is nowhere in the format to put one. Nothing it sends can "
            "clear a time set in another service. A TaskNotes file can hold a "
            "time, and that survives the round trip.",
            "Task Hub appears in your Obsidian sync history as a device named "
            "'Task Hub', and uses a device slot if your plan limits them.",
        ],
    ),
    ServiceDefinition(
        key=ServiceKind.APPLE.value,
        name="Apple",
        # Its own red rather than Todoist's. Two services sharing a badge colour
        # is fine until both appear on the same task, which is exactly what a
        # task synced to both looks like.
        colour="crimson",
        supports_tasks=True,
        supports_calendar=True,
        auth_kind="Apple ID with an app-specific password",
        summary="iCloud Calendar, and Reminders via a dedicated Apple ID.",
        phase=4,
        docs_slug="apple",
        caveats=[
            "Apple Calendar syncs normally over CalDAV with an app-specific "
            "password.",
            "Creating a task list in iCloud over CalDAV does not get round the "
            "upgrade. Task Hub can make one and read and write it perfectly "
            "well, but an upgraded Reminders app does not display it -- tested "
            "against a real account, and the list simply never appeared on the "
            "phone. To have tasks in Apple's own Reminders app you need the "
            "second Apple ID below; to have them on your phone at all, syncing "
            "the phone directly to Task Hub is simpler and has no upgrade "
            "problem.",
            "Reminders needs a second Apple ID used purely as a task store, "
            "added to your devices as a manual CalDAV account. Apple's Reminders "
            "app moves an 'upgraded' primary account's lists somewhere CalDAV "
            "cannot reach, so never sign that Apple ID in as a full iCloud "
            "account and never accept the 'Upgrade' prompt. The setup guide "
            "walks through this.",
            "If no reminder lists appear here at all but your calendars do, "
            "that Apple ID's Reminders have been upgraded. Apple leaves the old "
            "lists behind holding two placeholders — 'Where are my reminders?' "
            "and 'The creator of this list has upgraded these reminders.' — and "
            "Task Hub hides those rather than offer a list that syncs nothing "
            "but those two sentences. Calendars are unaffected, and the guide "
            "covers the two ways to sync tasks anyway.",
            "Apple requires an app-specific password. Your normal Apple ID "
            "password will always be rejected.",
            "Apple speaks CalDAV, so nothing is lost: times, timezones, "
            "priorities, repeat rules and notes all survive the round trip.",
        ],
    ),
    ServiceDefinition(
        key=ServiceKind.CALDAV.value,
        name="CalDAV",
        colour="amber",
        supports_tasks=True,
        supports_calendar=True,
        auth_kind="Server address, username and password",
        summary="Any other CalDAV server: Nextcloud, Fastmail, Baikal, Synology.",
        phase=6,
        docs_slug="caldav",
        caveats=[
            "This is the same connector Apple uses, pointed at your own server "
            "instead of iCloud. CalDAV is the one transport that loses nothing: "
            "times, timezones, priorities, repeat rules, tags and notes all "
            "survive the round trip in both directions.",
            "Give the address of the server, not of one calendar. Task Hub asks "
            "the server what the account owns and finds the collections itself, "
            "so https://cloud.example.com is usually enough.",
            "Most servers want an app password created in their own settings "
            "rather than your website login — Nextcloud, Fastmail and Zoho all "
            "do, and some refuse the account password outright.",
            "Whether tasks appear depends on the server, not on Task Hub. A "
            "collection has to accept VTODO for its to-dos to be visible: "
            "Nextcloud and Baikal do, and a calendar-only server will offer "
            "calendars here and no task lists.",
            "A server reached over plain http is allowed, for one on your own "
            "network. Anything across the internet should be https, because "
            "CalDAV sends the password on every request.",
            "Verified against a live CalDAV server — sign-in, discovery, and a "
            "to-do created, read back and deleted with its due time, priority "
            "and notes intact. That was Radicale rather than Nextcloud or "
            "Fastmail, so the protocol path is proven and those particular "
            "servers are not yet.",
        ],
    ),
    ServiceDefinition(
        key=ServiceKind.MICROSOFT.value,
        name="Microsoft",
        colour="black",
        supports_tasks=True,
        supports_calendar=True,
        auth_kind="OAuth 2.0 (Azure app registration)",
        summary="Microsoft To Do and Outlook Calendar, via Microsoft Graph.",
        phase=4,
        docs_slug="microsoft",
        caveats=[
            "Registering the app needs a Microsoft directory (an Entra ID tenant). Since June 2024 Microsoft refuses to create app registrations outside one, and a personal outlook.com account does not have one -- the portal answers \"the ability to create applications outside of a directory has been deprecated\". A work or school account already has a directory; otherwise a free Azure sign-up creates one. The setup guide lays out the options.",
            "Microsoft To Do cannot store a time of day. Like Google Tasks it "
            "keeps only the date, and quietly discards a time that is sent to "
            "it. Task Hub remembers the time separately, so editing a task's "
            "date in Microsoft will not destroy a time you set elsewhere.",
            "Microsoft To Do has no location, and its repeat rules are not "
            "exposed for editing through the API, so a repeating to-do syncs "
            "as a single item rather than as a wrong repeating one.",
            "Outlook Calendar syncs fully, including times, timezones, "
            "locations and the common repeat patterns.",
            "Works with both personal Microsoft accounts and work or school "
            "accounts, but a work account may need an administrator to approve "
            "the permissions.",
            "When creating the client secret in Azure, copy the Value column "
            "and not the Secret ID. This is the most common setup mistake and "
            "Microsoft's error message does not explain it.",
        ],
    ),
    ServiceDefinition(
        key=ServiceKind.THINGS3.value,
        name="Things 3",
        colour="black",
        supports_tasks=True,
        supports_calendar=False,
        auth_kind="Things Cloud email and password (unofficial)",
        summary="Things 3 to-dos, through the Things Cloud service.",
        phase=5,
        docs_slug="things3",
        unofficial=True,
        caveats=[
            "Cultured Code publishes no API for Things. This connector uses the "
            "community-documented Things Cloud endpoint, which is unofficial and "
            "can stop working without warning if Cultured Code changes it.",
            "Because there is no OAuth, this requires storing your Things Cloud "
            "password. It is encrypted at rest like every other credential.",
            "This connector is read-only. Task Hub imports your Things to-dos "
            "but never writes back, because writing to an undocumented endpoint "
            "risks corrupting the database that holds your real work.",
            "It has been run against a live Things Cloud account and imports "
            "correctly. Four faults only a real account could have shown were "
            "found and fixed doing so, which is exactly why the label was there. "
            "It still checks that it can sign in and read the moment you "
            "connect, so a change at Cultured Code's end surfaces immediately "
            "rather than as silent data loss.",
            "Things schedules a to-do to a day and keeps reminders separately, "
            "so it holds no time of day. Nothing it sends can clear a time set "
            "in another service.",
            "Deleting a to-do in Things does not delete it in Task Hub. Things "
            "reports a stream of changes rather than a complete list, so an "
            "item's absence is never treated as a deletion.",
            "If Things stops responding, the rest of your services keep syncing "
            "normally; only the Things connector is skipped.",
        ],
    ),
)
SERVICES_BY_KEY = {definition.key: definition for definition in SERVICE_CATALOGUE}

#: Phases completed so far. Connectors above this render as "not yet available".
CURRENT_PHASE = 6


@router.get("")
@router.get("/")
def index(request: Request, db: Session = Depends(get_db)):
    accounts = db.execute(select(Account)).scalars().all()
    counts: dict[str, int] = {}
    for account in accounts:
        counts[account.service.value] = counts.get(account.service.value, 0) + 1

    return deps.render(
        request, db, "services_index.html",
        catalogue=[d for d in SERVICE_CATALOGUE
                   if not d.hidden or d.key in counts],
        counts=counts,
        current_phase=CURRENT_PHASE,
    )


@router.get("/{service_key}")
def service_detail(service_key: str, request: Request, db: Session = Depends(get_db)):
    definition = SERVICES_BY_KEY.get(service_key)
    if definition is None:
        deps.flash(request, "Unknown service.", "error")
        return deps.redirect("/services")

    accounts = db.execute(
        select(Account)
        .where(Account.service == ServiceKind(service_key))
        .order_by(Account.slot)
    ).scalars().all()

    used_slots = {account.slot for account in accounts}
    free_slots = [n for n in range(1, MAX_SLOTS + 1) if n not in used_slots]

    if service_key == ServiceKind.OBSIDIAN.value:
        # Obsidian is neither an OAuth service nor a stored password: the
        # account is a sign-in to Obsidian's own client, and a vault has to be
        # picked afterwards, so it gets a page of its own.
        from app.web.obsidian_setup import state as obsidian_state

        return deps.render(
            request, db, "service_obsidian.html",
            definition=definition,
            accounts=accounts,
            obsidian=obsidian_state(db),
            available=definition.phase <= CURRENT_PHASE,
            current_phase=CURRENT_PHASE,
            # The same mapping table as every other service, so a vault's
            # folders are wired to collections exactly the way a Google list is.
            **_mapping_context(db, accounts),
        )

    from app.web.password_setup import service_for as _password_service

    if _password_service(service_key) is not None:
        return _password_detail(request, db, definition, accounts, free_slots)

    if service_key == ServiceKind.GOOGLE.value:
        return _google_detail(request, db, definition, accounts, free_slots)

    from app.web.oauth_setup import service_for as _oauth_service

    oauth_service = _oauth_service(service_key)
    if oauth_service is not None and definition.phase <= CURRENT_PHASE:
        return _oauth_detail(
            request, db, definition, oauth_service, accounts, free_slots
        )

    return deps.render(
        request, db, "service_detail.html",
        definition=definition,
        accounts=accounts,
        free_slots=free_slots,
        max_slots=MAX_SLOTS,
        available=definition.phase <= CURRENT_PHASE,
        current_phase=CURRENT_PHASE,
    )


def _mapping_context(db, accounts) -> dict:
    """Everything the shared mapping table needs, for any service.

    Built once here rather than per service page: the table's behaviour has to
    be identical for Google, Todoist, TickTick and whatever comes next, and it
    only stays identical if there is one place that assembles it.
    """
    from sqlalchemy import select as _select

    from app.db.models import RadicaleCollection as _Collection
    from app.db.models import RemoteList as _RemoteList

    # What disconnecting each account would leave orphaned, so the button can
    # say the number before it is pressed rather than after.
    from app.web.disconnect import cleanup_preview as _preview

    cleanup_by_account = {a.id: _preview(db, [a]) for a in accounts}

    lists_by_account = {}
    for account in accounts:
        lists_by_account[account.id] = (
            db.execute(
                _select(_RemoteList)
                .where(_RemoteList.account_id == account.id)
                .order_by(_RemoteList.kind, _RemoteList.name)
            )
            .scalars()
            .all()
        )

    collections = (
        db.execute(_select(_Collection).order_by(_Collection.display_name))
        .scalars()
        .all()
    )

    from app.db.models import CollectionKind as _Kind
    from app.db.models import ListMapping as _Mapping
    from app.db.models import SyncGroup as _Group
    from app.sync.engine import account_kind_enabled

    # group id -> radicale collection id, so a mapping can be shown against the
    # collection the user actually recognises rather than the internal group.
    group_collection = {
        g.id: g.radicale_collection_id for g in db.execute(_select(_Group)).scalars()
    }

    # remote_list id -> {"read": {collection ids}, "writeout": {list ids}}
    #
    # "read" is what the row's collection column shows. "writeout" is the third
    # column: the lists this row's result is written back out to. Write-back is
    # a property of the collection, so a row's targets are the lists written by
    # whichever collections that row reads into -- resolved here so the template
    # can ask a plain "is this box ticked" question of both columns.
    mapped: dict[int, dict] = {}
    written_by_group: dict[int, set[int]] = {}
    for mapping in db.execute(_select(_Mapping)).scalars():
        collection_id = group_collection.get(mapping.sync_group_id)
        if collection_id is None:
            continue
        entry = mapped.setdefault(
            mapping.remote_list_id,
            {"read": set(), "writeout": set(), "updates_only": False},
        )
        if mapping.read_enabled:
            entry["read"].add(collection_id)
            if mapping.create_from_remote is False:
                entry["updates_only"] = True
        if mapping.write_enabled:
            written_by_group.setdefault(mapping.sync_group_id, set()).add(
                mapping.remote_list_id
            )

    collection_group = {
        collection_id: group_id
        for group_id, collection_id in group_collection.items()
    }
    for entry in mapped.values():
        for collection_id in entry["read"]:
            group_id = collection_group.get(collection_id)
            if group_id is not None:
                entry["writeout"] |= written_by_group.get(group_id, set())

    # Every list that could receive write-back, across every connected service
    # and every account of it -- a collection may push into a Todoist list just
    # as readily as a Google one, so limiting the choice to the account being
    # edited would hide most of the useful targets. Grouped by account and
    # labelled with the service, because "Grocery List" alone is ambiguous the
    # moment two services both have one.
    #
    # The built-in Radicale account is left out on purpose: its lists *are* the
    # collections in the middle column, so offering them here would ask the user
    # to pick the same thing twice under a different name.
    target_groups: list[dict] = []
    for other in db.execute(
        _select(Account).where(Account.enabled.is_(True)).order_by(Account.id)
    ).scalars():
        if other.service == ServiceKind.RADICALE:
            continue
        service = SERVICES_BY_KEY.get(other.service.value)
        service_name = service.name if service else other.service.value.title()
        other_lists = db.execute(
            _select(_RemoteList)
            .where(_RemoteList.account_id == other.id)
            .order_by(_RemoteList.kind, _RemoteList.name)
        ).scalars().all()
        if not other_lists:
            continue
        target_groups.append({
            "account_id": other.id,
            "service": other.service.value,
            "colour": service.colour if service else "#64748b",
            "label": f"{service_name} — {other.label}" if other.label else service_name,
            "lists": other_lists,
        })

    # Everything the two dropdowns on each row need, assembled here because
    # Jinja has no comprehensions and building it inline would be unreadable.
    # Keyed by remote list id; each entry holds the option groups to show and
    # the "<row>:<target>" values that are currently ticked.
    dropdowns: dict[int, dict] = {}

    written_lists: set[int] = set()
    for lists in written_by_group.values():
        written_lists |= lists

    def _dropdown_for(remote_list) -> dict:
        entry = mapped.get(
            remote_list.id, {"read": set(), "writeout": set(), "updates_only": False}
        )

        read_options = [
            {"value": f"{remote_list.id}:{collection.id}",
             "label": collection.display_name, "note": ""}
            for collection in collections
            if collection.kind == remote_list.kind
        ]

        write_groups = []
        for target_group in target_groups:
            options = [
                {"value": f"{remote_list.id}:{target.id}",
                 "label": target.name,
                 "note": "(itself)" if target.id == remote_list.id else ""}
                for target in target_group["lists"]
                if target.kind == remote_list.kind
            ]
            if options:
                write_groups.append({
                    "label": target_group["label"],
                    "colour": target_group["colour"],
                    "options": options,
                })

        # Written to by some collection, but not read into any: a change made
        # there can never come back. Flagged in the row so it is visible before
        # a sync rather than after one fails to do what was expected.
        one_way = (
            remote_list.id in written_lists and not entry["read"]
        )

        return {
            "one_way": one_way,
            "updates_only": entry.get("updates_only", False),
            "read": [{"label": "", "colour": "", "options": read_options}],
            "read_selected": {
                f"{remote_list.id}:{collection_id}" for collection_id in entry["read"]
            },
            "writeout": write_groups,
            "writeout_selected": {
                f"{remote_list.id}:{target_id}" for target_id in entry["writeout"]
            },
        }

    for account_lists in lists_by_account.values():
        for remote_list in account_lists:
            dropdowns[remote_list.id] = _dropdown_for(remote_list)

    kind_enabled = {
        account.id: {
            "tasks": account_kind_enabled(account, _Kind.TASKS),
            "calendar": account_kind_enabled(account, _Kind.CALENDAR),
        }
        for account in accounts
    }

    return {
        "lists_by_account": lists_by_account,
        "collections": collections,
        "mapped": mapped,
        "dropdowns": dropdowns,
        "kind_enabled": kind_enabled,
        "target_groups": target_groups,
        "cleanup": cleanup_by_account,
        # Why a kind has no lists, where the reason is known and is not a fault
        # the user can fix. "No task lists in this account" reads as something
        # to go and correct; for an upgraded Apple ID there is nothing to
        # correct, and saying so saves the search.
        # Taken from the accounts rather than passed in: every account on a
        # service page belongs to that one service, and deriving it here keeps
        # all four callers of this helper unchanged.
        "empty_notes": EMPTY_LIST_NOTES.get(
            accounts[0].service.value if accounts else "", {}
        ),
    }


def _oauth_detail(request, db, definition, service, accounts, free_slots):
    """Todoist and TickTick: register an app, connect accounts, map the lists.

    Deliberately thin. Everything about *what* the mapping table does lives in
    the shared context and the shared macro, so this function only supplies the
    handful of things that are genuinely about this one service -- where its
    developer console lives, and what it will make of our redirect URI.
    """
    from app.web.oauth_setup import (
        client_credentials_for,
        redirect_uri_for,
        redirect_uri_problem,
    )

    client_id, client_secret = client_credentials_for(db, service.kind)
    redirect_uri = redirect_uri_for(request, service)

    return deps.render(
        request, db, "service_oauth.html",
        definition=definition,
        service=service,
        accounts=accounts,
        free_slots=free_slots,
        max_slots=MAX_SLOTS,
        available=True,
        current_phase=CURRENT_PHASE,
        client_id=client_id,
        configured=bool(client_id and client_secret),
        redirect_uri=redirect_uri,
        redirect_problem=redirect_uri_problem(redirect_uri, service),
        **_mapping_context(db, accounts),
    )


def _password_detail(request, db, definition, accounts, free_slots):
    """Apple, CalDAV and Things 3: sign in with a password, then map lists.

    Deliberately thin, exactly like the OAuth page. Everything about the mapping
    table comes from the shared context and the shared macro, so all four
    service pages behave identically and only the sign-in differs.
    """
    from app.web.password_setup import service_for

    return deps.render(
        request, db, "service_password.html",
        definition=definition,
        service=service_for(definition.key),
        accounts=accounts,
        free_slots=free_slots,
        max_slots=MAX_SLOTS,
        available=True,
        current_phase=CURRENT_PHASE,
        # Never the secret itself: only whether one is already saved, so the
        # field can say "leave blank to keep it" without echoing a password.
        has_secret={a.id: bool(decrypt_json(a.credentials).get("password"))
                    for a in accounts},
        # The server address is not a secret and is genuinely useful to see --
        # "which Nextcloud is this?" is answered by the address and by nothing
        # else on the page -- so unlike the password it is echoed back.
        saved_urls={a.id: decrypt_json(a.credentials).get("url", "")
                    for a in accounts},
        saved_usernames={
            a.id: (decrypt_json(a.credentials).get("username")
                   or decrypt_json(a.credentials).get("email") or "")
            for a in accounts
        },
        **_mapping_context(db, accounts),
    )


def _google_detail(request, db, definition, accounts, free_slots):
    """Google gets its own page: OAuth, discovered lists and the redirect URI."""
    from sqlalchemy import select as _select

    from app.db.models import RadicaleCollection as _Collection
    from app.db.models import RemoteList as _RemoteList
    from app.web.google_setup import (
        get_google_client_credentials,
        redirect_uri_for,
        redirect_uri_problem,
    )

    client_id, _secret = get_google_client_credentials(db)
    redirect_uri = redirect_uri_for(request)
    shared = _mapping_context(db, accounts)

    return deps.render(
        request, db, "service_google.html",
        definition=definition,
        accounts=accounts,
        free_slots=free_slots,
        max_slots=MAX_SLOTS,
        available=True,
        current_phase=CURRENT_PHASE,
        client_id=client_id,
        configured=bool(client_id and _secret),
        redirect_uri=redirect_uri,
        redirect_problem=redirect_uri_problem(redirect_uri),
        **shared,
    )


@router.post("/{service_key}/slots/{slot}/label")
def rename_slot(
    service_key: str,
    slot: int,
    request: Request,
    label: str = Form(""),
    db: Session = Depends(get_db),
):
    """Rename an account slot.

    Available before a connector goes live so slots can be planned out in
    advance -- "Work Google", "Personal Google" -- and stay recognisable.
    """
    if service_key not in SERVICES_BY_KEY:
        return deps.redirect("/services")

    account = db.execute(
        select(Account).where(
            Account.service == ServiceKind(service_key), Account.slot == slot
        )
    ).scalar_one_or_none()

    if account is None:
        account = Account(service=ServiceKind(service_key), slot=slot)
        db.add(account)

    account.label = label.strip()[:120]
    db.commit()
    deps.flash(request, "Slot updated.", "success")
    return deps.redirect(f"/services/{service_key}")


@router.post("/{service_key}/slots/{slot}/delete")
def delete_slot(
    service_key: str, slot: int, request: Request,
    remove_items: str = Form(""),
    db: Session = Depends(get_db),
):
    if service_key not in SERVICES_BY_KEY:
        return deps.redirect("/services")

    account = db.execute(
        select(Account).where(
            Account.service == ServiceKind(service_key), Account.slot == slot
        )
    ).scalar_one_or_none()

    if account is not None:
        disconnect_accounts(request, db, [account], wants_cleanup(remove_items))

    return deps.redirect(f"/services/{service_key}")


@router.post("/{service_key}/{slot}/discover")
def discover_lists(
    service_key: str, slot: int, request: Request, db: Session = Depends(get_db)
):
    """Fetch a connected account's lists so they can be mapped to collections.

    Written once, for every service, because it was previously written once for
    *some* services. The URL is generic and only the OAuth module claimed it, so
    a password-authenticated account -- Apple, Things 3 -- reached a handler that
    did not recognise its service key and redirected away without a word. The
    "Refresh lists" button appeared to work, nothing happened, and the account
    could never be mapped to anything because it had no lists on record.

    Google keeps its own handler at a more specific path, registered ahead of
    this one, because discovery there can refresh an access token and that new
    token has to be saved. Everything else wants exactly this.
    """
    if service_key not in SERVICES_BY_KEY:
        return deps.redirect("/services")

    account = db.execute(
        select(Account).where(
            Account.service == ServiceKind(service_key), Account.slot == slot
        )
    ).scalar_one_or_none()
    if account is None:
        deps.flash(request, "That slot is not connected.", "error")
        return deps.redirect(f"/services/{service_key}")

    from app.connectors.base import ConnectorAuthError, ConnectorError
    from app.sync.engine import refresh_remote_lists

    try:
        remote_lists = refresh_remote_lists(db, account)
    except ConnectorAuthError as exc:
        account.status = AccountStatus.NEEDS_AUTH
        account.status_detail = str(exc)
        db.commit()
        deps.flash(request, str(exc), "error")
        return deps.redirect(f"/services/{service_key}")
    except ConnectorError as exc:
        account.status = AccountStatus.ERROR
        account.status_detail = str(exc)
        db.commit()
        deps.flash(request, f"Could not read the lists: {exc}", "error")
        return deps.redirect(f"/services/{service_key}")

    account.status = AccountStatus.CONNECTED
    account.status_detail = None
    db.commit()

    # Said plainly, including when the answer is none: an account that reports
    # no lists at all is a real state -- an upgraded Apple ID has no reminder
    # lists CalDAV can see -- and silence would look like the button failing.
    deps.flash(
        request,
        f"Found {len(remote_lists)} list(s) in {account.label or service_key}."
        if remote_lists
        else f"{account.label or service_key} reported no lists at all.",
        "success" if remote_lists else "error",
    )
    return deps.redirect(f"/services/{service_key}")
