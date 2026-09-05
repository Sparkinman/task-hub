# Google setup — complete walkthrough

Every click, in order, from an empty Google account to a working two-way sync.
No step is assumed. If a step says "click the blue CREATE button", there is a
blue button labelled CREATE.

**Time needed:** about 15 minutes, once. Adding more Google accounts later takes
about 30 seconds each.

**Cost:** nothing. The APIs used here are free, and Google will not ask for a
credit card.

---

## The three values you will move

Only three pieces of text travel between the two windows. Everything else is
clicking. It helps to know in advance what you are collecting.

| # | Value | Created where | Pasted where |
|---|---|---|---|
| 1 | **Redirect URI** | Task Hub → Services → Google | Google Console → Clients → Authorised redirect URIs |
| 2 | **Client ID** | Google Console (after creating the client) | Task Hub → Services → Google → Client ID |
| 3 | **Client Secret** | Google Console (same moment) | Task Hub → Services → Google → Client Secret |

Value 1 goes **from Task Hub to Google**. Values 2 and 3 come **back from Google
to Task Hub**.

**Open two browser tabs now** — Task Hub in one, Google in the other — and keep
both open throughout. You will switch between them three times.

---

## Before you start: which address are you using?

This matters, and getting it wrong is the most common failure.

Look at your browser's address bar on the Task Hub tab.

- If it says **`localhost`** or **`127.0.0.1`** — good, carry on.
- If it says an address like **`192.168.1.42`** — **stop.** Google refuses to
  accept private IP addresses. Open Task Hub at `http://localhost:8080` instead,
  on the machine running Docker, just for this setup.

Google's rules, which cannot be worked around:

| Address you use | Google accepts it? |
|---|---|
| `http://localhost:8080` | Yes |
| `http://127.0.0.1:8080` | Yes |
| `http://192.168.1.42:8080` | **No** — raw IP address |
| `http://taskhub.local:8080` | **No** — not HTTPS, not localhost |
| `https://tasks.yourdomain.com` | Yes — needs a real certificate |

Once connected, Task Hub keeps the login on the server and syncs on its own
schedule. It never needs your browser again, so you can go straight back to
using whatever address you normally do.

Task Hub also checks this for you: if the address will not work, the Google page
shows a red warning instead of letting you waste ten minutes.

---

# Part A — Google Cloud Console

## Step 1 — Sign in

1. Open a new browser tab and go to **https://console.cloud.google.com**
2. Sign in with **the Google account whose tasks and calendar you want to
   sync**.
3. If you have several Google accounts, check the circular profile picture in
   the **top-right corner**. Click it to confirm you are in the right account.
   Being in the wrong one here is easy and confusing later.
4. First time only: a **Welcome** page appears. Tick the terms-of-service box,
   choose your country, and click **AGREE AND CONTINUE**.

## Step 2 — Create a project

A "project" is just a container for your settings.

5. Look at the **very top-left** of the page, immediately to the right of the
   words "Google Cloud". There is a dropdown showing either a project name or
   **Select a project**. Click it.
6. A dialog titled **Select a resource** opens. In its **top-right corner**,
   click **NEW PROJECT**.
7. In **Project name**, type: `Task Hub`
8. Leave **Location** as **No organisation**.
9. Click the blue **CREATE** button.
10. Wait about ten seconds. A notification appears in the top-right saying the
    project is ready.
11. **Do not skip this:** click the project dropdown at the top-left again and
    select **Task Hub**. The name at the top of the screen must read **Task
    Hub** before you continue. Everything below applies only to the project
    that is currently selected.

## Step 3 — Switch on the Google Tasks API

Google keeps every API switched off until you ask for it by name.

12. Click into the **search bar** running across the top of the page (it says
    "Search (/) for resources, docs, products, and more").
13. Type: `Google Tasks API`
14. In the dropdown of results, look for the section headed **Marketplace** and
    click **Google Tasks API**.
15. You land on a page with the Google Tasks API name and a blue **ENABLE**
    button. Click **ENABLE**.
16. Wait for the page to change. When it shows a dashboard with graphs, the API
    is on.

## Step 4 — Switch on the Google Calendar API

Exactly the same, for the second API.

17. Click the **search bar** at the top again.
18. Type: `Google Calendar API`
19. Click **Google Calendar API** under **Marketplace**.
20. Click the blue **ENABLE** button.

> Enable both even if you only want tasks. Task Hub asks Google for permission
> to both, and an API that is switched off produces a confusing error much later.

## Step 5 — Set up the consent screen

