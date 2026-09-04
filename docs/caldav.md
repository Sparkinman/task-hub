# CalDAV — Nextcloud, Fastmail, Baïkal and anything else

CalDAV is the open standard for calendars and to-do lists. This connector talks
to **any** server that speaks it, which is a long list: Nextcloud, Fastmail,
Baïkal, Radicale, SOGo, Synology Calendar, mailbox.org, Posteo, Zoho, Kolab, and
a great many mail providers you would not think of as calendar companies.

It is the same connector Apple uses, pointed at your server instead of iCloud.
Apple gets its own page only because iCloud's Reminders need three paragraphs of
warning that would be meaningless anywhere else.

> **CalDAV is the one transport that loses nothing.** A collection stores real
> iCalendar, so times, timezones, priorities, repeat rules, tags, locations and
> notes all survive the round trip in both directions. Every other service in
> Task Hub drops something — Google Tasks and Microsoft To Do both discard the
> time of day, Todoist flattens timezones — and none of that applies here.

---

## What you need

Three things, and Task Hub works out everything else:

1. **The server address** — of the *server*, not of one calendar.
2. **Your username** on it.
3. **A password**, usually an app password rather than your website login.

### The address

Task Hub asks the server what your account owns and finds the collections
itself, using the discovery rules in RFC 6764. That means the short address is
almost always enough:

| Server | What to enter |
| --- | --- |
| **Nextcloud** | `https://cloud.example.com` |
| **Fastmail** | `https://caldav.fastmail.com` |
| **Baïkal** | `https://dav.example.com` |
| **Synology Calendar** | `https://diskstation.example.com:5001` |
| **mailbox.org** | `https://dav.mailbox.org` |
| **Posteo** | `https://posteo.de:8443` |
| **Zoho Calendar** | `https://calendar.zoho.com` |
| **SOGo** | `https://sogo.example.com/SOGo/dav` |
| **Radicale elsewhere** | `https://radicale.example.com` |

If you leave the `https://` off, Task Hub adds it. **Plain `http://` is
allowed** — deliberately, for a server on your own network — and Task Hub
relaxes its certificate checking only for that case. Anything reached across the
internet should be `https`, because CalDAV sends your password with every single
request.

If the short address is refused, the long one always works. Your provider's help
pages call it the "CalDAV URL" or "principal URL"; on Nextcloud it looks like
`https://cloud.example.com/remote.php/dav/principals/users/yourname/`.

### The password

**Most servers want an app password rather than your normal login.** Some refuse
the account password outright, and some accept it but shouldn't:

- **Nextcloud** — Settings → Security → **Create new app password**. Nextcloud
  shows the username and password together; use both.
- **Fastmail** — Settings → Password & Security → **New app password**, with the
  *Calendars (CalDAV)* permission. A Fastmail account with two-step
  verification will refuse anything else.
- **Zoho** — Settings → Security → **Application-Specific Passwords**.
- **Synology, Baïkal, Radicale, SOGo** — your ordinary account password is
  normal here; these are servers you run yourself.

Task Hub encrypts whatever you give it at rest, with the same key as every other
credential, and never renders it back into the page.

---

## Connecting it

1. **Services → CalDAV → Connect your CalDAV account.**
2. **Friendly name** — optional, but worth it. "Nextcloud" or "Fastmail" is what
   you will see everywhere afterwards.
3. **Server address**, **Username**, **Password** from above.
4. **Connect and test.** Task Hub signs in immediately, so a wrong password is
   reported while you are still looking at the form rather than at the next
   sync.

Connected accounts are named `you at cloud.example.com`, so two servers with the
same username stay distinguishable.

5. **Refresh lists.** Every calendar and task list on the account appears.
6. Map each one to a Task Hub collection, and choose read, write-back, or both —
   the same mapping table as every other service.

## Whether you get task lists

**That depends on the server, not on Task Hub.** A CalDAV collection declares
which components it accepts, and Task Hub reads that declaration rather than
guessing from the name — a calendar-only collection would reject a to-do
written into it, and a list of reminders shown among the calendars would be
worse than not showing it.

- **Nextcloud, Baïkal, Radicale, SOGo, Synology** — support `VTODO`, so task
  lists appear.
- **Some mail-provider calendars** are events-only. You will see calendars here
  and no task lists, and that is the server saying no, not a fault.

On Nextcloud, task lists are the same objects the **Tasks** app shows; a
calendar and its to-do list are one collection underneath.

---

## Adding a whole account, not one calendar

Task Hub asks for the address of the *server* because it then discovers
everything on the account. There is no "add this one calendar" form, and that is
on purpose: a calendar added by its own URL would silently stop syncing the day
you renamed it, and a new calendar would never appear.

---

## Common problems

**"The server rejected that username and password."** In order of likelihood: an
app password is required and you used your website login; the username should be
the full email address rather than the short name (or the other way round); or
two-factor authentication is on, which makes an app password mandatory.

**"Could not reach the CalDAV server."** The address is wrong, the server is not
reachable from wherever Task Hub is running, or the certificate is not valid. A
self-signed certificate on your own network will be refused over `https` —
either install a real certificate or use `http://` on the local network, where
Task Hub allows it deliberately.

**Everything connects but no lists appear.** Press **Refresh lists**. If it is
still empty, the account genuinely owns nothing yet — make a calendar in the
server's own web interface first.

**Some collections are missing.** Task Hub lists what the account's principal
owns. Calendars *shared with* you by somebody else are often not included, and
some servers put them somewhere discovery does not reach.

## Disconnecting

**Disconnect** deletes the saved address, username and password. Nothing on the
server is altered, and you are asked separately whether to remove the items
Task Hub imported from it.

---

## See also

- [Apple](apple.md) — iCloud, which is this connector with iCloud's own quirks
- [Third-party apps](third-party-apps.md) — connecting apps *to* Task Hub's own
  CalDAV server, which is the other direction entirely
- [Nextcloud's CalDAV documentation](https://docs.nextcloud.com/server/latest/user_manual/en/groupware/sync_ios.html)
- [Fastmail's CalDAV documentation](https://www.fastmail.help/hc/en-us/articles/1500000278342)
