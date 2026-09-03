"""The address Task Hub believes it has, under every proxy arrangement.

These are the tests that keep one image universal. Every deployment shape Task
Hub claims to support -- a home network address, a Cloudflare tunnel, Tailscale,
somebody's own nginx -- differs only in which headers arrive and from where, so
each shape is one case here. A regression in this file means OAuth sign-in fails
for one whole class of user, with an error from Google that points at the wrong
thing entirely.
"""

from __future__ import annotations

import sys

from app.web import forwarded

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        _failures.append(name)
        print(f"  FAIL  {name}" + (f"  ({detail})" if detail else ""))


def scope(client=("127.0.0.1", 5000), host="taskhub.local:8080", scheme="http", **headers):
    raw = [(b"host", host.encode())]
    for key, value in headers.items():
        raw.append((key.replace("_", "-").encode(), value.encode()))
    return {"type": "http", "scheme": scheme, "headers": raw, "client": client}


def applied(trust="auto", **kwargs):
    """Run the middleware's rewrite and report the resulting address."""
    previous = forwarded.TRUST_MODE
    forwarded.TRUST_MODE = trust
    try:
        s = scope(**kwargs)
        forwarded.apply_forwarded(s)
        host = dict(s["headers"])[b"host"].decode()
        return f"{s['scheme']}://{host}", s.get("client")
    finally:
        forwarded.TRUST_MODE = previous


print("No proxy in front")

url, _ = applied(client=("192.168.1.20", 4000), host="192.168.1.50:8080")
check("a plain home-network visit is left exactly as it arrived",
      url == "http://192.168.1.50:8080", url)

print("\nReverse proxies")

url, client = applied(
    client=("127.0.0.1", 51000), host="127.0.0.1:8080",
    x_forwarded_proto="https", x_forwarded_host="tasks.example.com",
    x_forwarded_for="203.0.113.9",
)
check("nginx on the same machine, terminating TLS",
      url == "https://tasks.example.com", url)
check("  ...and the real visitor is restored, not the proxy",
      client == ("203.0.113.9", 0), str(client))

url, _ = applied(client=("192.168.1.2", 40000),
                 x_forwarded_proto="https", x_forwarded_host="tasks.example.com")
check("a proxy elsewhere on the home network is believed",
      url == "https://tasks.example.com", url)

url, _ = applied(client=("127.0.0.1", 6000), host="tasks.example.com",
                 x_forwarded_proto="https")
check("a Cloudflare tunnel, which rewrites the host itself",
      url == "https://tasks.example.com", url)

url, _ = applied(client=("127.0.0.1", 7000), host="pi.tail1234.ts.net",
                 x_forwarded_proto="https")
check("Tailscale, which serves real HTTPS on a .ts.net name",
      url == "https://pi.tail1234.ts.net", url)

url, _ = applied(client=("127.0.0.1", 8000),
                 forwarded='for=203.0.113.9;host="tasks.example.com";proto=https')
check("the standard Forwarded header, quotes and all",
      url == "https://tasks.example.com", url)

url, _ = applied(client=("127.0.0.1", 8000),
                 forwarded="host=standard.example;proto=https",
                 x_forwarded_host="legacy.example", x_forwarded_proto="http")
check("the standard header wins over the older one",
      url == "https://standard.example", url)

print("\nPorts, where a mistake breaks OAuth and nothing else")

url, _ = applied(client=("127.0.0.1", 9000), x_forwarded_proto="http",
                 x_forwarded_host="tasks.example.com", x_forwarded_port="8443")
check("a non-default port is put back on the address",
      url == "http://tasks.example.com:8443", url)

url, _ = applied(client=("127.0.0.1", 9000), x_forwarded_proto="https",
                 x_forwarded_host="tasks.example.com", x_forwarded_port="443")
check("the default port for the scheme stays implicit",
      url == "https://tasks.example.com", url)

url, _ = applied(client=("127.0.0.1", 9000), x_forwarded_proto="https",
                 x_forwarded_host="tasks.example.com:9443", x_forwarded_port="443")
check("a port already in the host is not doubled",
      url == "https://tasks.example.com:9443", url)

url, _ = applied(client=("127.0.0.1", 9000), x_forwarded_proto="http",
                 x_forwarded_host="[2001:db8::1]", x_forwarded_port="8080")
check("the colons in an IPv6 address are not mistaken for a port",
      url == "http://[2001:db8::1]:8080", url)

print("\nChained proxies")

url, client = applied(client=("127.0.0.1", 9000),
                      x_forwarded_proto="https, http",
                      x_forwarded_host="tasks.example.com, internal.lan",
                      x_forwarded_for="203.0.113.9, 10.0.0.2")
check("the browser's own values are the first in the list",
      url == "https://tasks.example.com" and client == ("203.0.113.9", 0),
      f"{url} {client}")

print("\nWhat may be believed")

# A genuinely routable address. The documentation ranges reserved for
# examples (203.0.113.x and friends) will not do here: Python classifies them
# as private, so using one would have tested nothing.
url, _ = applied(client=("8.8.8.8", 40000), host="192.168.1.50:8080",
                 x_forwarded_host="evil.example", x_forwarded_proto="https")
check("headers from the open internet are ignored, not obeyed",
      url == "http://192.168.1.50:8080", url)

url, _ = applied(trust="never", client=("127.0.0.1", 5000),
                 x_forwarded_host="tasks.example.com")
check("'never' ignores even a proxy on the same machine",
      url == "http://taskhub.local:8080", url)

url, _ = applied(trust="always", client=("8.8.8.8", 40000),
                 x_forwarded_host="tasks.example.com", x_forwarded_proto="https")
check("'always' is there for a proxy reached over a public address",
      url == "https://tasks.example.com", url)

url, _ = applied(client=("127.0.0.1", 5000), x_forwarded_proto="javascript")
check("a scheme that is not http or https is discarded",
      url == "http://taskhub.local:8080", url)

url, _ = applied(client=None, x_forwarded_host="tasks.example.com")
check("no peer address at all counts as local",
      url == "http://tasks.example.com", url)

print()
if _failures:
    print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
    sys.exit(1)
print("All forwarded-address tests passed.")
