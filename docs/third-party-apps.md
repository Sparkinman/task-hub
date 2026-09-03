# Third-party apps — what to choose and how to set it up

Task Hub runs a real CalDAV server, so your own apps can connect to it directly
rather than going through Google or Apple. This guide says **which app to choose
on each platform**, and gives the exact settings for each one.

---

## Why connect an app directly

Every other guide in this collection connects Task Hub to a *service*. This one
connects an *app on your device* to Task Hub itself.

That is usually the better route when you have a choice:

- **Nothing is lost.** CalDAV carries the due time, the timezone, the priority,
  the repeat rule and the notes. Google Tasks and Microsoft To Do all drop the
  time of day; a CalDAV app does not.
- **It is immediate.** A CalDAV client pushes the moment you tick something.
  Going through a service means waiting for the next sync.
- **Nothing passes through anyone else's servers.**

Connect to a *service* when you want the tasks to genuinely live there — shared
with family in Google, reachable by Siri, on a work Outlook calendar. Connect an
*app* when you just want to see and edit your tasks on a device.

---

## The three things every app asks for

Open **Radicale** in Task Hub's sidebar. It shows all three.

| | |
|---|---|
| **Server address** | `http://192.168.1.42:8080/radicale/yourname/` |
| **Username** | your **CalDAV** username |
| **Password** | your **CalDAV** password |

Three things to get right:

1. **The trailing slash matters.** Some clients fail without it.
2. **Use the CalDAV username and password, not your Task Hub web login.** They
   are deliberately different — the CalDAV password gets typed into every device
   you own, so you can change it without touching the login that guards your
   connected accounts.
3. **`localhost` only works on the machine running Docker.** On your phone,
   `localhost` means your phone. Use the server's network address, and put that
   same address into **Settings → Public address** so Task Hub shows it to you
   correctly.

> **Outside your home network, use HTTPS.** CalDAV signs in with HTTP Basic
> authentication, which is not encryption — over plain `http` your password
> crosses the internet readable on every single sync. Inside your own home
> network that is an acceptable risk; across the internet it is not. Set up the
> Cloudflare tunnel or Tailscale from `getting-started.md` first, then use the
> `https://` address everywhere below.

---

# What to choose

| Device | Calendar | Tasks | Notes |
|---|---|---|---|
| **iPhone / iPad** | built-in Calendar | built-in Reminders | Nothing to install |
| **Mac** | built-in Calendar | built-in Reminders | Nothing to install |
| **Android** | **DAVx⁵** + your calendar app | **DAVx⁵** + **Tasks.org** | DAVx⁵ is the sync layer, not an app you look at |
| **Windows** | **Thunderbird** | Thunderbird | Outlook cannot do CalDAV |
| **Linux** | Thunderbird, or GNOME Calendar | Thunderbird, or GNOME To Do | |
| **Any browser** | Task Hub's own pages | Task Hub's own pages | Nothing to set up |
| **E-ink / older devices** | depends — see the last section | | |

The two names worth knowing: **DAVx⁵** is the only serious CalDAV client for
Android, and **Thunderbird** is the only good free desktop one. Both are open
source and long-established.

---

# iPhone and iPad

Nothing to install. Apple's own apps speak CalDAV.

1. **Settings** → **Apps** → **Calendar** → **Calendar Accounts** → **Add
   Account** → **Other**.

   > On iOS 16 and earlier: **Settings** → **Calendar** → **Accounts** → **Add
   > Account** → **Other**.

2. Choose **Add CalDAV Account**, under the *Calendars* heading.

   > Not **Add Subscribed Calendar** — that is read-only, and it is the mistake
   > people make here. If your tasks show up but you cannot tick them off, this
   > is why.

3. Fill in:
   - **Server**: your address without the `http://`, e.g.
     `192.168.1.42:8080/radicale/yourname/`
   - **User Name**: your CalDAV username
   - **Password**: your CalDAV password
   - **Description**: `Task Hub`
4. **Next**, then **Save**.
5. On the account's settings screen, make sure both **Calendars** and
   **Reminders** are switched on. Reminders is often off by default, and it is
   the half that carries your tasks.