This is the screen you will see when you grant access.

21. Click the **search bar** and type: `Google Auth Platform`, then click the
    result.
    *(Alternative route: click the **☰** hamburger menu at the top-left → **APIs
    & Services** → **OAuth consent screen**. Both arrive at the same place.)*
22. **What you see next depends on whether this project is new:**

    **If you see a "Google Auth Platform" page with a GET STARTED button** —
    click **GET STARTED**, then fill in the form that appears:
    - **App name**: `Task Hub`
    - **User support email**: pick your own address from the dropdown.
    - Click **NEXT**.
    - **Audience**: select **External**. Click **NEXT**.
      - *You may also see **Internal**. That only appears if you have a Google
        Workspace organisation. If you have it and you are the only user,
        Internal is actually better — it skips the warning screen in Step 34.
        Personal Gmail accounts must choose External.*
    - **Contact Information**: type your own email address. Click **NEXT**.
    - Tick **I agree to the Google API Services: User Data Policy**.
    - Click **CONTINUE**, then **CREATE**.

    **If you instead see a page with a left-hand menu listing Overview,
    Branding, Audience, Clients, Data Access** — the consent screen already
    exists. Click **Branding**, check that **App name** and **User support
    email** are filled in, and click **SAVE**.

## Step 6 — Publish the app ⚠️ THE MOST IMPORTANT STEP

Skipping this is the number one reason self-hosted Google syncs break.

23. In the left-hand menu of the Google Auth Platform, click **Audience**.
24. Near the top of the page, find **Publishing status**.
25. **If it says "Testing"**, click the **PUBLISH APP** button.
26. A dialog appears asking you to confirm. Click **CONFIRM**.
27. Check that **Publishing status** now reads **In production**.

### Why this matters so much

While the status is **Testing**, Google deliberately expires your saved login
after exactly **7 days**. Task Hub would sync perfectly for a week, then stop
with an authorisation error. Every week. Forever.

Setting it to **In production** makes the login last indefinitely.

### Does "publish" make my app public?

No. It is not listed anywhere, not searchable, and nobody can use it without
your Client Secret. "Published" here only means "not in test mode".

### Will I need Google to verify my app?

No. Verification is for apps distributed to other people. Because yours is
unverified, Google shows one warning screen when you connect, which you click
through once. That is covered in Step 34.

---

# Part B — Get the redirect address from Task Hub

**Switch to your Task Hub tab.**

28. In the left sidebar, click **Services**.
29. Click the **Google** card.
30. At the top of the page is a box labelled **Authorised redirect URI**, with a
    **Copy** button beside it. Click **Copy**.

    The value looks like this:

    ```
    http://localhost:8080/oauth/google/callback
    ```

    > If a **red warning** appears above this box, Google will reject the
    > address. Re-read "Before you start" above — you almost certainly need to
    > open Task Hub at `http://localhost:8080` instead.

Keep this on your clipboard. You paste it in Step 36.

---

# Part C — Create the OAuth client

**Switch back to your Google Cloud Console tab.**

31. In the left-hand menu of the Google Auth Platform, click **Clients**.
32. At the top of the page, click **+ CREATE CLIENT**.
33. **Application type**: open the dropdown and choose **Web application**.

    > Choose **Web application**, not "Desktop app". Task Hub receives Google's
    > answer in your browser, which is what "Web application" means here.
    > Picking "Desktop app" produces a redirect error that is hard to diagnose.

34. **Name**: type `Task Hub`. This is only ever shown to you.
35. Scroll down to the section headed **Authorised redirect URIs**.
36. Click **+ ADD URI**.
37. **Paste the address you copied in Step 30** into the box that appears.

    Check it carefully before continuing:
    - It must end in `/oauth/google/callback`
    - There must be **no trailing slash** after `callback`
    - `http` (not `https`) is correct **only** for `localhost` or `127.0.0.1`
    - The port number must match (`8080` unless you changed it)

    Google compares this **character for character**. One wrong character
    produces `Error 400: redirect_uri_mismatch` at the very end.

38. Leave **Authorised JavaScript origins** completely empty.
39. Click the blue **CREATE** button.
40. A dialog appears titled **OAuth client created**, showing:
    - **Client ID** — a long string ending in `.apps.googleusercontent.com`
    - **Client Secret** — a shorter string usually starting with `GOCSPX-`

**Leave this dialog open.** You need both values in the next part.

