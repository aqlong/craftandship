/* global window, document */
(function () {
  'use strict';

  const buildId = window.STATUS_BUILD_ID;
  let currentStatus = window.STATUS_CURRENT;
  // Track the review_status server-rendered into the page so
  // that when the admin flips null/pending -> changes_requested or approved,
  // we can full-reload and render the new branch of status.ejs.
  let lastSeenReviewStatus = (typeof window.STATUS_REVIEW_STATUS !== 'undefined')
    ? window.STATUS_REVIEW_STATUS
    : null;

  const TERMINAL_STATES = ['live', 'failed', 'preview'];
  const POLL_INTERVAL_MS = 5000;
  const TERMINAL_POLL_INTERVAL_MS = 15000; // Slower poll after terminal state

  // Guard: when a form navigates away (e.g. approve), suppress poll reloads
  // to prevent a blank-screen race between the redirect and window.location.reload().
  var isNavigatingAway = false;
  document.querySelectorAll('form[method="POST"], form[method="post"]').forEach(function (f) {
    f.addEventListener('submit', function () { isNavigatingAway = true; });
  });

  // Always set up polling elements even for terminal states,
  // so we can detect requeues/rebuilds and auto-reload.

  const statusBadge = document.getElementById('status-badge');
  const progressSteps = document.getElementById('progress-steps');
  const queueNote = document.getElementById('queue-note');
  const buildLogsPanel = document.getElementById('build-logs');
  const buildLogsContainer = document.getElementById('build-logs-container');
  const estimateNote = document.getElementById('estimate-note');
  let lastLogId = 0;
  let lastLogStage = '';
  let isTerminal = TERMINAL_STATES.includes(currentStatus);

  const STEP_ORDER = [
    'intake',
    'payment_pending',
    'queued',
    'building',
    'testing',
    'deploying',
    'preview',
    'live',
  ];

  const STATUS_MESSAGES = {
    intake: 'Describing your website',
    payment_pending: 'Awaiting payment',
    queued: 'Waiting in queue',
    building: 'Building your website',
    testing: 'Running automated tests',
    deploying: 'Deploying to production',
    preview: 'Ready for review',
    live: 'Your website is live',
    failed: 'Build failed',
  };

  function updateProgressSteps(status) {
    if (!progressSteps) return;

    // For failed status, show failure at the last known stage (from logs)
    var currentIdx = STEP_ORDER.indexOf(status);
    if (status === 'failed' && currentIdx === -1) {
      currentIdx = Math.max(STEP_ORDER.indexOf(lastLogStage), STEP_ORDER.indexOf('queued'));
    }
    const stepEls = progressSteps.querySelectorAll('[data-step]');

    stepEls.forEach(function (stepEl, idx) {
      const dot = stepEl.querySelector('.progress-step-dot');
      const label = stepEl.querySelector('.progress-step-label');
      var isDone = idx < currentIdx || (idx === currentIdx && (status === 'live' || status === 'preview'));
      var isCurrent = idx === currentIdx && !isDone;

      if (dot) {
        // Reset all possible state classes
        dot.classList.remove('bg-brand', 'border-brand', 'text-black', 'border-border', 'text-subtle', 'bg-bg', 'text-brand', 'shadow-glow');
        if (isDone) {
          dot.classList.add('bg-brand', 'border-brand', 'text-black');
          dot.innerHTML = '&#10003;';
        } else if (isCurrent) {
          dot.classList.add('border-brand', 'text-brand', 'shadow-glow');
          if (status === 'failed') {
            dot.innerHTML = '&#10007;';
          } else {
            dot.innerHTML = '<span class="progress-spinner"></span>';
          }
        } else {
          dot.classList.add('border-border', 'text-subtle', 'bg-bg');
          dot.textContent = String(idx + 1);
        }
      }

      if (label) {
        label.classList.remove('text-brand', 'text-gray-100', 'font-semibold', 'text-subtle');
        if (isDone) {
          label.classList.add('text-brand');
        } else if (isCurrent) {
          label.classList.add('text-gray-100', 'font-semibold');
        } else {
          label.classList.add('text-subtle');
        }
      }
    });

    const connectors = progressSteps.querySelectorAll('.progress-connector');
    connectors.forEach(function (connector, idx) {
      connector.classList.remove('bg-brand', 'bg-border');
      if (idx < currentIdx) {
        connector.classList.add('bg-brand');
      } else {
        connector.classList.add('bg-border');
      }
    });
  }

  function updateStatusBadge(status, message) {
    if (!statusBadge) return;

    statusBadge.className = 'status-badge status-badge-' + status;
    statusBadge.textContent = message || STATUS_MESSAGES[status] || status;
  }

  // Escapes the five characters that matter in HTML text AND in a quoted
  // attribute value. (An escaper built on a text node's innerHTML leaves
  // quotes alone, which is not enough inside href="..." or value="...".)
  function escapeHtml(text) {
    return String(text == null ? '' : text)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function formatLogTime(isoString) {
    // Browser-local time with short TZ abbreviation (CDT, PST, etc.) -
    // matches the CAS convention enforced by the layout.ejs data-ts
    // renderer. Unambiguous when customer, support, and engineering read
    // the same log stream across zones.
    var d = new Date(isoString);
    return d.toLocaleTimeString('en-US', {
      hour: 'numeric',
      minute: '2-digit',
      second: '2-digit',
      hour12: true,
      timeZoneName: 'short',
    });
  }

  var STAGE_LABELS = {
    queued: 'Queued',
    building: 'Building',
    testing: 'Testing',
    deploying: 'Deploying',
    preview: 'Preview',
  };

  // Friendly stage label copy shown in the animated build card
  var STAGE_ACTIVITY_LABELS = {
    queued:    'Your website is next in line.',
    building:  'Building your website...',
    testing:   'Running automated tests...',
    deploying: 'Deploying to a live URL...',
  };

  // v2 testing-stage build_logs include per-check lint detail (engine
  // jargon, file counts, framework-internal vocabulary) that's useful in
  // the admin / log-tail debug surface but reads as gibberish in the
  // customer-facing live activity card. Suppress those messages from
  // build-activity-latest while still appending them to the hidden
  // build-logs-container for diagnosis. Stage transitions still fire on
  // log.stage and remain unaffected.
  var INTERNAL_DEBUG_MESSAGE_TOKENS = [
    'v2 lint',
    'v2 build-output lint',
    'Skipped v1 Claude-driven',
    'v2 compose has no LLM variance',
    '(spec wants',
    'v1→v2',
    'translation_warnings',
    'translation warnings (v1',
    'translation emitted',
    'Manifest reports',
    'Required files present',
    'heading-capable widgets',
    'italic-accent',
    'page views composed from widgets',
    'page view(s) not composed',
    'CAS utility classes',
    'Inter/Roboto',
    '100vh',
    'Powered-By Craft',
    'Powered-By attribution',
    'compose-manifest',
    'LINT FAIL',
    'Seed for ',
  ];

  // The build's warning lines are review material, not progress: what the
  // engine wrote about the site ("warning: <reason>: <detail>") and text the
  // spec carried ("spec-warning: ..."), the latter being the customer's own
  // words copied back. Neither is shown on the activity card. The worker
  // writes them with leading spaces, one to an entry or many joined by
  // newlines, and always with a warning line first, so the start of the
  // message decides.
  var BUILD_WARNING_LINE = /^\s*(?:spec-)?warning: /;

  function isInternalDebugMessage(msg) {
    if (!msg || typeof msg !== 'string') return false;
    if (BUILD_WARNING_LINE.test(msg)) return true;
    for (var i = 0; i < INTERNAL_DEBUG_MESSAGE_TOKENS.length; i++) {
      if (msg.indexOf(INTERNAL_DEBUG_MESSAGE_TOKENS[i]) !== -1) return true;
    }
    return false;
  }

  async function fetchLogs() {
    if (!buildLogsContainer) return;
    try {
      var logController = new AbortController();
      var logTimeout = setTimeout(function () { logController.abort(); }, 10000);
      var resp = await fetch('/api/builds/' + buildId + '/logs?after=' + lastLogId, { signal: logController.signal, cache: 'no-store' });
      clearTimeout(logTimeout);
      if (!resp.ok) return;
      var body = await resp.json();
      if (!body.logs || body.logs.length === 0) return;

      if (buildLogsPanel) buildLogsPanel.style.display = '';

      var activityStage = document.getElementById('build-activity-stage');
      var activityLatest = document.getElementById('build-activity-latest');

      body.logs.forEach(function (log) {
        if (log.stage !== lastLogStage) {
          // Keep the hidden terminal log for debugging compatibility
          var header = document.createElement('div');
          header.className = 'build-log-stage-header';
          header.textContent = STAGE_LABELS[log.stage] || log.stage;
          buildLogsContainer.appendChild(header);
          lastLogStage = log.stage;

          // Update the friendly stage label
          if (activityStage) {
            activityStage.textContent = STAGE_ACTIVITY_LABELS[log.stage] || 'Working on your website...';
          }

          // Advance the progress circles immediately when logs reveal a new stage,
          // without waiting for the next status poll to catch up.
          // Do NOT advance currentStatus to terminal states here: that lets
          // poll() detect the preview/live transition and trigger the page reload.
          var logStageIdx = STEP_ORDER.indexOf(log.stage);
          if (logStageIdx > STEP_ORDER.indexOf(currentStatus)) {
            updateProgressSteps(log.stage);
            updateStatusBadge(log.stage);
            if (!TERMINAL_STATES.includes(log.stage)) {
              currentStatus = log.stage;
            }
          }
        }
        // Keep appending to hidden terminal container for log ID tracking
        var entry = document.createElement('div');
        entry.className = 'build-log-entry';
        entry.innerHTML = '<span class="log-time">' + escapeHtml(formatLogTime(log.created_at)) + '</span>' + escapeHtml(log.message);
        buildLogsContainer.appendChild(entry);
        lastLogId = log.id;

        // Update the latest activity line in the friendly card (most recent message).
        // Skip internal-debug detail (v2 lint per-check output, translator
        // warnings, file/manifest counts) so customers don't see engine jargon
        // flash by; the hidden build-logs-container above still has every line
        // for debugging.
        if (activityLatest && log.message && !isInternalDebugMessage(log.message)) {
          activityLatest.textContent = log.message;
        }
      });
    } catch (_e) {
      // silent
    }
  }

  async function poll() {
    try {
      var controller = new AbortController();
      var timeoutId = setTimeout(function () { controller.abort(); }, 10000);
      const response = await fetch('/api/builds/' + buildId, { signal: controller.signal, cache: 'no-store' });
      clearTimeout(timeoutId);

      if (!response.ok) {
        schedulePoll();
        return;
      }

      // Fetch logs in parallel with JSON parsing (don't block status processing)
      var logPromise = !isTerminal ? fetchLogs() : Promise.resolve();

      const data = await response.json();

      // Wait for logs to finish (they may advance progress circles)
      await logPromise;
      const newStatus = data.status;

      // Detect status regression (e.g. failed -> queued from requeue/rebuild).
      // Reload the page so the user gets a fresh server-rendered view.
      var newIdx = STEP_ORDER.indexOf(newStatus);
      var curIdx = STEP_ORDER.indexOf(currentStatus);
      if (isTerminal && !TERMINAL_STATES.includes(newStatus)) {
        // Build was requeued or rebuilt; reload to get fresh page
        if (!isNavigatingAway) window.location.reload();
        return;
      }

      // Detect a review_status transition
      // (null/pending -> changes_requested/approved). Full-reload so the
      // preview branch of status.ejs picks up the new state, review notes,
      // and the "Reviewed" badge. Treat null and 'pending' as
      // equivalent for transition purposes (both render the waiting panel).
      var newReviewStatus = (typeof data.review_status !== 'undefined')
        ? data.review_status
        : null;
      function normalizeReview(rs) {
        return (rs === null || rs === 'pending') ? 'pending' : rs;
      }
      if (normalizeReview(newReviewStatus) !== normalizeReview(lastSeenReviewStatus)) {
        lastSeenReviewStatus = newReviewStatus;
        if (!isNavigatingAway) {
          window.location.reload();
          return;
        }
      }

      // Only advance status, never regress (logs may have already moved currentStatus forward)
      // Terminal states (failed, preview, live) always override since they're not in STEP_ORDER
      var isNewTerminal = TERMINAL_STATES.includes(newStatus);
      if (newStatus !== currentStatus && (isNewTerminal || newIdx >= curIdx)) {
        currentStatus = newStatus;

        updateProgressSteps(newStatus);
        updateStatusBadge(newStatus, data.progress_message);

        if (queueNote) {
          queueNote.style.display = newStatus === 'queued' ? '' : 'none';
        }

        if (estimateNote) {
          var activeStatuses = ['queued', 'building', 'testing', 'deploying'];
          estimateNote.style.display = activeStatuses.includes(newStatus) ? '' : 'none';
        }

        if (data.queue_position !== null && data.queue_position !== undefined && queueNote) {
          queueNote.textContent = 'Position in queue: ' + data.queue_position;
        }

        // Terminal states: reload to get full server-rendered page with all
        // features (revision modal, download source, credentials, etc.)
        if (newStatus === 'preview' || newStatus === 'live' || newStatus === 'failed') {
          if (!isNavigatingAway) window.location.reload();
          return;
        }
      }

      schedulePoll();
    } catch (_err) {
      schedulePoll();
    }
  }

  function schedulePoll() {
    var interval = isTerminal ? TERMINAL_POLL_INTERVAL_MS : POLL_INTERVAL_MS;
    setTimeout(poll, interval);
  }

  // Safety net: if the page has been open 30+ min without reaching a terminal
  // state, force reload. Catches edge cases where polling silently dies (e.g.
  // network blip, fetch hangs, tab throttling).
  if (!isTerminal) {
    setTimeout(function () {
      if (!isTerminal) {
        window.location.reload();
      }
    }, 30 * 60 * 1000);
  }

  // When tab comes back to foreground, poll immediately.
  // Browsers throttle setTimeout to ~1min+ in background tabs, so the user
  // may return to a stale status. This forces an instant refresh.
  var pollTimer = null;
  var _origSchedulePoll = schedulePoll;
  schedulePoll = function () {
    var interval = isTerminal ? TERMINAL_POLL_INTERVAL_MS : POLL_INTERVAL_MS;
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = setTimeout(poll, interval);
  };

  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) {
      // Tab became visible: poll immediately
      if (pollTimer) clearTimeout(pollTimer);
      pollTimer = setTimeout(poll, 200);
    }
  });

  // Start polling, even for terminal states (to detect requeues/rebuilds)
  schedulePoll();
})();
