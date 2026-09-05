# How Task Hub finds its own address

Most self-hosted software has to be told where it lives. You set a base URL in a
configuration file, and if you later reach it a different way — a new address, a
tunnel, a phone instead of a laptop — things quietly break in ways that point
nowhere near the setting that caused them.

Task Hub does not work that way, and this page explains what it does instead,
because it changes what you have to set up.

---

## The short version

**Task Hub's address is whatever address you are using.** Open it at
`http://192.168.1.50:8080` and that is its address. Open the same install at
`https://taskhub.tailnet.ts.net` a minute later and that is its address now.

There is nothing to configure, and the same downloaded image is correct on a
Raspberry Pi on your home network, behind a Cloudflare tunnel, over Tailscale,
and behind your own nginx.

---

## Why this matters to you

The address is not just cosmetic. It is what Task Hub gives to other services:

- **The OAuth redirect address.** When you connect Google, Microsoft, Todoist or
  TickTick, you register an address with them and they send you back to it after
  you sign in. If it does not match to the character, the sign-in fails at the
  very last step with an error that does not say why.
- **The CalDAV address.** The one you type into your phone, your laptop's
  calendar, or DAVx⁵ on Android.

Because both are built from the request in front of it, the address Task Hub
shows you on a page is one that demonstrably works — it is the address that just
delivered that page to you.

---

## What each service will accept

Every service is fussy in its own way, and none of them tells you clearly.

| How you reach Task Hub | Google | Microsoft | Todoist | TickTick | Android, Thunderbird | iPhone, iPad, Mac |
| --- | --- | --- | --- | --- | --- | --- |
| `http://192.168.1.50:8080` | ✗ | ✗ | ✓ | ✓ | ✓ | **✗** |
| `http://192-168-1-50.sslip.io:8080` | ✗ | ✗ | ✓ | ✓ | ✓ | **✗** |
| `http://localhost:8080` | ✓ | ✓ | ✓ | ✓ | only on that machine | only on that machine |
| `https://name.tailnet.ts.net` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `https://tasks.example.com` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

The two rules behind that table:

- **HTTPS is required by Google and Microsoft**, with one exception: both always
  accept `localhost`. That exception exists precisely for software running on
  your own machine, and it is the reason the trick below works.
- **A bare number is rejected by Google**, even over HTTPS. A name is required.
  `sslip.io` provides one free: `192-168-1-50.sslip.io` resolves to
  `192.168.1.50` with no sign-up and nothing to install. On its own it is not
  enough for Google, which wants HTTPS as well — but it is what turns a numeric
  address into something a certificate can be issued for.

  Only Google applies this rule. An earlier version of this page said TickTick
  did too; it does not, and a numeric redirect URI saves in the TickTick
  Developer Center without complaint.

- **Apple devices need HTTPS for calendars and reminders.** This is the rule
  that catches people out, because iOS does not say so. Given a plain `http`
  address it offers to continue without SSL, saves the account, warns that it
  may not sync — and then never sends the password at all. The server sees one
  unauthenticated request, answers `401`, and hears nothing more. What the phone
  shows is *"CalDAV account verification failed"*, which reads exactly like a
  wrong password and is not one. Android's DAVx⁵ and Thunderbird are happy over
  plain `http`; Apple's clients are not, and no amount of retyping changes it.

  Task Hub can give you an HTTPS address without a terminal: **Settings →
  Remote access** runs a Cloudflare tunnel from inside the container. Tick the
  box, paste a token from Cloudflare's dashboard, and the address it gives you
  works for iPhones, for Google and for Microsoft all at once.

**You only need an acceptable address at the moment you connect a service.**
Afterwards it keeps working from any address for ever, because renewing a
connection does not involve your address at all.

### What happens when you move afterwards

Moving Task Hub — from a LAN address to Tailscale, from an SSH port forward to a
Cloudflare tunnel — breaks nothing that is already connected. It breaks the
*next* connection: reconnecting an account, adding a second one, or renewing
TickTick, which has no refresh and must be reconnected when its token expires.
Those send the address you are on at the time, and the console still holds only
the old one, so the sign-in fails at its final step, weeks after the move that
caused it, with an error that blames the service.

Task Hub records the address each account was connected at, and tells you on the
overview and on the service's own page when it no longer matches. Nothing is
broken when that appears, and the fix is one paste while everything still works:
add the new address to the console **alongside** the old one. Keeping several
registered is normal — it is what lets you connect over a port forward and then
use Task Hub through a tunnel.

If you have moved to an address the console would refuse — back to a bare LAN
address, say — Task Hub says that instead, because adding it is not the fix.

---

## Connecting Google or Microsoft with no setup at all

Borrow your own computer's `localhost` for two minutes. On the computer you
browse from, in a terminal:

```
ssh -L 8080:localhost:8080 pi@taskhub.local
```

Replace `pi@taskhub.local` with the machine Task Hub runs on. Leave that window
open and browse to **`http://localhost:8080`**.

That is the same Task Hub — the connection is being carried across — but the
address is now one Google and Microsoft accept without argument. Connect them,
then close the terminal and go back to using whatever address you like.

---

## Behind a reverse proxy

If you run nginx, Caddy, Traefik or Nginx Proxy Manager, Task Hub reads the
headers your proxy sends and reports the browser's address rather than the
proxy's. It understands `Host`, `X-Forwarded-Proto`, `X-Forwarded-Host`,
`X-Forwarded-Port` and the standard `Forwarded` header.

There is a ready-made nginx configuration in the project at
`deploy/nginx-taskhub.conf`. Two rules apply to any proxy:

**Forward the headers.** A proxy that does not pass `Host` or `X-Forwarded-Host`
leaves Task Hub describing your proxy's internal address, which is no use to
anyone. Most proxies do this by default; nginx configured by hand does not.

**Give Task Hub a name of its own.** `tasks.example.com` works.
`example.com/tasks` does not — Task Hub's pages link to `/settings`, `/tasks`
and so on from the root of the site, so a sub-path sends every link to the wrong
place. Use a subdomain.

---

## When the headers are believed, and when they are not

Those headers are just text in a request, and anyone who can reach Task Hub can
put anything in them. So they are only honoured when the connection arrives from
somewhere a proxy plausibly is: the same machine, or your local network. A
request straight off the public internet is taken at face value and its claims
about being something else are ignored.

That default is right for essentially every home setup. If you have a proxy that
reaches Task Hub over a public address, set `TASKHUB_TRUST_PROXY=always` in your
`.env` file. To ignore the headers entirely, set it to `never`.

---

## The manual override, and why you probably do not want it

**Settings → Public address** lets you set the address by hand. It is there for
one case: the address you need to hand out is not the address you are using.
Setting up from the machine Task Hub runs on, while your phone will reach it by
a different name, is the example.

It changes the CalDAV address Task Hub hands out, and nothing else. A value left
over from an earlier setup therefore breaks phone sync with no obvious cause, so
Task Hub warns you on the Radicale page when the override disagrees with the
address you are actually using. The safest setting is an empty one.

**It does not change where the task services send you back to.** Connecting
Google, Microsoft, Todoist or TickTick always uses the address you are reaching
Task Hub on at that moment, whatever is set here. That is deliberate: the
address that just delivered the page to your browser is one that demonstrably
works, and a redirect address that disagrees with the browser by a single
character fails at the last step of sign-in. So to register a particular
address with a service, reach Task Hub on that address and connect it there.
