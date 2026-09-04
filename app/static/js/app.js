/* Task Hub — client-side behaviour.
 *
 * Deliberately small and dependency-free: no framework, no build step, no CDN.
 * Everything here is progressive enhancement over server-rendered HTML, so if
 * this file fails to load the application still works through normal form
 * submissions -- it just reloads the page more often.
 */

(function () {
  "use strict";

  /* --- Theme -------------------------------------------------------------
   * Applied to <html> as early as possible. The inline snippet in the page
   * head does the initial application to avoid a flash of the wrong theme;
   * this keeps it in step when the setting changes.
   */
  function applyTheme(theme) {
    var root = document.documentElement;
    if (theme === "light" || theme === "dark") {
      root.setAttribute("data-theme", theme);
    } else {
      root.removeAttribute("data-theme");
    }
    try {
      localStorage.setItem("taskhub-theme", theme);
    } catch (e) {
      /* Private browsing or blocked site data: the server-side setting still
         applies on the next page load, so there is nothing to recover from. */
    }
  }

  document.addEventListener("change", function (event) {
    var input = event.target.closest('[data-theme-choice]');
    if (input) applyTheme(input.value);
  });

  /* --- Flash messages ----------------------------------------------------
   * Success and info messages fade out on their own. Errors stay put, because
   * something the user needs to act on should not vanish while they read it.
   */
  /* How long a message stays. An error gets longer than a confirmation
     because it usually has to be read and understood, not just noticed. */
  function flashLife(node) {
    if (node.classList.contains("alert-error")) return 12000;
    if (node.classList.contains("alert-warning")) return 9000;
    return 5000;
  }

  function dismissFlash(node) {
    if (node.dataset.going === "1") return;
    node.dataset.going = "1";
    node.style.transition = "opacity .35s ease, transform .35s ease";
    node.style.opacity = "0";
    node.style.transform = "translateX(12px)";
    setTimeout(function () { node.remove(); }, 380);
  }

  /* Every message gets a close button and a timer. The timer pauses while the
     pointer or keyboard focus is on the message, so a long error cannot
     disappear out from under someone in the middle of reading it. */
  function armFlash(node) {
    if (node.dataset.armed === "1") return;
    node.dataset.armed = "1";

    if (!node.querySelector(".flash-close")) {
      var close = document.createElement("button");
      close.type = "button";
      close.className = "flash-close";
      close.setAttribute("aria-label", "Dismiss this message");
      close.title = "Dismiss";
      close.innerHTML = "&times;";
      close.addEventListener("click", function () { dismissFlash(node); });
      node.appendChild(close);
    }

    var timer = null;
    var start = function () {
      clearTimeout(timer);
      timer = setTimeout(function () { dismissFlash(node); }, flashLife(node));
    };
    var hold = function () { clearTimeout(timer); };

    node.addEventListener("mouseenter", hold);
    node.addEventListener("mouseleave", start);
    node.addEventListener("focusin", hold);
    node.addEventListener("focusout", start);
    start();
  }

  function initFlashes() {
    document.querySelectorAll(".flash-stack .alert").forEach(armFlash);
  }

  // Shared with the other blocks in this file: a second copy would drift, and
  // reaching across an IIFE boundary silently throws at the moment it is needed.
  window.taskhub = window.taskhub || {};

  function showToast(message, kind) {
    var stack = document.querySelector(".flash-stack");
    if (!stack) {
      stack = document.createElement("div");
      stack.className = "flash-stack";
      document.body.appendChild(stack);
    }
    var node = document.createElement("div");
    node.className = "alert alert-" + (kind || "info");
    var body = document.createElement("div");
    body.className = "grow";
    body.textContent = message;
    node.appendChild(body);
    stack.appendChild(node);
    armFlash(node);
  }

  window.taskhub.toast = showToast;

  /* --- Task completion ---------------------------------------------------
   * Toggling posts in the background and updates that row straight away, then
   * the page settles into its new shape a moment later: a finished task drops
   * into Completed at the bottom, and one re-opened from there returns to
   * whichever group its due date puts it in. A task that has changed status
   * belongs in a different section, and leaving it where it was reads as the
   * click not having worked.
   *
   * The placing is the server's. It already owns the grouping and ordering
   * rules, and working the position out here would be a second copy of them,
   * free to drift out of step with the first.
   *
   * The refresh is deferred and restarted on every toggle, which is what makes
   * this bearable: clearing six things in a row costs one refresh after the
   * last of them, not six that shuffle the list under the cursor mid-click.
   */
  var regroupTimer = null;

  function regroupSoon() {
    window.clearTimeout(regroupTimer);
    regroupTimer = window.setTimeout(function () {
      /* Never out from under someone's typing. An inline editor open, a modal
         open, or a focused field all mean the refresh would throw away work
         that was never saved -- so it is abandoned, and the row simply stays
         where it is until the next time the page loads. */
      var editing = document.querySelector(".task-list form .card:not([hidden])");
      var openEditor = Array.prototype.some.call(
        document.querySelectorAll('[id^="edit-"]'),
        function (el) { return !el.classList.contains("hidden"); }
      );
      var dialogOpen = !!document.querySelector("dialog[open]");
      var active = document.activeElement;
      var typing = active && /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName) &&
                   active.type !== "checkbox";
      if (editing || openEditor || dialogOpen || typing) return;
      window.location.reload();
    }, 900);
  }

  document.addEventListener("change", function (event) {
    var checkbox = event.target.closest(".task-check");
    if (!checkbox) return;

    var row = checkbox.closest(".task");
    var url = checkbox.getAttribute("data-toggle-url");
    if (!row || !url) return;

    var wanted = checkbox.checked;
    row.classList.add("is-busy");

    fetch(url, {
      method: "POST",
      headers: { Accept: "application/json" },
      credentials: "same-origin"
    })
      .then(function (response) {
        return response.json().then(function (body) {
          return { ok: response.ok, body: body };
        });
      })
      .then(function (result) {
        row.classList.remove("is-busy");
        if (!result.ok) {
          checkbox.checked = !wanted;
          showToast(result.body.error || "Could not update that task.", "error");
          return;
        }
        row.classList.toggle("is-done", result.body.completed);
        checkbox.checked = result.body.completed;

        /* Its section has changed either way round, so let the page resettle.
           Only on the tasks page: the calendar has no such grouping, and the
           inline editor would lose half-typed text to a refresh. */
        if (row.closest(".task-list")) {
          row.classList.add("is-leaving");
          regroupSoon();
        }
      })
      .catch(function () {
        row.classList.remove("is-busy");
        checkbox.checked = !wanted;
        window.clearTimeout(regroupTimer);
        showToast("Could not reach the server. Your change was not saved.", "error");
      });
  });

  /* --- Inline task editing ---------------------------------------------- */
  document.addEventListener("click", function (event) {
    var trigger = event.target.closest("[data-edit-toggle]");
    if (!trigger) return;
    event.preventDefault();
    var id = trigger.getAttribute("data-edit-toggle");
    var panel = document.getElementById(id);
    if (!panel) return;
    var hidden = panel.classList.toggle("hidden");
    if (!hidden) {
      var first = panel.querySelector("input, textarea, select");
      if (first) first.focus();
    }
  });

  /* --- Copy to clipboard -------------------------------------------------
   * Used for CalDAV URLs and OAuth redirect URIs, which are long, exact, and
   * miserable to retype into a phone or a cloud console.
   */
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-copy]");
    if (!button) return;
    event.preventDefault();

    var selector = button.getAttribute("data-copy");
    var source = document.querySelector(selector);
    if (!source) return;
    var text = source.value !== undefined ? source.value : source.textContent;

    var done = function () {
      var original = button.textContent;
      button.textContent = "Copied";
      setTimeout(function () { button.textContent = original; }, 1600);
    };

    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(source, done); });
    } else {
      // Clipboard API needs a secure context; plain HTTP on a LAN does not
      // have one, which is a normal way to run this application.
      fallbackCopy(source, done);
    }
  });

  function fallbackCopy(source, done) {
    if (source.select) {
      source.select();
      source.setSelectionRange(0, 99999);
      try {
        document.execCommand("copy");
        done();
        return;
      } catch (e) { /* fall through to the prompt below */ }
    }
    showToast("Select the text and copy it manually.", "info");
  }

  /* --- Destructive action confirmation ----------------------------------- */
  document.addEventListener("submit", function (event) {
    var form = event.target;
    var message = form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) {
      event.preventDefault();
      return;
    }
    // Stop double submission on slow networks: a second POST could create a
    // duplicate task or re-run a sync.
    var submit = form.querySelector('button[type="submit"]:not([data-allow-repeat])');
    if (submit && !form.hasAttribute("data-no-lock")) {
      setTimeout(function () {
        submit.disabled = true;
        submit.setAttribute("aria-disabled", "true");
      }, 0);
    }
  });

  /* --- Password confirmation --------------------------------------------
   * Caught in the browser so a mismatched pair does not cost a round trip and
   * a re-typed form.
   */
  document.addEventListener("submit", function (event) {
    var form = event.target;
    var pair = form.querySelectorAll("[data-password-pair]");
    if (pair.length !== 2) return;
    if (pair[0].value !== pair[1].value) {
      event.preventDefault();
      pair[1].setCustomValidity("The two passwords do not match.");
      pair[1].reportValidity();
      var submit = form.querySelector('button[type="submit"]');
      if (submit) { submit.disabled = false; submit.removeAttribute("aria-disabled"); }
    }
  });

  document.addEventListener("input", function (event) {
    if (event.target.hasAttribute && event.target.hasAttribute("data-password-pair")) {
      event.target.setCustomValidity("");
    }
  });

  /* --- Due time requires a due date -------------------------------------- */
  document.addEventListener("input", function (event) {
    var timeInput = event.target;
    if (!timeInput.matches || !timeInput.matches('[data-requires-date]')) return;
    var dateInput = document.querySelector(timeInput.getAttribute("data-requires-date"));
    if (!dateInput) return;
    if (timeInput.value && !dateInput.value) {
      var today = new Date();
      dateInput.value = today.toISOString().slice(0, 10);
    }
  });

  /* --- Mail provider picker ----------------------------------------------
   * Fills the server, port and security from the chosen provider. Typing the
   * server name by hand is where this goes wrong: "smtp.google.com" instead of
   * "smtp.gmail.com" fails with a connection error that points at the network
   * rather than at the one wrong word.
   */
  document.addEventListener("change", function (event) {
    var picker = event.target.closest("[data-mail-provider]");
    if (!picker) return;
    var option = picker.options[picker.selectedIndex];
    var host = option.getAttribute("data-host");
    if (!host) return;
    var set = function (id, value) {
      var field = document.getElementById(id);
      if (field && value) field.value = value;
    };
    set("smtp-host", host);
    set("smtp-port", option.getAttribute("data-port"));
    var security = document.getElementById("smtp-security");
    if (security) security.value = option.getAttribute("data-security");
    var note = document.getElementById("smtp-provider-note");
    var hint = option.getAttribute("data-username");
    if (note && hint) note.textContent = hint;
  });

  /* --- Day shortcuts for the daily summary -------------------------------
   * "Every day" and "Monday to Friday" are the two choices nearly everybody
   * wants, and ticking five boxes to get the second is a chore. The checkboxes
   * remain the truth -- these buttons only set them -- so the form still works
   * with scripting off.
   */
  document.addEventListener("click", function (event) {
    var shortcut = event.target.closest("[data-digest-days]");
    if (!shortcut) return;
    var wanted = shortcut.getAttribute("data-digest-days").split(",");
    var boxes = document.querySelectorAll('#digest-days input[type="checkbox"]');
    Array.prototype.forEach.call(boxes, function (box) {
      box.checked = wanted.indexOf(box.value) !== -1;
    });
  });

  /* --- Filter form auto-submit ------------------------------------------- */
  document.addEventListener("change", function (event) {
    var control = event.target.closest("[data-autosubmit]");
    if (control && control.form) control.form.requestSubmit();
  });

  /* --- Modal dialogs ------------------------------------------------------ */
  document.addEventListener("click", function (event) {
    var opener = event.target.closest("[data-open-modal]");
    if (opener) {
      event.preventDefault();
      var dialog = document.getElementById(opener.getAttribute("data-open-modal"));
      if (dialog && dialog.showModal) dialog.showModal();
      return;
    }
    var closer = event.target.closest("[data-close-modal]");
    if (closer) {
      event.preventDefault();
      var open = closer.closest("dialog");
      if (open) open.close();
    }
  });

  document.addEventListener("DOMContentLoaded", initFlashes);
})();