Your calendars appear in Calendar and your task lists in Reminders. Siri works
with them like any other list.

**If it will not save:** iOS is strict about certificates. Over plain `http` on
your home network it works; over `https` with a self-signed certificate it will
refuse. A Cloudflare tunnel gives you a real certificate and avoids this.

---

# Mac

**Calendars:**

1. **Calendar** → **Settings** → **Accounts** → **+** → **Other CalDAV
   Account**.
2. **Account Type**: **Manual**.

   > Automatic asks for an email address and tries to guess the server. It will
   > not guess yours. Choose Manual.

3. **User Name**, **Password**, and **Server Address** — the full address
   including `http://` and the trailing slash.
4. **Sign In**.

**Reminders:** the same account carries them. **Reminders** → **Settings** →
**Accounts**, and confirm Task Hub is enabled.

---

# Android

Android has no built-in CalDAV support at all, so you need **DAVx⁵** — it is the
sync engine that makes your normal calendar and task apps talk to Task Hub. You
open it once and then essentially never again.

## Step 1 — Install

- **F-Droid** (`f-droid.org`) — free.
- **Google Play** — a few euro, which supports the developer. Identical
  software.

Also install a task app, because Android has no built-in one:

- **Tasks.org** — the best choice, and the one to pick. Free on F-Droid, and
  designed for exactly this.
- **OpenTasks** — older, simpler, still works.

## Step 2 — Add the account

1. Open DAVx⁵ and tap **+**.
2. Choose **Login with URL and user name**.

   > Not "Login with email address" — that tries to discover a server from your
   > email domain, which only works for hosted providers.

3. **Base URL**: your full address, e.g.
   `http://192.168.1.42:8080/radicale/yourname/`
4. **User name** and **Password**: your CalDAV credentials.
5. **Login**, then **Create account**.
6. Choose **Groups are per-contact categories** if asked — it makes no
   difference here.

## Step 3 — Choose what syncs

7. On the account screen you will see **CALDAV** with your calendars and task
   lists.
8. Tick each one you want.
9. Tap the refresh icon.

Calendars now appear in Google Calendar or whichever calendar app you use, and
your tasks appear in Tasks.org.

## Android battery settings — the one thing that will bite you

Android aggressively suspends background apps, and DAVx⁵ syncing in the
background is exactly what gets suspended. If sync only happens when you open
DAVx⁵:

- **Settings** → **Apps** → **DAVx⁵** → **Battery** → **Unrestricted**.
- On Samsung, also remove DAVx⁵ from **Sleeping apps** in **Device care**.
- On Xiaomi, OnePlus and Huawei, enable **Autostart** for it.

DAVx⁵ has a warning icon on its main screen that detects most of these and links
straight to the right settings page.

---

# Thunderbird — Windows, Mac and Linux

Free, and the most capable desktop option. Version 115 or later handles tasks
properly.

**Calendars:**

1. **Calendar** tab → right-click in the calendar list → **New Calendar**.
2. **On the Network** → **Next**.
3. **Username** and **Location**: your CalDAV username, and the full address.
4. **Find Calendars**. Thunderbird lists everything it finds — including your
   task lists, which appear as calendars.
5. Tick what you want, give them colours, **Subscribe**.

**Tasks** appear in Thunderbird's **Tasks** tab automatically once subscribed.
Nothing extra to set up.

**If "Find Calendars" finds nothing:** the address is wrong or the trailing
slash is missing. Try the full path to one collection —
`http://.../radicale/yourname/collection-id/` — which the Radicale tab in Task
Hub shows for each collection.

---

# Linux desktop

**GNOME.** **Settings** → **Online Accounts** → **Nextcloud**. Despite the name,
this is a plain CalDAV client and works with Task Hub — enter the server address
and your CalDAV credentials, then switch off Files and Contacts and leave
Calendar and Tasks on. GNOME Calendar and GNOME To Do pick them up.

**KDE.** **System Settings** → **Personal Information** → **Add** → **DAV
groupware resource**. Choose the manual configuration and enter the address.
KOrganizer then shows both calendars and tasks.

Thunderbird works on Linux too, and is more predictable than either.

---

