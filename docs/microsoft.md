# Microsoft setup — complete walkthrough

> **Microsoft is not finished yet.** This connector is written but has never
> been run against a real Microsoft account, and it has no tests. The
> walkthrough below is complete as designed, but nobody has confirmed it works
> end to end. Setting it up asks you to create an Azure app registration, which
> is a substantial piece of work to do on something unproven. This notice comes
> off the day it completes a sync.

Connects **Microsoft To Do** and **Outlook Calendar**. Every click, in order,
from nothing to a working two-way sync.

**Time needed:** about 15 minutes, once. Extra Microsoft accounts take a minute
each afterwards.

**Cost:** nothing. Everything here is free, and Microsoft will not ask for a
card.

**Works with:** personal accounts (outlook.com, hotmail.com, live.com) and work
or school accounts. A work account may need an administrator's approval — see
the troubleshooting section.

---

## The three values you will move

Only three pieces of text travel between the two windows.

| # | Value | Created where | Pasted where |
|---|---|---|---|
| 1 | **Redirect URI** | Task Hub → Services → Microsoft | Azure → your app → Authentication |
| 2 | **Application (client) ID** | Azure → your app → Overview | Task Hub → Application ID |
| 3 | **Client secret** | Azure → your app → Certificates & secrets | Task Hub → Client secret |

**Open two browser tabs** — Task Hub in one, Azure in the other.

---

## Before you start: which address are you using?

Look at your browser's address bar on the Task Hub tab.

- **`localhost`** or **`127.0.0.1`** — fine, carry on.
- **`https://` and a real domain** — also fine.
- **`http://192.168.x.x`** — stop. Microsoft rejects plain `http` for anything
  that is not localhost. Open Task Hub at `http://localhost:8080` for the setup,
  or use your HTTPS address.

Once connected, Task Hub keeps the sign-in on the server. It never needs your
browser again, so you can go back to whatever address you normally use.

---

# Part A — Register the application in Azure

## Step 1 — Sign in

1. Go to **https://portal.azure.com** and sign in with the Microsoft account you
   want to sync.
2. In the search bar at the top, type `App registrations` and click it in the
   results.

> You do **not** need an Azure subscription, and you will not be charged. App
> registration is part of the free identity platform.

## Step 2 — Create the registration

3. Click **+ New registration**.
4. **Name**: `Task Hub`. Only you ever see this.
5. **Supported account types**: choose

   > **Accounts in any organizational directory (Any Microsoft Entra ID tenant –
   > Multitenant) and personal Microsoft accounts (e.g. Skype, Xbox)**

   This is the broadest option and the one that works for both a personal
   outlook.com account and a work account. Choosing a narrower one is the most
   common reason a personal account is refused later.

6. **Redirect URI**: leave it blank for now — you will add it in Step 8, once
   you have copied the exact address from Task Hub.
7. Click **Register**.

## Step 3 — Copy the Application ID

8. You land on the app's **Overview** page. Find **Application (client) ID** — a
   long value with dashes, like `4a1f8c22-9b3e-4d77-a0e1-2c6b5f9d8e34`.
9. Click the copy icon beside it and paste it somewhere for a moment. This is
   **value 2**.

---

# Part B — Get the redirect address from Task Hub

**Switch to your Task Hub tab.**

10. Sidebar → **Services** → **Microsoft**.
11. At the top is **Authorised redirect URI** with a **Copy** button. Click it.
    It looks like:

    ```
    http://localhost:8080/oauth/microsoft/callback
    ```

    If a red warning appears above it, Microsoft will reject that address —
    re-read "Before you start" above.

---

# Part C — Register the redirect address

**Back in Azure.**

12. In the left menu of your app, click **Authentication**.
13. Click **+ Add a platform**.
14. Choose **Web**.

    > **Web**, not "Single-page application" and not "Mobile and desktop". Task
    > Hub receives Microsoft's answer on its own server, which is what "Web"
    > means here. Picking the wrong one produces an error at the very end that
    > does not explain itself.

15. In **Redirect URIs**, paste the address you copied in Step 11.
16. Leave everything else as it is and click **Configure**.

---

# Part D — Create a client secret

17. In the left menu, click **Certificates & secrets**.
18. On the **Client secrets** tab, click **+ New client secret**.
19. **Description**: `Task Hub`. **Expires**: choose the longest offered —
    normally 24 months.
20. Click **Add**.
21. The new secret appears in a table with two columns, **Value** and
    **Secret ID**.

    > ⚠️ **Copy the `Value` column, not `Secret ID`.** This is the single most
    > common mistake with Azure. The Secret ID looks like a perfectly good
    > credential and is not one; Microsoft will reject it with an unhelpful
    > error. The **Value** is shown **only now** — once you navigate away it can
    > never be displayed again, though you can always delete the secret and make
    > another.

22. Copy the **Value**. This is **value 3**.

---