/* --- Live sync progress ---------------------------------------------------
 * Polls the status endpoint while a sync is running so the page reports what
 * is happening. A long first sync of a large calendar takes minutes, and
 * without this the interface is indistinguishable from a hang -- which is
 * exactly how it read before.
 */
(function () {
  "use strict";

  var banner = document.getElementById("sync-progress");
  if (!banner) return;

  var counts = banner.querySelector("[data-progress-counts]");
  var label = banner.querySelector("[data-progress-label]");
  var idleAfterDone = false;

  function render(state) {
    if (!state.run) return;
    var r = state.run;
    if (counts) {
      counts.innerHTML =
        "<span>Pulled <b>" + r.pulled + "</b></span>" +
        "<span>Written <b>" + r.pushed + "</b></span>" +
        "<span>Unchanged <b>" + r.skipped + "</b></span>" +
        (r.errors ? "<span>Problems <b>" + r.errors + "</b></span>" : "");
    }
    if (label) {
      label.textContent = state.running
        ? "Syncing… this can take a few minutes the first time."
        : "Last sync " + (r.outcome || "finished") + ".";
    }
  }

  // While a sync is in flight, check often enough to feel live. When idle, keep
  // checking slowly so a scheduled sync starting in the background still shows
  // up without the user having to reload the page.
  var BUSY_INTERVAL = 2000;
  var IDLE_INTERVAL = 20000;

  function poll() {
    fetch("/sync/status", {
      headers: { Accept: "application/json" },
      credentials: "same-origin"
    })
      .then(function (response) {
        if (!response.ok) throw new Error("status " + response.status);
        return response.json();
      })
      .then(function (state) {
        if (state.running) {
          render(state);
          banner.hidden = false;
          idleAfterDone = true;
          setTimeout(poll, BUSY_INTERVAL);
          return;
        }

        banner.hidden = true;

        if (idleAfterDone) {
          // It was running and has now finished: reload so the task and
          // calendar counts on the page reflect what just arrived.
          window.location.reload();
          return;
        }
        setTimeout(poll, IDLE_INTERVAL);
      })
      .catch(function () {
        // A failed check must not leave a spinner implying work is happening.
        banner.hidden = true;
        setTimeout(poll, IDLE_INTERVAL);
      });
  }

  poll();

  /* --- Sync now, without losing your place --------------------------------
   * Submitting the form navigated to the history page, which threw away
   * whatever you were half way through configuring. Posting it from here keeps
   * you on the page and lets the banner above report progress in place; the
   * Details link opens the history in its own tab if you want it.
   *
   * A plain form submit still works if this never runs, so nothing depends on
   * the script being there. */
  function toast(message, kind) {
    if (window.taskhub && window.taskhub.toast) {
      window.taskhub.toast(message, kind);
    }
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form || form.getAttribute("action") !== "/sync/run") return;
    event.preventDefault();

    var button = form.querySelector('button[type="submit"]');
    if (button) button.disabled = true;

    fetch("/sync/run", {
      method: "POST",
      headers: { Accept: "application/json", "X-Requested-With": "fetch" },
      credentials: "same-origin"
    })
      .then(function (response) { return response.json(); })
      .then(function (result) {
        toast(result.message, result.started ? "info" : "warning");
        if (result.started) {
          banner.hidden = false;
          idleAfterDone = true;
          poll();
        }
      })
      .catch(function () {
        toast("Could not start the sync. Is Task Hub still running?", "error");
      })
      .then(function () {
        if (button) button.disabled = false;
      });
  });

  /* --- Checkbox dropdowns -------------------------------------------------
     Keeps the summary showing how many boxes are ticked, and closes an open
     menu when the click lands elsewhere -- a <details> left open behind the
     one you just opened would otherwise cover the row beneath it. */

  function refreshCheckdrop(drop) {
    var label = drop.querySelector("[data-checkdrop-count]");
    if (!label) return;
    var boxes = drop.querySelectorAll('.checkdrop-item input[type="checkbox"]');
    var count = 0;
    boxes.forEach(function (box) { if (box.checked) count += 1; });
    if (count === 0) {
      label.textContent = label.getAttribute("data-empty-label");
      return;
    }
    var noun = label.getAttribute("data-noun") || "selected";
    label.textContent = count + " " + noun + (count === 1 ? "" : "s");
  }

  document.addEventListener("change", function (event) {
    var drop = event.target.closest(".checkdrop");
    if (drop) refreshCheckdrop(drop);
  });

  document.addEventListener("click", function (event) {
    document.querySelectorAll(".checkdrop[open]").forEach(function (drop) {
      if (!drop.contains(event.target)) drop.removeAttribute("open");
    });
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    document.querySelectorAll(".checkdrop[open]").forEach(function (drop) {
      drop.removeAttribute("open");
    });
  });


  /* --- Unsaved-change tracking --------------------------------------------
     Ticking a box changes nothing until the form is submitted, which is not
     obvious when the button has scrolled out of view. */

  document.addEventListener("change", function (event) {
    var form = event.target.form;
    if (!form) return;
    var bar = form.querySelector("[data-savebar]");
    if (!bar) return;
    bar.classList.add("is-dirty");
    var note = bar.querySelector("[data-savebar-note]");
    if (note) note.hidden = false;
    form.dataset.dirty = "1";
  });

  document.addEventListener("submit", function (event) {
    if (event.target && event.target.dataset) delete event.target.dataset.dirty;
  });

  window.addEventListener("beforeunload", function (event) {
    var dirty = document.querySelector('form[data-dirty="1"]');
    if (!dirty) return;
    event.preventDefault();
    event.returnValue = "";
  });


  /* --- Write-back implies reading back -----------------------------------
     Ticking a list as a write-back target almost always means "keep these two
     in step", but write and read are separate switches, and a target that is
     never read is a one-way street: complete the task there and nothing comes
     back. Rather than let that be discovered later, ticking a target also
     ticks that list's read box for the same collection. Untick it again for a
     deliberately one-way list -- nothing here forces it back on. */

  function reconcileTwoWay(form) {
    var changed = [];
    form.querySelectorAll('input[name="writeout"]:checked').forEach(function (box) {
      var parts = box.value.split(":");
      var sourceId = parts[0];
      var targetId = parts[1];
      if (!sourceId || !targetId || sourceId === targetId) return;

      form.querySelectorAll('input[name="read"]:checked').forEach(function (readBox) {
        var readParts = readBox.value.split(":");
        if (readParts[0] !== sourceId) return;
        var wanted = form.querySelector(
          'input[name="read"][value="' + targetId + ":" + readParts[1] + '"]'
        );
        if (wanted && !wanted.checked) {
          wanted.checked = true;
          changed.push(wanted);
          // A write-back target should report changes, not act as a second
          // place tasks are created -- otherwise the two lists become mirrors.
          var only = form.querySelector(
            'input[name="updatesonly"][value="' + targetId + '"]'
          );
          if (only && !only.checked) only.checked = true;
        }
      });
    });
    changed.forEach(function (box) {
      var drop = box.closest(".checkdrop");
      if (drop) refreshCheckdrop(drop);
    });
    return changed.length;
  }

  document.addEventListener("change", function (event) {
    var name = event.target.name;
    if (name !== "writeout" && name !== "read") return;
    var form = event.target.form;
    if (!form) return;
    if (reconcileTwoWay(form) > 0) {
      showToast(
        "That list is now also read for changes, so a task completed there " +
        "comes back. It will not become a source of new tasks unless you " +
        "clear \u201cChanges only\u201d on its own row.",
        "info"
      );
    }
  });

})();