> ⚠️ **The Client Secret is shown only once.** If you close this dialog without
> copying it, you cannot get it back — but nothing is lost: click your client in
> the **Clients** list and use **ADD SECRET**, or delete the client and repeat
> Steps 32–39.
>
> Clicking **DOWNLOAD JSON** saves both values to a file, which is a safe way to
> avoid losing them.

---

# Part D — Paste the credentials into Task Hub

**Switch to your Task Hub tab** (still on Services → Google).

41. In the Google dialog, click the **copy icon** next to **Client ID**.
42. In Task Hub, click the **Client ID** box and paste.
43. Switch back to Google, click the **copy icon** next to **Client Secret**.
44. In Task Hub, click the **Client Secret** box and paste.
45. Click **Save credentials**.

You should see a green confirmation. Task Hub checks the shape of the Client ID
and tells you immediately if the two values were swapped — a common slip.

> The secret is encrypted before being stored and is never displayed again. All
> ten Google account slots share these credentials, so this part is done once no
> matter how many Google accounts you connect.

You can now close the Google dialog and the Cloud Console tab. The rest happens
in Task Hub.

---

# Part E — Connect your Google account

46. Still on **Services → Google**, scroll to **Step 2 · Connect your Google
    accounts**.
47. Click the blue **Connect** button next to the empty slot.
48. Your browser goes to Google's sign-in page. Choose the account you want to
    sync.
49. **You will now see a warning: "Google hasn't verified this app".**

    This is expected. It is your own private app and Google has not reviewed it.

    - Click **Advanced** — a small link at the **bottom-left** of the warning.
    - The panel expands. Click **Go to Task Hub (unsafe)**.
    - The word "unsafe" is Google being cautious about apps it has not reviewed.
      You created this one, five minutes ago.

50. Google now lists what is being requested — your tasks and your calendars.
    Scroll down and click **Continue**.
51. Your browser returns to Task Hub, which shows a green message with the
    connected email address.

Task Hub immediately makes a real call to Google to confirm the connection
works, so if anything is wrong you find out now rather than at the next sync.

---

# Part F — Choose what syncs

Connecting an account syncs **nothing** by itself. Nothing moves until you
choose it — Task Hub will never push your tasks somewhere you did not ask for.

52. Still on **Services → Google**, click **Refresh lists** next to your
    account. Task Hub asks Google for your task lists and calendars, and shows
    what it found.
53. In the left sidebar, click **Sync**.
54. You will see one panel for each of your Radicale collections — for example
    **Tasks** and **Calendar** (created by the setup wizard).
55. Find the panel for the collection you want to fill, and look at its table.
    Each row is one of your Google lists, with two tick boxes:

    - **Read into [collection]** — pull items *from* that Google list into
      Radicale.
    - **Write back to this list** — push items *from* Radicale *to* that Google
      list.

56. Tick the boxes you want. You can tick as many rows as you like, and the two
    columns are independent:

    - **Both ticked** — full two-way sync. This is the usual choice.
    - **Read only** — mirror a shared or read-only calendar without ever
      writing to it.
    - **Write only** — push everything out to a list without pulling anything
      back.
    - **Read three, write one** — gather several Google lists into one
      collection, but send new items to only one of them.

57. Click **Save** at the bottom of that panel.
58. Repeat for your calendar collection if you want calendar sync too.

> Tasks and calendars are kept separate, so a task collection only offers task
> lists and a calendar collection only offers calendars. Every service stores
> them separately too.

> ⚠️ **Think before ticking your main calendar.** Selecting your primary Google
> Calendar means every event you have ever had is copied into Radicale. That is
> fine, and it is what some people want — but the first sync can take several
> minutes and the result is a very full collection. Try a small test calendar
> first if you are unsure.

---

# Part G — First sync

59. On the **Sync** page, click **Sync now** at the top-right.
60. You are taken to **Sync history**, showing what was pulled, pushed and
    skipped.
61. Click **Tasks** in the sidebar. Your Google tasks are there, each with a
    green **Google** badge showing where it came from.

From now on Task Hub syncs by itself at the interval you chose during setup
(minimum 3 minutes; 15 is a good default).

**The first sync is the slow one.** Later syncs only move what actually changed,
and the history page reports the rest as "skipped".

---

# What Google can and cannot store

These are limits of Google's own API, not of Task Hub.

| | Google Tasks | Google Calendar |
|---|---|---|
| Date | Yes | Yes |
| **Time of day** | **No** | Yes |
| Notes / description | Yes | Yes |
| Priority | **No** | — |
| Tags / labels | **No** | **No** |
| Location | **No** | Yes |
| Repeating items | **No** (via the API) | Yes |

