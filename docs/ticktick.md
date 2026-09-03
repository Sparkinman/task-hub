# TickTick setup — complete walkthrough

Every click, in order, from an empty TickTick account to a working two-way sync.
No step is assumed.

**Time needed:** about 10 minutes, once.

**Cost:** nothing. TickTick's Open API is free and works on the free plan.

---

## Read this first: TickTick's API is unusually limited

This is not a complaint about Task Hub and it is not something Task Hub can work
around by trying harder. TickTick's public **Open API** is genuinely narrow, and
three of its gaps change what you can expect.

**1. There is no "list all tasks" endpoint.** The only way to read tasks is to
ask for one project at a time. That is fine, but it means every mapped list costs
its own request on every sync.

**2. Completed tasks are not returned.** A project listing contains only tasks
that are still open. A task you tick off in TickTick does not appear as
completed — it simply vanishes from the response.

That second point is dangerous rather than merely annoying, because a task that
disappears from a listing normally means it was deleted, and a deletion
propagates everywhere. Task Hub therefore **never treats a TickTick listing as
complete**. Instead it remembers which tasks it saw last time, and asks TickTick
about each one that has gone missing, individually. TickTick will answer for a
completed task even though it omits it from the listing. So:

- A task that comes back marked complete is reported as **completed**.
- A task that answers "not found" is reported as **deleted**.
- If dozens vanish at once, Task Hub assumes a TickTick fault rather than a
  burst of productivity, leaves them alone, and says so in the sync log.

The practical effect is that completions **do** propagate, at the cost of one
extra request per completed task.

**3. The Inbox is invisible.** TickTick's project listing does not include the
Inbox, and there is no way to reach tasks that have no project. Anything in your
TickTick Inbox cannot be synced. Move it into a real list if you want it to
appear.

**4. There is no calendar.** TickTick's calendar is not exposed to third parties
at all. Only tasks sync.

---

## The three values you will move

| # | Value | Created where | Pasted where |
|---|---|---|---|
| 1 | **Redirect URI** | Task Hub → Services → TickTick | TickTick Developer Center → OAuth redirect URL |
| 2 | **Client ID** | TickTick Developer Center | Task Hub → Services → TickTick → Client ID |
| 3 | **Client Secret** | TickTick Developer Center | Task Hub → Services → TickTick → Client Secret |

**Open two browser tabs now** — Task Hub in one, TickTick in the other. Task
Hub's links to TickTick open in a new tab for exactly this reason.

---

## Before you start: which address are you using?

TickTick accepts plain `http` and private network addresses, so whatever address
you already use will work.

| Address you use | TickTick accepts it? |
|---|---|
| `http://localhost:8080` | Yes |
| `http://192.168.1.42:8080` | Yes |
| `https://tasks.example.com` | Yes |

The one absolute rule: **the redirect URI registered with TickTick must match the
address you actually use, character for character.** `http` and `https` differ. A
trailing slash differs. A port differs.

TickTick's error message when they do not match is particularly unhelpful — often
just a blank page or a bare `400` — so it is worth checking twice rather than
guessing afterwards.

---

# Part A — Create the TickTick application

## Step 1 — Open the Developer Center

On the Task Hub TickTick page, click **Open the TickTick Developer Center**. It
opens in a new tab at:

```
https://developer.ticktick.com/manage
```

Sign in with the TickTick account you want to sync.

This is a different site from the TickTick app itself, and it is easy to end up
on the marketing page by mistake. The address must end in `/manage`.

## Step 2 — Create a new app

Click **New App**.

- **Name:** `Task Hub`, or anything you like.

Click **Save** / **Create**.

Your app now appears in the list. Click it to open its settings.

## Step 3 — Copy the redirect URI from Task Hub

Switch to the Task Hub tab, on **Services → TickTick**.

Find the box labelled **Redirect URI** and click **Copy**. It will look like:

```
http://192.168.1.42:8080/oauth/ticktick/callback
```

## Step 4 — Paste it into TickTick

Back on the TickTick tab, find **OAuth redirect URL** (sometimes shown as
*Redirect URI*).

Paste the value. Add nothing. Click **Save**.

## Step 5 — Copy the Client ID and Client Secret

On the same app page you will see:

- **Client ID**
- **Client Secret**

Copy both. Keep the tab open until you have pasted them successfully, in case
you need to re-copy.

---

# Part B — Paste the credentials into Task Hub

On the Task Hub tab, **Services → TickTick**:

1. Paste the **Client ID**.
2. Paste the **Client Secret**.
3. Click **Save application details**.