/* --- Remote access status --------------------------------------------------
 * The tunnel takes a few seconds to register with Cloudflare, and a wrong
 * token fails silently from the user's point of view. Polling turns both into
 * something visible instead of a page that just sits there.
 */
(function () {
  "use strict";

  var pill = document.getElementById("tunnel-pill");
  if (!pill) return;

  var summary = document.getElementById("tunnel-summary");
  var logBox = document.getElementById("tunnel-log");
  var settled = 0;

  function poll() {
    fetch("/settings/tunnel/status", {
      headers: { Accept: "application/json" },
      credentials: "same-origin"
    })
      .then(function (r) { return r.json(); })
      .then(function (s) {
        pill.textContent = s.healthy ? "Connected" : (s.enabled ? "Connecting…" : "Off");
        pill.className = "pill" + (s.healthy ? " pill-success" : (s.enabled ? " pill-warning" : ""));
        if (summary) summary.textContent = s.healthy ? "" : (s.error || "");
        if (logBox && s.log && s.log.length) logBox.textContent = s.log.join("\n");

        // Keep watching while it is still settling, then back off so an idle
        // settings page is not polling forever.
        if (s.enabled && !s.healthy) { settled = 0; setTimeout(poll, 2000); }
        else if (settled < 3) { settled += 1; setTimeout(poll, 4000); }
      })
      .catch(function () { setTimeout(poll, 8000); });
  }

  poll();
})();