# Part E — Paste both values into Task Hub

**Switch to Task Hub** (Services → Microsoft).

23. Paste the **Application (client) ID** from Step 9 into **Application ID**.
24. Paste the **client secret Value** from Step 22 into **Client secret**.
25. Click **Save credentials**.

The secret is encrypted before storage and never displayed again. All ten
Microsoft slots share these credentials, so this part is done once.

---

# Part F — Connect your account

26. Scroll to the accounts section and click **Connect** on an empty slot.
27. Sign in to Microsoft and review the permissions being requested — your tasks
    and your calendars. Click **Accept**.
28. You return to Task Hub, which shows the connected address.

Task Hub immediately makes a real call to Microsoft to confirm the connection
works, so a problem surfaces now rather than at 3am.

---

# Part G — Choose what syncs

Connecting syncs **nothing** by itself.

29. Click **Refresh lists** next to your account. Task Hub asks Microsoft for
    your To Do lists and your calendars.
30. For each list you want, tick the Radicale collection it should sync with.
    One tick means two-way: changes here reach the collection, and changes made
    anywhere else in that collection come back.
31. Click **Save sync settings for this account**.
32. Press **Sync now**.

Tasks and calendars are kept separate, so a task collection only offers To Do
lists and a calendar collection only offers calendars.

---

# What Microsoft can and cannot store

| | Microsoft To Do | Outlook Calendar |
|---|---|---|
| Date | Yes | Yes |
| **Time of day** | **No** | Yes |
| Notes | Yes | Yes |
| Priority | Yes (high / normal / low) | — |
| Tags | Yes (categories) | — |
| Location | **No** | Yes |
| Repeating | **No** (via the API) | Yes |

## The due-time limitation

**Microsoft To Do stores a due date, not a due time.** Its API accepts a full
timestamp and its own apps show only the date; a time sent in is quietly
discarded. This is the same limitation Google Tasks has.

Task Hub keeps the date and the time as separate pieces of information, and the
Microsoft connector declares that it cannot hold a time. That declaration is
what stops Microsoft's answer from clearing a time you set in Todoist, in
TickTick, or on a CalDAV client.

In practice: set a task for **5 March at 2:30pm** in Todoist; it appears in
Microsoft To Do as **5 March**. Change the date in Microsoft to **9 March**, and
after the next sync it reads **9 March at 2:30pm** everywhere. Your date change
applied; your time survived.

## Recurring events

Outlook describes repetition as a structured object rather than a rule string.
Task Hub converts the common patterns — daily, weekly (including which days),
monthly on a date, and yearly. Anything more elaborate syncs as a **single
occurrence** rather than as a wrong repeating one, which is the safer failure.

---

# Troubleshooting

### "AADSTS7000215: Invalid client secret provided"

You pasted the **Secret ID** instead of the secret's **Value**. Go back to
**Certificates & secrets**, delete the secret, create a new one, and copy the
**Value** column this time.

### "AADSTS50011: The redirect URI specified does not match"

The address registered in Azure is not exactly the one Task Hub used.

1. In Task Hub, copy the **Authorised redirect URI** again.
2. In Azure: your app → **Authentication** → **Redirect URIs**.
3. Compare character by character. Watch for a trailing `/`, `http` versus
   `https`, a different port, or `127.0.0.1` in one place and `localhost` in the
   other — Microsoft treats those as different.

### "AADSTS65001: The user or administrator has not consented"

A work or school account whose administrator restricts which applications may
be used. Either ask them to approve it, or connect a personal account instead.
There is nothing Task Hub can do from this side.

### "Selected user account does not exist in tenant"

The registration was created with a narrower **Supported account types** than
Step 5 specifies. In Azure, your app → **Authentication** → **Supported account
types**, and change it to the multitenant-and-personal option.

### The sign-in works but no lists appear

Press **Refresh lists**. If it is still empty, check the permissions on the app
registration: **API permissions** should list `Tasks.ReadWrite` and
`Calendars.ReadWrite` under Microsoft Graph. Task Hub asks for these during
sign-in, so this normally only happens if consent was partially declined.

### Tasks reach Task Hub but never reach Microsoft

Check **Write back** is switched on for that list on the Microsoft page. Read
and write are separate switches.

### A task appeared twice

This can happen the first time you connect a service that already holds the same
tasks. Task Hub matches existing items by exact title and deliberately refuses
to guess when a title is ambiguous — a duplicate you can delete is safer than
merging two tasks that were never the same one. Delete it; it will not return.

---

# Adding more Microsoft accounts

Repeat Parts F and G only. The Azure work is done — all ten slots share the same
Application ID and secret.

# Disconnecting

**Disconnect** in Task Hub deletes the saved sign-in. To revoke access from
Microsoft's side as well, go to
**https://account.live.com/consent/Manage** (personal) or ask your
administrator (work). Neither action deletes any of your tasks or events.
