# Daily summary by email

Task Hub can send you one message listing what is overdue, what is due today,
and what is coming over the next seven days — across every service and every
collection, with each line saying where it came from. You choose the time and
which days it goes out. It is set up entirely through the web interface: there
is no configuration file and no terminal step.

**Task Hub sends mail and never receives any.** There is no inbox, no listener
and no open port. Nothing can be sent *to* Task Hub by email, and nothing tries.

---

## What you need

An email account Task Hub can send through. Any provider works: Gmail, Outlook,
Fastmail, your own mail server, or a sending service like Mailgun or Postmark if
you already use one.

> **Use an app-specific password if your provider offers one.**
>
> Gmail, Yahoo and iCloud all refuse your normal password here and require one
> generated in your account's security settings. Even where the normal password
> would work, an app-specific one is better: it can be revoked on its own
> without changing the password that opens your mailbox.
>
> Task Hub encrypts it at rest with the same key as every other credential, and
> never logs it. It is still worth giving it the least powerful credential that
> does the job.

### The settings your provider uses

| Provider | Server | Port | Security |
| --- | --- | --- | --- |
| **Gmail** | `smtp.gmail.com` | 587 | STARTTLS |
| **Outlook / Microsoft 365** | `smtp-mail.outlook.com` | 587 | STARTTLS |
| **Fastmail** | `smtp.fastmail.com` | 465 | SSL/TLS |
| **iCloud** | `smtp.mail.me.com` | 587 | STARTTLS |
| **Yahoo** | `smtp.mail.yahoo.com` | 465 | SSL/TLS |

If one combination does not work, try the other: **the port and the security
setting have to agree.** 587 is almost always STARTTLS and 465 is almost always
SSL/TLS.

**Getting an app-specific password:**

- **Gmail** — [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
  Two-factor authentication has to be on first, or the page will not appear.
- **iCloud** — account.apple.com → Sign-In and Security → App-Specific Passwords.
  The same place as the one the Apple connector uses; make a second one.
- **Yahoo** — Account Security → Generate app password.
- **Fastmail** — Settings → Password & Security → New app password, and give it
  the *SMTP* permission rather than full access.

---

## Setting it up

Task Hub offers this during first-time setup, as the last step of the wizard,
with a **Skip — set this up later** button beside it. Skipping costs you
nothing: everything below is on the Settings page afterwards, and no other part
of Task Hub depends on email.

### Step 1 — The mail server

**Settings → Email.**

0. **Who sends your email?** — pick your provider from the list and the next
   three boxes fill themselves in correctly. Do this rather than typing the
   server name: `smtp.google.com` looks exactly right, does not exist, and fails
   with a message about not being able to reach the server, which sends you
   looking at your network instead of at the one wrong word. (Task Hub now
   corrects that particular name and a dozen like it, and says so.)
1. **Mail server** and **Port** from the table above, if your provider is not
   listed.
2. **Security** to match the port.
3. **Username** — usually your full email address.
4. **Password** — the app-specific one.
5. **Send from** — the address the message comes from. Most providers insist
   this matches the account signing in; if the message is rejected as a
   forgery, this is why.
6. **Save mail server**.

The password box being empty means "keep the one already saved", so you can
change the port later without retyping it.

### Step 2 — The summary

**Settings → Daily summary.**

1. **Send to** — where the summary goes. Often the same address, but it does not
   have to be.
2. **Time** — when it arrives, **in the timezone set at the top of the Settings
   page**, not the server's. Somebody in Denver gets seven in the morning
   theirs, wherever the container is.
3. **Which days** — tick any days you like. **Every day** and **Monday to
   Friday** are one click each, and any other combination works: a Monday-only
   message is a weekly plan rather than a daily one, and a Friday-only message
   is a review of what did not get done.
4. **Turn on daily summary**.

### Step 3 — Prove it works

**Send a test message**, under Email, sends one straight away. The address is
filled in with the summary's own recipient, so pressing the button without
touching anything tests the thing that will actually happen each morning; type a
different address to send it somewhere else.

The result is recorded beside the button — **Working** or **Failed**, with the
time it was checked — so you can come back later and see the answer rather than
having to test again. A failure quotes the mail server's own complaint and says
which part to fix: a rejected password, a refused sender, or a server that could
not be reached.

**Send one now**, under Daily summary, sends today's real list immediately, even
on a day with nothing due.

---

## What the message looks like

```
Subject: Task Hub — 2 overdue, 3 due today (2026-09-10)

OVERDUE (2)
- Renew the insurance (Thu 3 Sep) [Todoist · Personal]
- Send the invoice (Wed 9 Sep 17:00) [Microsoft To Do · Work]

DUE TODAY (3)
- Call the dentist [Apple · Personal]
- Stand-up (09:00) [Microsoft To Do · Work]
- Post the parcel (17:30) [Task Hub · Personal]

THE NEXT 7 DAYS (2)
- Dentist appointment (Fri 11 Sep 10:00) [Apple · Personal]
- Quarterly review (Tue 15 Sep) [TickTick · Work]

— Task Hub
```

Four things about what it contains:

- **Overdue comes first**, because the thing that is already late is the item
  most likely to matter, and a list that opens with today's work buries it.
- **Every line names where it came from.** With several services connected this
  is one list of things living in four different places, and the square brackets
  answer the question you are actually asking: which app do I open to deal with
  this? The name is the service the task was *first seen in* — its home —
  followed by the Task Hub collection it belongs to.
- **Completed and cancelled tasks are left out.** A summary of what to do should
  not be padded with what is already done.
- **The week ahead is context, not a reason to write.** A day whose only work is
  in the next seven days counts as a quiet day.

## How often it goes out

The summary is not locked to every morning. The **Which days** row takes any
combination:

| What you want | Tick |
| --- | --- |
| A message every morning | **Every day** |
| Nothing at the weekend | **Monday to Friday** |
| A plan for the week ahead | Monday only |
| A look back at what slipped | Friday only |
| Twice a week | Monday and Thursday, say |

Whatever you pick, the message still covers everything overdue, everything due
that day, and the following seven days — so a Monday-only summary is a weekly
plan, not a message that misses six days of work.

## Quiet days

**By default, a day with nothing overdue and nothing due sends no message at
all.** That is deliberate. A message that arrives every morning saying "nothing
due" stops being read within a week, and then the one that matters is not read
either.

If you would rather have the daily confirmation, tick **Send even on a day with
nothing due**.

---

## If it stops arriving

**Check the spam folder first**, particularly for the first message. A new
sender address that has never written to you before is exactly what a spam
filter is looking for.

**Look at the container log.** The scheduled job records what it did every
morning — sent, or nothing due, or the error. A mail failure never affects
syncing: the two run as separate jobs precisely so that a mail server being down
cannot interfere with keeping your tasks in step.

**App-specific passwords get revoked.** Changing your account password often
invalidates them, and the summary will stop with an authentication error. Make a
new one and paste it in.

### "Username and Password not accepted" from Gmail

This is the most common failure, and it does not mean you typed the password
wrongly. **Gmail refuses ordinary account passwords over SMTP, always** — even
the correct one, even with the right server. You need an app password:

1. Two-factor authentication has to be on:
   [myaccount.google.com/security](https://myaccount.google.com/security) →
   2-Step Verification. Without it the next page does not exist.
2. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
3. Type any name — "Task Hub" — and press **Create**.
4. Google shows sixteen letters in four groups. Paste them into **Password** in
   Task Hub. The spaces do not matter.
5. **Send a test message.**

The same applies to Yahoo and iCloud. Outlook.com needs one only if you have
two-step verification switched on.
