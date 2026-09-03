# Apple setup — complete walkthrough

Connects **iCloud Calendar** and **Apple Reminders** using CalDAV, which is the
same standard Task Hub's own server speaks. There is no developer account, no
app registration and no OAuth — Apple gives you a password and that is the whole
setup.

**Time needed:** about 5 minutes for calendars.

Reminders need one extra piece of preparation and a warning you should read
before you start.

---

## Read this first — the Reminders problem

Apple has two kinds of Reminders storage.

**The old kind** is CalDAV. Every reminder is a standard VTODO on Apple's
server, and any CalDAV client — Task Hub included — can read and write it.

**The new kind** is a private Apple format. In 2021 Apple began prompting people
to "Upgrade" their Reminders, which unlocked features such as smart lists and
tags. That upgrade moves your reminders out of CalDAV entirely. They keep
working perfectly in Apple's own apps and become **completely invisible to every
other application, permanently.** There is no way to reverse it, and Apple
support confirms this.

So:

> ⚠️ **On the Apple ID you want to sync, never tap "Upgrade" when the Reminders
> app offers it.** If it has already been upgraded, its reminders cannot be
> synced by Task Hub or by anything else. This is Apple's decision, not a
> limitation of this application.

### If your reminders are already upgraded

Use a **second Apple ID** — a free one, made for this purpose — and add it to
your devices as a **manual CalDAV account**. Manually added CalDAV accounts are
explicitly unaffected by the upgrade and stay on the old, readable format. Part
D below covers this.

**Calendars have no such problem.** iCloud Calendar is CalDAV whatever you have
done with Reminders, so if you only want calendars, skip to Part A and ignore
all of this.

---

# Part A — Turn on two-factor authentication

App-specific passwords, which Task Hub needs, only exist on accounts with
two-factor authentication.

1. Go to **https://account.apple.com** and sign in.
2. **Sign-In and Security** → **Two-Factor Authentication**.
3. If it is off, turn it on and follow the prompts.

Most Apple accounts already have this.

---

# Part B — Create an app-specific password

An app-specific password is a password that works for one application and can be
revoked on its own. Task Hub never sees your real Apple password.

4. Still at **https://account.apple.com**, under **Sign-In and Security**, click
   **App-Specific Passwords**.
5. Click **+** (or "Generate an app-specific password").
6. Name it `Task Hub` and click **Create**. Confirm with your Apple password.
7. Apple shows a password in the form `abcd-efgh-ijkl-mnop`.

   > It is displayed **once**. Copy it now. If you lose it, delete it here and
   > generate another — nothing breaks.

**Include the dashes.** Apple's own instructions have said both things over the
years; entered exactly as displayed is what works.

---

# Part C — Connect it in Task Hub

8. In Task Hub: **Services** → **Apple**.
9. Click **Connect** on an empty slot.
10. **Apple ID**: your full address, e.g. `you@icloud.com`.
11. **App-specific password**: the value from Step 7.
12. Leave **Server address** blank unless you know you need something else. Task
    Hub finds your server automatically — Apple assigns accounts to numbered
    servers such as `p42-caldav.icloud.com` and discovers the right one from
    your sign-in.
13. Click **Save**.

Task Hub immediately signs in to check, so a wrong password is reported now
rather than silently failing later.

## Choose what syncs

14. Click **Refresh lists**. Your iCloud calendars and any CalDAV-visible
    reminder lists appear.
15. Tick the Radicale collection each should sync with.
16. Click **Save sync settings for this account**, then **Sync now**.

Task Hub asks each list what it holds and offers calendars only to calendar
collections and reminder lists only to task collections, so the two cannot be
crossed by accident.

**If reminder lists are missing but calendars are there,** that account's
Reminders have been upgraded. Part D is the way round it.

---

# Part D — The second Apple ID method for Reminders

This gives you working reminder sync without touching your existing account.

## Create the account

17. On a Mac or iPhone, or at **https://account.apple.com**, create a **new,
    free Apple ID**. Any email address you can receive mail at will do. Call it
    something obvious like `you+tasks@gmail.com`.
18. Turn on two-factor authentication for it (Part A).
19. Sign in to **https://www.icloud.com** with the new ID and open **Reminders**
    once. This creates the account's default list, which must exist before
    anything can sync to it.

    > **Do not tap "Upgrade"** if it is offered. This is the entire point of the
    > exercise. Answer "Not Now", or simply close the page.

