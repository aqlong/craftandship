/* Support Chat Widget: Craft & Ship (impeccable design) */
(function () {
  'use strict';

  // Do not render on intake pages
  if (window.location.pathname.startsWith('/intake/')) return;

  var STORAGE_KEY = 'cs_support_chat';
  var MAX_MESSAGE_LENGTH = 1000;

  function loadHistory() {
    try {
      var raw = sessionStorage.getItem(STORAGE_KEY);
      return raw ? JSON.parse(raw) : [];
    } catch (_e) { return []; }
  }

  function saveHistory(messages) {
    try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify(messages)); } catch (_e) {}
  }

  var messages = loadHistory();

  // ── Styles ────────────────────────────────────────────────────────────────
  var style = document.createElement('style');
  style.textContent = `
    .sc-bubble {
      position: fixed;
      bottom: 24px;
      right: 24px;
      width: 52px;
      height: 52px;
      border-radius: 50%;
      background: #C68A3A;
      border: none;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      box-shadow: 0 4px 20px rgba(0,0,0,0.4), 0 0 0 1px rgba(1,135,160,0.3);
      z-index: 9999;
      transition: transform 0.15s ease, box-shadow 0.15s ease;
    }
    .sc-bubble:hover {
      transform: scale(1.06);
      box-shadow: 0 6px 24px rgba(0,0,0,0.5), 0 0 0 1px rgba(1,135,160,0.4);
    }
    .sc-bubble svg { width: 22px; height: 22px; fill: #000; }

    .sc-window {
      position: fixed;
      bottom: 88px;
      right: 24px;
      width: 360px;
      max-width: calc(100vw - 48px);
      height: 480px;
      max-height: calc(100vh - 120px);
      background: #141416;
      border: 1px solid rgba(255,255,255,0.07);
      border-radius: 16px;
      display: flex;
      flex-direction: column;
      z-index: 9999;
      box-shadow: 0 16px 48px rgba(0,0,0,0.5), 0 0 0 1px rgba(255,255,255,0.04);
      overflow: hidden;
      font-family: Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    }
    .sc-window.sc-hidden { display: none; }

    .sc-header {
      display: flex;
      align-items: center;
      gap: 0.625rem;
      padding: 0.875rem 1.125rem;
      background: #0C0C0E;
      border-bottom: 1px solid rgba(255,255,255,0.06);
      flex-shrink: 0;
    }
    .sc-header-dot {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: #C68A3A;
      animation: sc-pulse 3s ease-in-out infinite;
      flex-shrink: 0;
    }
    @keyframes sc-pulse {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.5; }
    }
    .sc-header-title {
      font-size: 0.8125rem;
      font-weight: 600;
      color: #F2EEE5;
      flex: 1;
    }
    .sc-header-sub {
      font-size: 0.7rem;
      color: #545460;
    }
    .sc-header-close {
      background: none;
      border: none;
      color: #545460;
      cursor: pointer;
      font-size: 1.125rem;
      line-height: 1;
      padding: 0 2px;
      transition: color 0.1s;
    }
    .sc-header-close:hover { color: #F2EEE5; }

    .sc-messages {
      flex: 1;
      overflow-y: auto;
      padding: 1rem;
      display: flex;
      flex-direction: column;
      gap: 0.625rem;
      scrollbar-width: thin;
      scrollbar-color: rgba(255,255,255,0.08) transparent;
    }
    .sc-messages::-webkit-scrollbar { width: 4px; }
    .sc-messages::-webkit-scrollbar-track { background: transparent; }
    .sc-messages::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.08); border-radius: 2px; }

    .sc-msg {
      max-width: 84%;
      padding: 0.625rem 0.875rem;
      border-radius: 12px;
      font-size: 0.8125rem;
      line-height: 1.55;
      word-wrap: break-word;
      white-space: pre-wrap;
    }
    .sc-msg-assistant {
      background: #1A1A1E;
      border: 1px solid rgba(255,255,255,0.06);
      color: #86868E;
      align-self: flex-start;
      border-bottom-left-radius: 3px;
    }
    .sc-msg-user {
      background: #C68A3A;
      color: #000;
      font-weight: 500;
      align-self: flex-end;
      border-bottom-right-radius: 3px;
    }

    .sc-typing {
      align-self: flex-start;
      padding: 0.625rem 0.875rem;
      background: #1A1A1E;
      border: 1px solid rgba(255,255,255,0.06);
      border-radius: 12px;
      border-bottom-left-radius: 3px;
      display: flex;
      gap: 4px;
      align-items: center;
    }
    .sc-typing span {
      width: 5px;
      height: 5px;
      background: #545460;
      border-radius: 50%;
      animation: sc-bounce 1.2s infinite ease-in-out;
    }
    .sc-typing span:nth-child(2) { animation-delay: 0.2s; }
    .sc-typing span:nth-child(3) { animation-delay: 0.4s; }
    @keyframes sc-bounce {
      0%, 80%, 100% { transform: translateY(0); }
      40% { transform: translateY(-5px); }
    }

    .sc-welcome {
      font-size: 0.8125rem;
      color: #545460;
      text-align: center;
      padding: 1.5rem 1rem;
      line-height: 1.6;
    }

    .sc-input-area {
      display: flex;
      gap: 0.5rem;
      padding: 0.75rem 1rem;
      border-top: 1px solid rgba(255,255,255,0.06);
      background: #0C0C0E;
      flex-shrink: 0;
    }
    .sc-input {
      flex: 1;
      background: #141416;
      border: 1px solid rgba(255,255,255,0.08);
      border-radius: 8px;
      padding: 0.5rem 0.75rem;
      color: #F2EEE5;
      font-size: 0.8125rem;
      font-family: inherit;
      resize: none;
      outline: none;
      min-height: 36px;
      max-height: 72px;
      line-height: 1.5;
      transition: border-color 0.15s;
    }
    .sc-input:focus { border-color: rgba(1,135,160,0.4); }
    .sc-input::placeholder { color: #3a3a40; }
    .sc-send {
      background: #C68A3A;
      border: none;
      border-radius: 8px;
      padding: 0 0.875rem;
      color: #000;
      font-weight: 600;
      font-size: 0.8125rem;
      cursor: pointer;
      font-family: inherit;
      white-space: nowrap;
      transition: background 0.15s;
      flex-shrink: 0;
    }
    .sc-send:hover { background: #A87328; }
    .sc-send:disabled { opacity: 0.4; cursor: not-allowed; }

    .sc-footer-link {
      display: block;
      text-align: center;
      font-size: 0.7rem;
      color: #3a3a40;
      padding: 0.375rem;
      text-decoration: none;
      background: #0C0C0E;
      border-top: 1px solid rgba(255,255,255,0.04);
      transition: color 0.1s;
      flex-shrink: 0;
    }
    .sc-footer-link:hover { color: #545460; }
  `;
  document.head.appendChild(style);

  // ── Bubble ────────────────────────────────────────────────────────────────
  var bubble = document.createElement('button');
  bubble.className = 'sc-bubble';
  bubble.setAttribute('aria-label', 'Open support chat');
  bubble.innerHTML = '<svg viewBox="0 0 24 24"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H5.2L4 17.2V4h16v12z"/></svg>';
  document.body.appendChild(bubble);

  // ── Window ────────────────────────────────────────────────────────────────
  var win = document.createElement('div');
  win.className = 'sc-window sc-hidden';
  win.innerHTML = `
    <div class="sc-header">
      <div class="sc-header-dot"></div>
      <span class="sc-header-title">Craft &amp; Ship Support</span>
      <span class="sc-header-sub">Usually instant</span>
      <button class="sc-header-close" aria-label="Close chat">&times;</button>
    </div>
    <div class="sc-messages" id="sc-messages"></div>
    <div class="sc-input-area">
      <textarea class="sc-input" placeholder="Ask anything..." rows="1"></textarea>
      <button class="sc-send">Send</button>
    </div>
    <a class="sc-footer-link" href="/contact">Prefer email? Contact us &rarr;</a>
  `;
  document.body.appendChild(win);

  var msgContainer = win.querySelector('#sc-messages');
  var input = win.querySelector('.sc-input');
  var sendBtn = win.querySelector('.sc-send');
  var closeBtn = win.querySelector('.sc-header-close');
  var isOpen = false;
  var isSending = false;

  function scrollToBottom() { msgContainer.scrollTop = msgContainer.scrollHeight; }

  function addMessageToDOM(role, text) {
    var div = document.createElement('div');
    div.className = 'sc-msg sc-msg-' + role;
    div.textContent = text;
    msgContainer.appendChild(div);
    scrollToBottom();
  }

  function renderHistory() {
    msgContainer.innerHTML = '';
    if (messages.length === 0) {
      var welcome = document.createElement('div');
      welcome.className = 'sc-welcome';
      welcome.textContent = 'Ask me anything about Craft & Ship; pricing, build times, what kinds of apps we build, or how to get started.';
      msgContainer.appendChild(welcome);
      return;
    }
    messages.forEach(function (m) { addMessageToDOM(m.role, m.text); });
  }

  function showTyping() {
    var typing = document.createElement('div');
    typing.className = 'sc-typing';
    typing.id = 'sc-typing';
    typing.innerHTML = '<span></span><span></span><span></span>';
    msgContainer.appendChild(typing);
    scrollToBottom();
  }

  function hideTyping() {
    var el = document.getElementById('sc-typing');
    if (el) el.remove();
  }

  function toggleWindow() {
    isOpen = !isOpen;
    if (isOpen) {
      win.classList.remove('sc-hidden');
      renderHistory();
      input.focus();
    } else {
      win.classList.add('sc-hidden');
    }
  }

  async function sendMessage() {
    if (isSending) return;
    var text = input.value.trim();
    if (!text) return;
    if (text.length > MAX_MESSAGE_LENGTH) text = text.slice(0, MAX_MESSAGE_LENGTH);

    var welcome = msgContainer.querySelector('.sc-welcome');
    if (welcome) welcome.remove();

    messages.push({ role: 'user', text: text });
    saveHistory(messages);
    addMessageToDOM('user', text);
    input.value = '';
    input.style.height = 'auto';

    isSending = true;
    sendBtn.disabled = true;
    showTyping();

    try {
      var historyToSend = messages.slice(0, -1).slice(-20);
      var resp = await fetch('/api/support/chat', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'x-csrf-token': (document.querySelector('meta[name="csrf-token"]') || {}).content || '',
        },
        body: JSON.stringify({ message: text, history: historyToSend }),
      });

      hideTyping();

      if (!resp.ok) {
        var errData = await resp.json().catch(function () { return {}; });
        addMessageToDOM('assistant', errData.error || 'Something went wrong. Please try again.');
        return;
      }

      var data = await resp.json();
      messages.push({ role: 'assistant', text: data.reply });
      saveHistory(messages);
      addMessageToDOM('assistant', data.reply);
    } catch (_err) {
      hideTyping();
      addMessageToDOM('assistant', 'Unable to connect. Please check your connection and try again.');
    } finally {
      isSending = false;
      sendBtn.disabled = false;
      input.focus();
    }
  }

  bubble.addEventListener('click', toggleWindow);
  closeBtn.addEventListener('click', toggleWindow);
  sendBtn.addEventListener('click', sendMessage);
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });
  input.addEventListener('input', function () {
    this.style.height = 'auto';
    this.style.height = Math.min(this.scrollHeight, 72) + 'px';
  });
})();