The card should show a green **Configured** badge.

Both values are encrypted before being written to disk, with the key in
`/data/secret.key`.

---

# Part C — Connect your TickTick account

Click **Connect a TickTick account**.

You are sent to TickTick's own sign-in page. Task Hub never sees your TickTick
password.

TickTick asks you to approve two permissions:

- `tasks:read` — read your tasks and lists
- `tasks:write` — create, update and delete tasks

TickTick does not offer a separate delete scope, so deletion comes bundled with
writing. Task Hub only ever deletes a task in TickTick in response to that task
being deleted somewhere else.

Click **Allow**.

You are returned to Task Hub, which immediately verifies the token by listing
your projects. The account card should show a green **Connected** badge.

## If the browser does not come back to Task Hub

This is common with TickTick, and it is not a fault.

TickTick will only ever redirect to **the one address registered in the
Developer Center**. If that is not the address you are browsing with — you
registered `http://localhost:8080/...` but you are using
`http://192.168.1.42:8080/...`, or you are on a phone — the redirect lands
somewhere that cannot receive it. You end up on an error page, a blank page, or
a "site cannot be reached".

**The authorization code is still in the address bar of that page.** Nothing is
lost.

1. Select the whole address from the failed page's address bar and copy it. It
   looks like:

   ```
   http://localhost:8080/oauth/ticktick/callback?code=abc123def456&state=xyz
   ```

2. Return to Task Hub, **Services → TickTick**.
3. Open **The browser did not come back to Task Hub — finish by hand**.
4. Paste the whole address and click **Finish connecting**.

Task Hub pulls the `code` out of whatever you paste and exchanges it for a
token. You may paste the entire address, just the query string, or just the code
itself — all three work, because "paste only the part after `code=`" is exactly
the instruction people get wrong.

The code is single-use and short-lived. If it has already expired, start the
connection again and paste the fresh one promptly.

## A word about TickTick tokens

TickTick issues **no refresh token**. The access token it gives out is
long-lived, and Task Hub uses it as-is — but when it eventually expires or is
revoked, there is no way to renew it automatically.

If TickTick starts refusing, the account will show **Needs reconnecting** and the
fix is to click **Reconnect** and approve again. Nothing is lost: your mappings,
your links and your tasks all survive a reconnection.

---

# Part D — Fetch your lists

On the account card, click **Refresh lists**.

Task Hub asks TickTick for every project and lists them.

Remember that your **Inbox will not appear** — see the limitations at the top.
That is TickTick's API, not a bug here. Shared lists you have read-only access
to appear but are marked accordingly.

---

# Part E — Choose what syncs

Each TickTick list gets a row with three columns.

### Column 1 — the list

Just its name.

### Column 2 — "Read into these collections"

Tick the Radicale collections this list's tasks should flow **into**. More than
one is allowed — one TickTick list can feed several collections.

Beneath it: **Changes only — don't add new tasks from this list.** Leave it clear
for a list that genuinely originates tasks; tick it for a list that is only a
destination.

### Column 3 — "Write the result back out to these lists"

Tick the lists that should **receive** what the collection produces. Every list
from every connected service appears here, grouped by service — so a TickTick
list can be written to from a collection fed by Google, and vice versa.

The targets need not include the list the tasks came from:

> Read **TickTick → Work** into the collection **Office**. Write the merged
> result out to **Google → Team Tasks**. Your TickTick list stays personal; the
> shared view lives in Google.

Ticking a write target sets that target up for you — its collection box is
ticked and **Changes only** is switched on — so completions come back from it
without new tasks added there flowing the other way. Clear Changes only if you
want a full mirror.

**One rule Task Hub enforces:** a list may accept write-back from only one
collection, because two would each create their own copy of every task and then
undo one another.

### Then save

Press the amber **Save sync settings for this account** bar. Nothing takes effect
until it is saved.

---

# Part F — First sync

Press **Sync now** at the top of the page and watch the history page.

Because of the completed-task limitation described at the top, the **first**
sync of a TickTick list establishes the baseline of which tasks exist. Completion
detection begins working from the **second** sync onward — there is nothing to
compare against until then. If you complete a task in TickTick and it does not
propagate immediately after your very first sync, run one more.

---

# What TickTick can and cannot store

| Field | TickTick | Notes |
|---|---|---|
| Title | Yes | |
| Notes | Yes | The task's content field |
| Completion | Yes | Detected as described above |
| Due date | Yes | |
| **Due time** | **Yes** | |
| Start date | Yes | |
| Priority | Yes | See below |
| Recurrence | Yes | `repeatFlag` really is an RRULE, so it round-trips |
| **Tags** | **No** | See below |
| Location | **No** | A location set elsewhere is preserved, not erased |
| Calendar events | **No** | Not exposed by the Open API at all |
| Subtasks | Partially | Read as part of the parent; not synced individually |