20. Create an app-specific password for this new ID (Part B).
21. Connect it in Task Hub as a second Apple slot (Part C).

## Add it to your devices as a manual CalDAV account

This is the step that keeps it on the old format. Adding it as a normal iCloud
account would defeat the purpose.

**On iPhone or iPad:**

22. **Settings** → **Apps** → **Calendar** → **Calendar Accounts** → **Add
    Account** → **Other**.
23. Choose **Add CalDAV Account** — under the *Calendars* heading. Not "Add
    Subscribed Calendar", and not the iCloud button at the top.
24. Fill in:
    - **Server**: `caldav.icloud.com`
    - **User Name**: the new Apple ID
    - **Password**: its app-specific password
    - **Description**: `Tasks`
25. Tap **Next**, then **Save**.
26. Make sure **Reminders** is switched on for this account.

**On a Mac:**

27. **Calendar** → **Settings** → **Accounts** → **+** → **Other CalDAV
    Account**.
28. **Account Type**: **Manual**. Server address `caldav.icloud.com`, the new
    Apple ID, its app-specific password.
29. In **Reminders** → **Settings** → **Accounts**, confirm the account is
    enabled.

The new account's lists now appear in Reminders alongside your existing ones,
they stay on CalDAV permanently, and Task Hub can read and write them.

> **Which list you add a reminder to matters.** Reminders added to your original
> account are invisible to Task Hub; reminders added to the new account sync
> everywhere. In the Reminders app, put the new account's list in your
> favourites so it is the easy one to reach.

---

## An alternative: skip Apple's servers entirely

Task Hub runs its own CalDAV server, and your iPhone can talk to it directly. So
instead of syncing Task Hub to iCloud and iCloud to your phone, you can point
your phone at Task Hub — the same three fields as Step 24, with the address from
Task Hub's **Radicale** tab.

That is fewer moving parts, no app-specific password, no upgrade problem, and
your tasks stop passing through Apple's servers on the way to your own. The
trade-off is that Siri and the iCloud website will not see those lists.

The Apple connector remains the right answer when you want your reminders to
genuinely live in iCloud — shared with family, on an Apple Watch, or reachable
by Siri.

---

# What Apple can store

CalDAV is the standard Task Hub's own model was built around, so **Apple loses
nothing**:

| | |
|---|---|
| Due date and **time of day** | Yes |
| Timezones | Yes |
| Notes, priority, tags | Yes |
| Start dates, repeating rules | Yes |
| Location, all-day events | Yes |

This makes Apple, alongside Task Hub's own server, one of the two places a task
can be stored with nothing shaved off. Google Tasks and Microsoft To Do both
drop the time of day; Apple keeps it.

---

# Troubleshooting

### "Authentication failed" with the right password

- You used your **real Apple password** rather than the app-specific one. They
  are not interchangeable.
- The app-specific password was typed rather than pasted, and a character is
  wrong. Delete it at account.apple.com and generate a new one.
- Two-factor authentication is off on the account. See Part A.

### No lists at all

Sign in to **https://www.icloud.com** with the same Apple ID and confirm
Calendar and Reminders both open and have at least one list. A brand-new Apple
ID has nothing until you open those apps once.

### Calendars appear, reminders do not

That account's Reminders have been upgraded to Apple's private format. Part D.

### "Server address" — when to fill it in

Almost never. Task Hub discovers the correct server automatically. Fill it in
only for a non-iCloud CalDAV server (Fastmail, Nextcloud, mailbox.org) — the
same connector handles those. Fastmail's is
`https://caldav.fastmail.com/dav/`; Nextcloud's is
`https://your-server/remote.php/dav/`.

### Sync is slow

Apple's CalDAV servers are noticeably slower than Google's or Todoist's,
especially the first time when everything is fetched at once. Later syncs
transfer only what changed. A first sync of several hundred items taking a
couple of minutes is normal.

### A reminder was completed but came back

Check the same list is not connected twice — once through the Apple connector
and again by pointing your phone at Task Hub's own server. Choose one route per
list.

---

# Revoking access

At **https://account.apple.com** → **Sign-In and Security** →
**App-Specific Passwords**, delete the `Task Hub` entry. Access stops
immediately. Nothing in your calendars or reminders is deleted, and you can
generate a new password whenever you like.
