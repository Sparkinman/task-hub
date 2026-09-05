# Todoist setup — complete walkthrough

**The short version:** paste an API token. Two minutes, no application to
register, nothing to configure. Everything after Part A is optional.

**Cost:** nothing. The Todoist API is free on the free plan.

---

## Read this first: which API version this uses

Todoist used to have two separate APIs — the **REST API v2** and the **Sync API
v9**. Both were shut down in early 2026. Anything written against them stopped
working entirely; it did not degrade or warn, it simply stopped.

Task Hub talks to the current **unified API v1** at
`https://api.todoist.com/api/v1`. That matters to you for one practical reason:
**all Todoist object IDs changed** when the old APIs were retired. If you
previously synced Todoist with some other tool and it has gone quiet, that is
why, and it is not something Task Hub can fix on that tool's behalf.

---

# Part A — Connect with an API token (recommended)

Todoist hands out a personal API token from its own settings. It is a bearer
token with full access to your account, it **never expires**, and it needs no
application registered anywhere.

For a Task Hub that you own and only you use, this achieves everything OAuth
would — minus the redirect URI, the client secret, and the hourly token refresh.

## Step 1 — Copy the token from Todoist

On the Task Hub Todoist page, click the link to open Todoist's developer
settings. It opens in a new tab at:

```
https://app.todoist.com/app/settings/integrations/developer
```

You can also get there by hand: **Todoist → your avatar (top left) → Settings →
Integrations → Developer**.

Click **Copy API token**.

## Step 2 — Paste it into Task Hub

Back on **Services → Todoist**, in the card headed **Connect with an API token**:

1. Give the account a name — `Personal`, `Work`, anything. It is only for you.
2. Paste the token.
3. Click **Connect this account**.

Task Hub checks the token against Todoist immediately, so a token with a stray
space in it fails now rather than silently at 3am. On success the card shows a
green **Connected** badge and your Todoist email address.

## Step 3 — Fetch your projects

Click **Refresh lists** on the account card. Task Hub asks Todoist for every
project and lists them, Inbox included.