## Priorities

TickTick uses 0 none, 1 low, 3 medium, 5 high. iCalendar runs 1 to 9 with 1 most
urgent and 0 unset.

| TickTick | Shown as | Back to TickTick |
|---|---|---|
| High (5) | 1 — highest | High |
| Medium (3) | 5 | Medium |
| Low (1) | 9 | Low |
| None (0) | unset | None |

The mapping round-trips exactly, so a task does not drift a priority step on
each pass.

## Why tags are not synced

TickTick does have tags. Its Open API neither returns them on a task nor accepts
them on a write.

Task Hub therefore does not claim the tags field for TickTick at all. This is
deliberate and it protects you: if Task Hub claimed the field, TickTick reporting
no tags would look like *you had removed them*, and the next sync would dutifully
strip the tags off the matching task in Todoist. Declining the field means tags
set anywhere else are left completely alone.

## All-day tasks

TickTick sends a full timestamp even for an all-day task, where the time portion
is meaningless. Task Hub reads the `isAllDay` flag and discards that phantom time
rather than propagating a midnight that nobody asked for.

---

# Adding a second TickTick account

The application you registered works for every account. Click **Connect a
TickTick account** again and sign in as the other one. Up to ten are supported,
each with its own lists and mapping table.

You will probably need a private browsing window, or to sign out of TickTick
first, or TickTick will reconnect the account you are already signed in as.

---

# Troubleshooting

### A blank page, or a bare 400, when you click Connect

Nearly always the redirect URI. TickTick fails this check without a useful
message.

Compare the value in Task Hub's **Redirect URI** box against the **OAuth redirect
URL** saved in the Developer Center, character by character. `http` vs `https`,
the port, and a trailing slash are all differences.

**If you got as far as approving access** and only then landed on a broken page,
you do not need to fix anything first — the code is in that page's address bar.
Copy the whole address and use **finish by hand**, described in Part C.

### "TickTick rejected the Client ID or Client Secret"

- Check for a leading or trailing space in either box.
- Confirm you are looking at the right app in the Developer Center.
- Re-copy both values and save again.

TickTick requires the client credentials to be sent as HTTP Basic authentication
rather than in the form body. Task Hub does this correctly; the reason it is
worth mentioning is that a great many third-party examples get it wrong, so if
you are comparing against one, that is why it differs.

### "TickTick refused this login" after it worked for a while

The access token expired or was revoked. TickTick issues no refresh token, so
this cannot be fixed automatically. Click **Reconnect** on the account card and
approve again. Nothing is lost.

### My Inbox tasks are missing

Expected. TickTick's API does not expose the Inbox. Move the tasks into a real
list.

### A completed task did not propagate

Two possibilities, in order of likelihood.

1. **It was the first sync of that list.** Completion detection compares against
   what was seen last time, and the first pass has nothing to compare against.
   Run a second sync.
2. **The list is not ticked in the collections column.** Write-back alone does
   not cause a list to be read. A row in that state is marked **One-way**.

### "N tasks disappeared from this list at once"

Task Hub's guard against a TickTick fault. If a large number of tasks vanish from
a listing simultaneously, that is far more likely to be a bad response than a
genuine mass completion, so nothing is changed. If they really were all
completed, complete them in batches, or wait — the next sync will handle a
smaller number.

### Tasks appear in Task Hub but never reach TickTick

1. Check the list has a **write** target ticked.
2. Check the master switch for tasks is on.
3. Check the amber save bar is not still showing unsaved changes.
4. Check the list is not one you only have read access to.

---

# What is stored, and where

| Item | Where | Protection |
|---|---|---|
| Client ID and Secret | `taskhub.db`, settings table | Encrypted with `/data/secret.key` |
| Access token | `taskhub.db`, accounts table | Encrypted with `/data/secret.key` |
| A pasted authorization code | Nowhere | Exchanged immediately and discarded |
| Which task ids were last seen | `taskhub.db`, accounts table | Plain; it is only a list of ids |
| Your TickTick password | Nowhere | Task Hub never receives it |

The "last seen" list is what makes completion detection possible. It is kept per
list rather than per account, so two TickTick lists in the same collection cannot
overwrite each other's bookkeeping.

Disconnecting an account deletes its stored token immediately. It does not delete
anything from TickTick, and it does not remove anything from your collections.