# Outlook — the honest answer

**Microsoft Outlook cannot connect to a CalDAV server.** Not the desktop
version, not the web version, not the new Outlook for Windows. Microsoft removed
what limited support existed and has no plans to bring it back. Paid add-ins
exist; they are unreliable and none can be recommended.

**Do this instead:** connect Task Hub to Microsoft using `microsoft.md`. Your
tasks and calendars then reach Outlook through Microsoft's own servers, which
Outlook is perfectly happy with. The sync is a few minutes slower and Microsoft
To Do drops the time of day, but it works and it needs no add-in.

This is the one case where connecting to the *service* rather than the *app* is
the right answer.

---

# E-ink tablets, older devices and everything else

Devices such as reMarkable, Boox, Kobo and older tablets vary enormously. Three
questions, in order:

**1. Can it run Android apps?** Boox tablets and some others can. Install DAVx⁵
and Tasks.org and follow the Android section. This is the best outcome.

**2. Does it have a real web browser?** Then open Task Hub's own pages —
`/tasks` and `/calendar`. They are plain HTML, work without JavaScript for
reading, and are legible on a low-refresh screen. Nothing to install.

**3. Neither?** Some devices can subscribe to a read-only calendar feed. That
gives you a view of your tasks and nothing more, which for a reading device is
often all you want.

**Whatever the device, it must be able to reach Task Hub.** On your home network
that is automatic. From outside, Tailscale only works if the device can run
Tailscale — many of these cannot, and for those a **Cloudflare tunnel is the
only option**, because its address is an ordinary web address that anything with
a browser can reach. `getting-started.md` covers both.

---

# Choosing between an app and a service

The same tasks can reach a device two ways, and connecting **both** for the same
list is the one thing to avoid — two routes to the same place produce
duplicates and edits that fight each other.

Pick one per list:

- **Straight to Task Hub** (this guide) — nothing lost, instant, private.
  Choose this by default.
- **Through a service** — when the tasks must genuinely live there: shared with
  family in Google, on a work Outlook calendar, reachable by Siri or by a
  colleague.

A concrete example. If your iPhone talks directly to Task Hub, do **not** also
connect Task Hub to Apple for those same lists — your reminders would arrive
twice by two different routes. Either point the phone at Task Hub, or connect
Apple and let iCloud feed the phone. Both are fine; both at once is not.

---

# Troubleshooting

### "Cannot connect" or "Server not found"

- Are you using the network address rather than `localhost`? On a phone,
  `localhost` is the phone.
- Is the trailing `/` there?
- Is the device on the same network — or, if not, is the tunnel or VPN up?
- Open the same address in the device's browser. If it asks for a username and
  password, the server is reachable and the problem is in the app's settings.

### "Authentication failed"

You are almost certainly using your Task Hub **web** login. The CalDAV username
and password are different — the Radicale tab shows the username, and you chose
the password during setup.

### It connects, but nothing appears

The account has no collections yet, or none are ticked. In Task Hub, open
**Radicale** and confirm at least one collection exists. In the app, check what
is selected — DAVx⁵ and Thunderbird both require you to tick each list
explicitly.

### Calendars appear but tasks do not

- **iPhone:** Reminders is switched off for the account. Settings → the account
  → turn Reminders on.
- **Android:** no task app installed. Install Tasks.org.
- **Thunderbird:** look in the **Tasks** tab, not the calendar grid.

### Edits do not come back

Something is subscribed read-only. On iOS this means "Add Subscribed Calendar"
was used instead of "Add CalDAV Account" — remove it and add it again properly.

### Duplicates

The same list is connected twice by two routes — usually directly *and* through
a service. Disconnect one of them. Existing duplicates need deleting by hand;
they will not come back.

### Changes take a long time to appear

Each app has its own interval, independent of Task Hub's:

- **DAVx⁵**: account → sync interval. Fifteen minutes is the practical floor.
- **iOS**: Settings → Calendar → Accounts → Fetch New Data. Push is not
  available for CalDAV; choose Every 15 Minutes.
- **Thunderbird**: right-click the calendar → Properties → refresh interval.

Pulling down to refresh always syncs immediately.
