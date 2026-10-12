// Anonymous intake client. When buildId is null (fresh GET /try with no
// draft yet), the first message creates a draft via POST /try/start.
// Subsequent messages post to /try/:buildId/message. Swaps in a signup
// banner when the server reports signupRequired or signupRequiredNext.
(function () {
  'use strict';

  var buildId = window.ANON_BUILD_ID; // null on fresh GET /try
  var csrfToken = window.ANON_CSRF_TOKEN;
  var turnCap = window.ANON_TURN_CAP || 3;

  var input = document.getElementById('message-input');
  var sendBtn = document.getElementById('send-btn');
  var messagesEl = document.getElementById('chat-messages');
  var banner = document.getElementById('anon-signup-banner');
  var inputArea = document.getElementById('anon-chat-input-area');
  var turnsUsedEl = document.getElementById('anon-turns-used');

  if (!csrfToken || !input || !sendBtn || !messagesEl) {
    return;
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function appendMessage(role, content) {
    // Harbor v2 (2026-04-27): row + bubble class names match the EJS-rendered
    // markup in views/intake-anon.ejs so dynamically-appended messages get
    // the same warm-paper / ink styling as the server-rendered ones.
    var wrap = document.createElement('div');
    wrap.className = 'harbor-chat-row harbor-chat-row--' + role;
    var bubble = document.createElement('div');
    bubble.className = 'harbor-chat-bubble harbor-chat-bubble--' + role;
    var pre = document.createElement('pre');
    pre.className = 'harbor-chat-text';
    // Render basic markdown: **bold** and newlines (mirrors intake.js)
    pre.innerHTML = escapeHtml(content)
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/\n/g, '<br>');
    bubble.appendChild(pre);
    wrap.appendChild(bubble);
    messagesEl.appendChild(wrap);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function showSignupGate() {
    if (banner) banner.classList.remove('hidden');
    if (inputArea) inputArea.classList.add('hidden');
  }

  // Rerender the spec preview panel from a fresh AnonSpecPreview payload.
  // Mirrors the server-side partial at views/partials/_anon-spec-preview.ejs.
  function renderSpecPreview(preview) {
    var panel = document.querySelector('[data-anon-spec-preview]');
    if (!panel || !preview) return;
    panel.classList.remove('hidden');

    function setText(field, value) {
      var el = panel.querySelector('[data-field="' + field + '"]');
      if (el) el.textContent = value || '';
    }
    setText('app_name', preview.app_name);
    setText('one_liner', preview.one_liner);
    setText('audience', preview.audience);

    var featuresEl = panel.querySelector('[data-field="key_features"]');
    if (featuresEl && Array.isArray(preview.key_features)) {
      featuresEl.innerHTML = '';
      preview.key_features.forEach(function (feat) {
        var li = document.createElement('li');
        li.className = 'anon-spec__feature';
        var svg = '<svg class="anon-spec__check" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>';
        var span = document.createElement('span');
        span.textContent = feat;
        li.innerHTML = svg;
        li.appendChild(span);
        featuresEl.appendChild(li);
      });
    }

    var pagesEl = panel.querySelector('[data-field="pages"]');
    if (pagesEl && Array.isArray(preview.pages)) {
      pagesEl.innerHTML = '';
      preview.pages.forEach(function (page) {
        var chip = document.createElement('span');
        chip.className = 'anon-spec__page-chip';
        chip.textContent = page;
        pagesEl.appendChild(chip);
      });
    }

    var emptyEl = panel.querySelector('[data-field="empty"]');
    if (emptyEl) emptyEl.remove();
  }

  function setTurnsUsed(n) {
    if (turnsUsedEl) turnsUsedEl.textContent = String(n);
  }

  function setPending(pending) {
    sendBtn.disabled = pending;
    input.disabled = pending;
    sendBtn.textContent = pending ? 'Thinking...' : 'Send';
  }

  // First message on a fresh page (no buildId): create the draft via
  // POST /try/start with the message as a seed. Server processes the
  // seed in the same request, so by the time we navigate to
  // /try/:buildId the conversation already has greeting + user msg +
  // AI reply.
  async function startDraft(text) {
    var res = await fetch('/try/start', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'x-csrf-token': csrfToken,
      },
      body: JSON.stringify({ message: text }),
    });

    if (!res.ok) {
      var errData = await res.json().catch(function () { return {}; });
      throw new Error((errData && errData.error) || 'Failed to start. Please try again.');
    }

    return await res.json();
  }

  async function sendMessage() {
    var raw = (input.value || '').trim();
    if (!raw) return;
    var text = raw.slice(0, 1000);

    appendMessage('user', text);
    input.value = '';
    setPending(true);

    try {
      // No buildId yet: first message creates the draft and seeds it.
      // Navigate to the full chat page so session + DB messages are in
      // sync. The server already processed our message in the same
      // round-trip.
      if (!buildId) {
        var startData = await startDraft(text);
        if (startData.buildId) {
          window.location.href = '/try/' + startData.buildId;
          return;
        }
        appendMessage('assistant', 'Something went wrong. Please refresh and try again.');
        return;
      }

      var res = await fetch('/try/' + encodeURIComponent(buildId) + '/message', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'x-csrf-token': csrfToken,
        },
        body: JSON.stringify({ message: text }),
      });

      if (res.status === 401) {
        window.location.href = '/try';
        return;
      }

      var data = await res.json().catch(function () {
        return {};
      });

      if (data && data.signupRequired) {
        showSignupGate();
        return;
      }

      if (!res.ok) {
        appendMessage(
          'assistant',
          (data && data.error) || 'Something went wrong. Please try again.'
        );
        return;
      }

      if (data.reply) {
        appendMessage('assistant', data.reply);
      }
      if (typeof data.turnCount === 'number') {
        setTurnsUsed(data.turnCount);
      }
      if (data.specPreview) {
        renderSpecPreview(data.specPreview);
      }
      if (data.signupRequiredNext || (typeof data.turnsRemaining === 'number' && data.turnsRemaining === 0)) {
        showSignupGate();
      }
    } catch (err) {
      appendMessage('assistant', err.message || 'Network error. Please try again.');
    } finally {
      setPending(false);
      input.focus();
    }
  }

  sendBtn.addEventListener('click', sendMessage);
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  // If the server rendered us already at the cap, lock the UI.
  if ((window.ANON_TURN_COUNT || 0) >= turnCap) {
    showSignupGate();
  }

  messagesEl.scrollTop = messagesEl.scrollHeight;
})();
