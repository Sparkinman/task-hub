/* Task Hub's service worker.
 *
 * Deliberately cautious about what it keeps. Everything behind the login is
 * somebody's tasks, calendar and notes, and a cache is a copy that outlives
 * the session -- so pages are never stored, and neither is anything under
 * /notes/, which serves PDFs of a person's handwritten notebooks.
 *
 * What is cached is the shell: the stylesheet, the scripts and the icons.
 * Those are the parts that make a launched app feel instant instead of blank,
 * they contain nothing private, and they are already versioned by the ?v=
 * stamp on their URLs, so a stale one cannot survive an upgrade.
 */

const SHELL = "taskhub-shell-v1";

/* Fetched on install so the app opens offline with its own styling rather than
 * an unstyled error. Nothing here is user data. */
const SHELL_URLS = [
  "/static/css/app.css",
  "/static/img/favicon.svg",
  "/static/img/icon-192.png",
  "/offline",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL).then((cache) =>
      /* Individually, so one missing file does not fail the whole install and
       * leave the app with no service worker at all. */
      Promise.all(SHELL_URLS.map((url) => cache.add(url).catch(() => null)))
    ).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(
        names.filter((n) => n !== SHELL).map((n) => caches.delete(n))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  /* Never touched: the CalDAV endpoint speaks to calendar clients, and the
   * notes routes serve private PDFs that have no business in a cache. */
  if (url.pathname.startsWith("/radicale") || url.pathname.startsWith("/notes")) {
    return;
  }

  /* Static assets: cache first. They carry a version stamp, so a cached copy
   * is only ever returned for the exact version that asked for it. */
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.match(request).then((hit) =>
        hit || fetch(request).then((response) => {
          if (response.ok) {
            const copy = response.clone();
            caches.open(SHELL).then((cache) => cache.put(request, copy));
          }
          return response;
        })
      )
    );
    return;
  }

  /* Pages: always from the network, never stored. If the network is gone,
   * a short page that says so, rather than the browser's own error. */
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(() =>
        caches.match("/offline").then((hit) => hit || new Response(
          "Task Hub is not reachable right now.",
          { status: 503, headers: { "Content-Type": "text/plain" } }
        ))
      )
    );
  }
});

/* --- Notifications ---------------------------------------------------------
 * Two events. A push arrives encrypted and is shown; a tap on it brings the
 * relevant page forward rather than opening yet another copy of the app, which
 * is the difference between this feeling like an app and feeling like a series
 * of browser tabs.
 */

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    /* A push with no payload, or one this version does not understand. Better a
     * plain notification than none: something wanted attention. */
    data = {};
  }
  const title = data.title || "Task Hub";
  event.waitUntil(
    self.registration.showNotification(title, {
      body: data.body || "",
      icon: "/static/img/icon-192.png",
      badge: "/static/img/icon-192.png",
      /* Same tag replaces rather than stacks, so a sync failing on every pass
       * leaves one notification instead of a screenful. */
      tag: data.tag || "taskhub",
      renotify: false,
      data: { url: data.url || "/" },
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true })
      .then((windows) => {
        for (const client of windows) {
          if ("focus" in client) {
            /* Reuse the window that is already open. */
            return client.navigate ? client.navigate(target).then((c) => c.focus())
                                   : client.focus();
          }
        }
        return self.clients.openWindow(target);
      })
  );
});