/* --- Calendar --------------------------------------------------------------
 * Clicking an event opens the editor pre-filled from data on the button, so
 * editing costs no round trip. Ticking a task off updates that one entry in
 * place, because a reload would move everything under the cursor.
 */
(function () {
  "use strict";

  /* Opening the editor: copy the clicked event's details into the form. */
  document.addEventListener("click", function (event) {
    var el = event.target.closest("[data-event]");
    if (!el) return;

    var data;
    try { data = JSON.parse(el.getAttribute("data-event")); } catch (e) { return; }

    var form = document.getElementById("edit-event-form");
    if (!form) return;

    form.action = "/calendar/" + encodeURIComponent(data.collection) +
                  "/" + encodeURIComponent(data.uid) + "/update";

    var del = document.getElementById("edit-delete-btn");
    if (del) {
      del.formAction = "/calendar/" + encodeURIComponent(data.collection) +
                       "/" + encodeURIComponent(data.uid) + "/delete";
    }

    var set = function (id, value) {
      var field = document.getElementById(id);
      if (field) field.value = value || "";
    };
    set("ed-title", data.title);
    set("ed-notes", data.notes);
    set("ed-location", data.location);
    set("ed-start-date", data.start_date);
    set("ed-start-time", data.start_time);
    set("ed-end-date", data.end_date);
    set("ed-end-time", data.end_time);

    var allday = document.getElementById("ed-allday");
    if (allday) {
      allday.checked = !!data.all_day;
      applyAllDay(allday);
    }

    var note = document.getElementById("edit-recurring-note");
    if (note) note.classList.toggle("hidden", !data.recurring);
  });

  /* All-day hides the time fields rather than disabling them: a disabled input
     is not submitted, and an empty time already means "all day" to the server. */
  function applyAllDay(box) {
    var target = document.querySelector(box.getAttribute("data-allday-toggle") || "");
    var row = box.closest("form");
    if (!row) return;
    var times = row.querySelectorAll('input[type="time"]');
    for (var i = 0; i < times.length; i++) {
      times[i].closest(".field").style.display = box.checked ? "none" : "";
      if (box.checked) times[i].value = "";
    }
  }

  document.addEventListener("change", function (event) {
    var box = event.target.closest("[data-allday-toggle]");
    if (box) applyAllDay(box);
  });

  /* Deleting from inside the edit dialog. */
  document.addEventListener("click", function (event) {
    var btn = event.target.closest("[data-confirm-delete]");
    if (!btn) return;
    var what = btn.getAttribute("data-confirm-delete") || "item";
    if (!window.confirm("Delete this " + what + " permanently? This cannot be undone.")) {
      event.preventDefault();
    }
  });

  /* Opening the task editor: same idea as the event editor above. */
  document.addEventListener("click", function (event) {
    var el = event.target.closest("[data-task]");
    if (!el) return;

    var data;
    try { data = JSON.parse(el.getAttribute("data-task")); } catch (e) { return; }

    var form = document.getElementById("edit-task-form");
    if (!form) return;

    form.action = "/calendar/task/" + encodeURIComponent(data.collection) +
                  "/" + encodeURIComponent(data.uid) + "/update";
    var del = document.getElementById("edit-task-delete");
    if (del) {
      del.formAction = "/calendar/task/" + encodeURIComponent(data.collection) +
                       "/" + encodeURIComponent(data.uid) + "/delete";
    }

    var set = function (id, value) {
      var field = document.getElementById(id);
      if (field) field.value = value === undefined || value === null ? "" : value;
    };
    set("et-title", data.title);
    set("et-notes", data.notes);
    set("et-due-date", data.due_date);
    set("et-due-time", data.due_time);

    /* Priorities are stored 1-9 but offered as three bands, so snap to the
       band the stored number falls in rather than showing nothing. */
    var band = "0";
    var p = Number(data.priority) || 0;
    if (p >= 1 && p <= 3) band = "1";
    else if (p >= 4 && p <= 6) band = "5";
    else if (p >= 7) band = "9";
    set("et-priority", band);
  });

  /* Clicking an empty hour, or an agenda day, starts a new event there. */
  document.addEventListener("click", function (event) {
    /* A click that landed on something already in the calendar is that
       thing's click, not an invitation to create a new one. */
    if (event.target.closest(".cal-item, .cal-more, a")) return;

    var slot = event.target.closest("[data-new-at]");
    if (!slot) return;

    var at = slot.getAttribute("data-new-at") || "";
    var parts = at.split("T");
    if (parts.length !== 2) return;

    var dialog = document.getElementById("new-event-modal");
    if (!dialog || !dialog.showModal) return;

    var set = function (id, value) {
      var field = document.getElementById(id);
      if (field) field.value = value;
    };
    set("ev-start-date", parts[0]);
    set("ev-end-date", parts[0]);
    set("ev-start-time", parts[1]);
    set("ev-end-time", plusAnHour(parts[1]));

    var allday = document.getElementById("new-event-modal").querySelector('[name="all_day"]');
    if (allday && allday.checked) { allday.checked = false; applyAllDay(allday); }

    dialog.showModal();
    var title = document.getElementById("ev-title");
    if (title) { title.value = ""; title.focus(); }
  });

  function plusAnHour(hhmm) {
    var bits = (hhmm || "").split(":");
    if (bits.length < 2) return hhmm;
    var h = (parseInt(bits[0], 10) + 1) % 24;
    return (h < 10 ? "0" : "") + h + ":" + bits[1];
  }

  /* The end time trails the start by an hour, until it is set by hand. */
  document.addEventListener("input", function (event) {
    var end = event.target.closest("#ev-end-time");
    if (end) { end.dataset.touched = "1"; return; }

    var start = event.target.closest("[data-autoend]");
    if (!start) return;
    var target = document.querySelector(start.getAttribute("data-autoend"));
    if (target && target.dataset.touched !== "1") target.value = plusAnHour(start.value);
  });

  /* A month cell is a way into its day. */
  document.addEventListener("click", function (event) {
    if (event.target.closest(".cal-item, .cal-more, a, button")) return;
    var cell = event.target.closest("[data-goto]");
    if (cell) location.href = cell.getAttribute("data-goto");
  });

  /* The browser stops an end before a start before the server has to.
     Both forms use the same field-name pairs, so one rule covers them. */
  function syncEventBounds(form) {
    if (!form) return;
    var sd = form.querySelector('[name="start_date"]');
    var ed = form.querySelector('[name="end_date"]');
    var st = form.querySelector('[name="start_time"]');
    var et = form.querySelector('[name="end_time"]');
    if (!sd || !ed) return;

    ed.min = sd.value || "";
    if (ed.value && sd.value && ed.value < sd.value) ed.value = sd.value;

    if (st && et) {
      /* A time floor only makes sense while both ends are on the same day. */
      var sameDay = sd.value && ed.value && sd.value === ed.value;
      et.min = sameDay ? (st.value || "") : "";
      if (sameDay && st.value && et.value && et.value < st.value) {
        et.value = plusAnHour(st.value);
      }
    }
  }

  document.addEventListener("input", function (event) {
    var field = event.target.closest(
      '[name="start_date"], [name="end_date"], [name="start_time"], [name="end_time"]'
    );
    if (field) syncEventBounds(field.form);
  });

  document.addEventListener("click", function (event) {
    var opener = event.target.closest('[data-open-modal]');
    if (!opener) return;
    var dialog = document.getElementById(opener.getAttribute("data-open-modal"));
    if (dialog) setTimeout(function () { syncEventBounds(dialog.querySelector("form")); }, 0);
  });

  /* Quick add reveals its extra fields once the task has a name. */
  document.addEventListener("input", function (event) {
    var box = event.target.closest(".quickadd-title");
    if (!box) return;
    var form = box.closest(".quickadd");
    if (form) form.classList.toggle("is-open", box.value.trim().length > 0);
  });

  /* Ticking a task off from the calendar. */
  document.addEventListener("click", function (event) {
    var item = event.target.closest("[data-toggle-task]");
    if (!item) return;
    var url = item.getAttribute("data-toggle-task");
    /* The tick is a small button inside the chip; the struck-through styling
       belongs on the chip, so find it before changing anything. */
    var chip = item.closest(".cal-item") || item.closest(".tp-task") || item;
    chip.style.opacity = "0.4";

    fetch(url, { method: "POST", headers: { Accept: "application/json" },
                 credentials: "same-origin" })
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        chip.style.opacity = "";
        if (!res.ok) { showToast(res.body.error || "Could not update that task.", "error"); return; }
        chip.classList.toggle("is-done", res.body.completed);
      })
      .catch(function () {
        chip.style.opacity = "";
        showToast("Could not reach the server. Your change was not saved.", "error");
      });
  });
})();