That is the whole connection. Skip to
[Part C — Choose what syncs](#part-c--choose-what-syncs).

## Connecting more than one Todoist account

Paste a token from each. Open Todoist in a private window, sign in as the other
account, copy its token, and paste it into a new account card. Up to ten
accounts are supported, each with its own projects and its own mapping.

## A word about what the token can do

The token grants full read and write access to that Todoist account — treat it
exactly like a password. Task Hub encrypts it before writing it to disk, using
the key in `/data/secret.key`, and never displays it again.

If you ever want to revoke it, regenerate the token on the same Todoist settings
page. The old one stops working immediately, and Task Hub will report the
account as needing attention.

---

# Part C — Choose what syncs

This is the part worth reading slowly, because it is where Task Hub differs most
from a simple mirror.

Each Todoist project gets a row with three columns.

### Column 1 — the project

Just its name.

### Column 2 — "Read into these collections"

Tick the Radicale collections this project's tasks should flow **into**. You can
tick more than one: a single Todoist project can feed several collections, which
is how the same shopping list reaches two different people's calendars.

Under the dropdown is a checkbox: **Changes only — don't add new tasks from this
list.** Leave it clear for a project that is a genuine source of tasks. Tick it
for a project that is only a destination — see the next column.

### Column 3 — "Write the result back out to these lists"

Tick the lists that should **receive** what the collection produces. These are
not limited to Todoist: every list from every connected service appears here,
grouped by service, so a Todoist project can be fed from a Google list and vice
versa.

They also do not have to include the project the tasks came from. That is the
useful part:

> Read **Todoist → Errands** into the collection **Household**. Write the merged
> result out to **Google → Shared Errands**. Everyone sees the combined list in
> Google; your own Todoist project stays yours.

When you tick a write target, Task Hub sets it up for you: it ticks that list's
own collection box and switches on **Changes only**. That combination means a
task completed in the destination comes back to you, while a task somebody adds
directly to the destination stays there rather than appearing in your Todoist
project. If you would rather have a full two-way mirror, clear the Changes only
box on that row.

**One rule Task Hub enforces:** a list may accept write-back from only one
collection. Two collections writing into one list would each create their own
copy of every task and then undo one another on alternating passes. If you tick
a second, Task Hub refuses it and says which collection already has it.

### Then save

Press the amber **Save sync settings for this account** bar at the bottom. It
turns amber the moment you change anything, because nothing you tick has any
effect until it is saved.

---

# Part D — First sync

Press **Sync now** at the top of the page.

You are taken to the sync history, which updates as it works. The first pass
does the most: every task in every mapped project is read, matched up, and
written out. Later passes are much quieter because Task Hub only writes what has
actually changed.

Check the result in Todoist and in your collection. A task created in one should
be in the other.

---

# What Todoist can and cannot store

Task Hub declares these limits to its merge engine, which is what stops a field
Todoist cannot hold from being wiped out by a sync.

| Field | Todoist | Notes |
|---|---|---|
| Title | Yes | Up to 500 characters; longer titles are truncated on the way in |
| Notes | Yes | The task's description field |
| Completion | Yes | |
| Due date | Yes | |
| **Due time** | **Yes** | Unlike Google Tasks, Todoist keeps the time of day |
| Priority | Yes | See below |
| Tags | Yes | Todoist labels |
| Location | **No** | A location set elsewhere is preserved, not erased |
| Recurrence | **No** | See below |
| Calendar events | **No** | Todoist has no calendar; only tasks sync |

## Priorities

Todoist's scale runs 1 to 4 with **4 as most urgent**, and 1 meaning "no
priority". iCalendar — which Radicale, Apple and Microsoft all use — runs 1 to 9
with **1 as most urgent** and 0 meaning unset. The two are inverted as well as
differently sized.

Task Hub converts explicitly in both directions:

| Todoist | Shown as | Back to Todoist |
|---|---|---|
| P1 (4) | 1 — highest | P1 |
| P2 (3) | 3 | P2 |
| P3 (2) | 5 | P3 |
| P4 (1, default) | unset | P4 |

The mapping round-trips exactly, so a task moved Todoist → Task Hub → Todoist
comes back with the priority it started with rather than drifting a step on each
pass.

## Recurrence

Todoist stores recurrence as a human sentence — "every 2 weeks starting Monday" —
rather than as an iCalendar RRULE. There is no reliable, lossless conversion
between the two, and a wrong guess would silently change when your task recurs.

Task Hub therefore **does not claim** the recurrence field for Todoist. In
practice that means:

- A recurring Todoist task syncs as a task. Its recurrence stays in Todoist and
  continues to work there.
- A recurrence set in a calendar app is **preserved**, not overwritten by
  Todoist reporting nothing.
- Each occurrence Todoist generates appears as it becomes current.

## Completed tasks

Todoist's main task listing returns only what is still open. Completed tasks come
from a separate endpoint, which Task Hub queries for **the last 30 days**.

That window is why a task you completed in Todoist months ago will not suddenly
propagate; and it is also why a task completed today does. If reading the
completed list fails for any reason, Task Hub treats the whole pull as
incomplete, specifically so that the shorter listing is never mistaken for you
having deleted everything.

---

# Troubleshooting

### "Todoist rejected that token"

The token was mistyped, has a stray space, or has been regenerated in Todoist
since you copied it. Copy it again from **Settings → Integrations → Developer**
and paste it afresh.

### "invalid_client"

*(OAuth only.)*

Todoist did not recognise the Client ID or Client Secret.

- Check for a leading or trailing space in either box. Copying from a browser
  often picks one up.
- Check you copied from the right app, if you created more than one.
- Re-copy the secret and save again.

### "redirect_uri_mismatch", or Todoist refuses to return

The **OAuth redirect URL** saved in the Todoist App Management console does not
exactly match the address Task Hub sent.

Compare them character by character:

| Looks the same, is not | |
|---|---|
| `http://` vs `https://` | Different |
| `localhost` vs `127.0.0.1` | Different |
| `:8080` vs no port | Different |
| trailing `/` vs none | Different |

The value shown in Task Hub's **Redirect URI** box is always the correct one for
the address you are currently using. Copy it again and re-save it in Todoist.

### "That authorization code was already used or has expired"

The sign-in was started, then left sitting, or the browser back button was used.
Start the connection again from the beginning.

### "That sign-in did not match the one this page started"

Task Hub's protection against a connection request it did not initiate. It also
fires if your session expired mid-flow, or if you started the connection in one
browser and finished it in another. Sign in to Task Hub again and retry.

### Tasks appear in Task Hub but never reach Todoist

Almost always the mapping rather than the connection.

1. On **Services → Todoist**, check the project has a **write** target ticked.
2. Check the master switch for tasks at the top of the panel is on.
3. Check the amber save bar is not still showing unsaved changes.
4. Check the row is not marked **One-way**.

### A task I completed in Todoist did not complete elsewhere

Check that the project is ticked in the **collections** column. Write-back alone
does not make Task Hub read a list, and a list that is never read cannot report
that anything changed in it. A row in that state is marked **One-way**.

### Tasks are duplicating

Two collections have been pointed at the same Todoist project for write-back.
Task Hub refuses this when you save, but a configuration created before that
check existed can still be in the database. Open the project's row and make sure
exactly one collection writes to it.

### Everything stopped after early 2026

Your other tools, not Task Hub. The old REST v2 and Sync v9 APIs were retired,
and all Todoist object IDs changed with them. Task Hub uses the current unified
v1 API and is unaffected.

---

# What is stored, and where

| Item | Where | Protection |
|---|---|---|
| Client ID and Secret | `taskhub.db`, settings table | Encrypted with `/data/secret.key` |
| Access and refresh token | `taskhub.db`, accounts table | Encrypted with `/data/secret.key` |
| Your Todoist password | Nowhere | Task Hub never receives it |

Disconnecting an account deletes its stored tokens immediately. It does not
delete anything from Todoist, and it does not remove anything from your
collections.

---

# Appendix — Connecting with OAuth instead

You do not need this. It exists for anyone who would rather Task Hub never held
a token that works everywhere, or who is sharing one Task Hub between people.

It is more steps and more that can go wrong. If you are the only person using
this Task Hub, use the API token in Part A.

## Register the application

### Step 1 — Open the App Management console

On the Task Hub Todoist page, click **Open the Todoist App Management console**.
It opens in a new tab at:

```
https://developer.todoist.com/appconsole.html
```

Sign in with the Todoist account you want to sync, if you are not already.

### Step 2 — Create a new app

Click **Create a new app**.

- **App name:** `Task Hub` (or anything you like — you are the only person who
  will ever see it)
- **App service URL:** leave blank, or put your Task Hub address

Click **Create app**.

You are now on your app's settings page. Leave this tab open.

### Step 3 — Copy the redirect URI from Task Hub

Switch to the Task Hub tab, on **Services → Todoist**.

At the top of the page is a box labelled **Redirect URI** with a **Copy** button.
Click Copy. It will look like one of these:

```
http://localhost:8080/oauth/todoist/callback
http://192.168.1.42:8080/oauth/todoist/callback
https://tasks.example.com/oauth/todoist/callback
```

If Task Hub shows a warning under that box, read it now rather than later — it
is telling you the address will not work from the device you plan to use.

### Step 4 — Paste it into Todoist

Back on the Todoist tab, find the field labelled **OAuth redirect URL**.

Paste the value you just copied. Do not add anything to it. Do not add a
trailing slash.

Click **Save settings**.

### Step 5 — Copy the Client ID and Client Secret

Still on the Todoist app page, near the top, you will see:

- **Client ID** — a long string of letters and numbers
- **Client secret** — another long string

Unlike some services, Todoist will show you the secret again later, so there is
no danger of losing it. Still, copy both now.

---

## Paste the credentials into Task Hub

Switch to the Task Hub tab, **Services → Todoist**.

1. Paste the **Client ID** into the Client ID box.
2. Paste the **Client secret** into the Client Secret box.
3. Click **Save application details**.

The card should now show a green **Configured** badge.

The secret is encrypted before it is written to disk, using the key in
`/data/secret.key`. It is never shown again after saving — if you need to change
it, paste a fresh one.

---

## Connect the account

Still on **Services → Todoist**, click **Connect a Todoist account**.

You are sent to Todoist's own sign-in page. Task Hub never sees your Todoist
password — the whole point of OAuth is that your password never leaves Todoist.

Todoist will ask you to approve access for:

- **Read and write tasks** (`data:read_write`)
- **Delete** (`data:delete`)

The delete permission is worth a word, because granting deletion to anything
feels uncomfortable. Task Hub asks for it so that a task you delete in Google or
in your calendar app can also be removed from Todoist. Without it, a deleted task
would silently reappear in Todoist on the next sync and there would be no way to
get rid of it except by hand. Task Hub never deletes anything on its own
initiative — only in response to a deletion you made somewhere else.

Click **Agree**.

You are returned to Task Hub, which immediately checks the connection by asking
Todoist who you are. The account card should now show:

- A green **Connected** badge
- Your Todoist email address

If instead you see an error, jump to [Troubleshooting](#troubleshooting) below.

---

## Fetch your projects

On the account card, click **Refresh lists**.

Task Hub asks Todoist for every project on the account and lists them. Your
**Inbox** appears as a project too, marked as the default.

If a project is missing, it is almost always because it belongs to a Todoist
workspace or team you are a member of rather than owner of. Re-run Refresh lists
after opening it once in Todoist.

---

---

## Subtasks

Carried both ways, several levels deep.

One behaviour worth knowing: **completing a parent inside Todoist completes
every subtask under it.** That is Todoist's own behaviour, and Task Hub reads
the result rather than causing it — Task Hub itself never completes a task
because another one was completed. [How subtasks work](subtasks.md).