## The due-time problem, and why Task Hub is built the way it is

Google Tasks cannot store a time of day. Send it "5 March at 2:30pm" and it
keeps "5 March" and throws the time away.

A naive sync would read that back, conclude you had deleted the time, and erase
2:30pm from every other service too. That is the single most destructive thing a
task sync can do.

Task Hub stores the date and the time as **separate pieces of information**, and
every connector declares what it is actually capable of holding. Because the
Google connector declares that it cannot hold a time, Task Hub never lets
Google's answer touch the time — only the date.

What you will actually see:

1. Create a task for **5 March at 2:30pm** in Todoist or in Task Hub.
2. In Google it appears as **5 March**, with no time. That is Google's limit.
3. Change that task in Google to **9 March**.
4. After the next sync it reads **9 March at 2:30pm** everywhere.

Your date change was applied. Your time was not lost. The same protection covers
priorities and tags, which Google Tasks also cannot store.

---

# Troubleshooting

### "Error 400: redirect_uri_mismatch"

The address registered with Google does not exactly match the one Task Hub sent.

1. In Task Hub: **Services → Google**, click **Copy** on the Authorised redirect
   URI.
2. In Google: **Google Auth Platform → Clients →** click your client name →
   **Authorised redirect URIs**.
3. Compare them character by character. The usual culprits:
   - a trailing `/` after `callback`
   - `https` where it should be `http`
   - a different port
   - `127.0.0.1` in one place and `localhost` in the other — Google treats these
     as different addresses
   - a private IP such as `192.168.1.42`, which Google always rejects
4. Fix it, click **SAVE**, wait a minute or two for Google to catch up, then try
   again.

### Sync works for a week, then stops

Your app is still in **Testing**. Go to **Google Auth Platform → Audience** and
click **PUBLISH APP** (Steps 23–27), then click **Reconnect** in Task Hub.

### "Google did not return a refresh token"

Google only issues a long-term token the first time an account authorises an
app. Clear the old grant and try again:

1. Go to **https://myaccount.google.com/permissions**
2. Find **Task Hub** and click it, then **Remove access**.
3. Back in Task Hub, click **Connect** again.

### "invalid_client"

The Client ID or Secret was pasted wrongly. Check for a leading or trailing
space, and confirm they are not swapped — the **Client ID** ends in
`.apps.googleusercontent.com`.

### "Google Tasks API has not been used in project ... or it is disabled"

Step 3 or Step 4 was missed, or was done while a different project was selected.
Check the project name at the top-left of the console, then enable the API again.

### "Access blocked: Task Hub has not completed the Google verification process"

You are on the warning screen from Step 49 and have not clicked through it.
Click **Advanced** at the bottom-left, then **Go to Task Hub (unsafe)**.

If there is no **Advanced** link, your app is set to **External** *and*
**Testing**, and your account is not in the test-user list. The fix is to
publish the app (Steps 23–27).

### Tasks appear in Task Hub but never reach Google

Check the **Write back to this list** box is ticked for that list on the **Sync**
page. Read and write are separate switches.

### Nothing syncs at all

- On the **Sync** page, does the collection show a green "Reading n · Writing n"
  badge? If it says "Not syncing", nothing has been ticked.
- Check **Sync → History** for the actual error message.
- If a Google list shows "Already syncing with …", it belongs to a different
  collection. A list can only feed one collection.

### A task appeared twice

This can happen the first time you connect a service that already holds the same
tasks. Task Hub matches existing items by their exact title and deliberately
refuses to guess when a title is ambiguous — creating a duplicate you can delete
is safer than merging two tasks that were never the same one. Delete the
duplicate; it will not return.

### The first sync is taking a long time

Normal if you selected a large calendar. Watch **Sync → History**; the counts
update as it works. Later syncs are much faster because unchanged items are
skipped entirely.

---

# Adding more Google accounts

Repeat **Part E** and **Part F** only. The Cloud Console work is done — all ten
slots share the same Client ID and Secret.

# Disconnecting

Clicking **Disconnect** in Task Hub deletes the saved login. To revoke access
from Google's side as well, go to **https://myaccount.google.com/permissions**,
find Task Hub, and click **Remove access**.

Neither action deletes any of your tasks or events, in Google or in Task Hub.

---

## Subtasks

Carried both ways. Google's API accepted more than one level of nesting when
tested, though Google's own apps appear to show only one — so Task Hub may be
holding more structure than Google displays. [How subtasks work](subtasks.md).
