/* global window, document */
(function () {
  'use strict';

  var buildId = window.REVISION_BUILD_ID;
  var csrfToken = window.REVISION_CSRF_TOKEN;
  var revisionReady = window.REVISION_READY;

  var chatMessages = document.getElementById('chat-messages');
  var messageInput = document.getElementById('message-input');
  var sendBtn = document.getElementById('send-btn');
  var confirmSection = document.getElementById('confirm-section');

  if (!chatMessages) return;

  scrollToBottom();

  function scrollToBottom() {
    if (chatMessages) {
      chatMessages.scrollTop = chatMessages.scrollHeight;
    }
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

  function appendMessage(role, content) {
    // Harbor v2: row + bubble class names match the server-rendered markup
    // in views/revision.ejs so dynamically-appended messages get the
    // warm-paper / ink styling under layout-harbor.
    var msgDiv = document.createElement('div');
    msgDiv.className = 'harbor-chat-row harbor-chat-row--' + role;

    var bubble = document.createElement('div');
    bubble.className = 'harbor-chat-bubble harbor-chat-bubble--' + role;

    var text = document.createElement('pre');
    text.className = 'harbor-chat-text';
    text.innerHTML = escapeHtml(content)
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/\n/g, '<br>');

    bubble.appendChild(text);
    msgDiv.appendChild(bubble);
    chatMessages.appendChild(msgDiv);
    scrollToBottom();
  }

  function showTypingIndicator() {
    var indicator = document.createElement('div');
    indicator.className = 'harbor-chat-row harbor-chat-row--assistant chat-typing-indicator';
    indicator.id = 'typing-indicator';

    var bubble = document.createElement('div');
    bubble.className = 'harbor-chat-bubble harbor-chat-bubble--assistant typing-bubble';
    bubble.innerHTML = '<span class="typing-dot"></span><span class="typing-dot"></span><span class="typing-dot"></span>';

    indicator.appendChild(bubble);
    chatMessages.appendChild(indicator);
    scrollToBottom();
    return indicator;
  }

  function removeTypingIndicator() {
    var indicator = document.getElementById('typing-indicator');
    if (indicator) indicator.remove();
  }

  function setLoading(loading) {
    if (sendBtn) {
      sendBtn.disabled = loading;
      sendBtn.textContent = loading ? 'Sending...' : 'Send';
    }
    if (messageInput) {
      messageInput.disabled = loading;
    }
  }

  async function sendMessage() {
    if (!messageInput) return;

    var message = messageInput.value.trim();
    if (!message || revisionReady) return;

    messageInput.value = '';
    appendMessage('user', message);
    setLoading(true);

    showTypingIndicator();

    try {
      var response = await fetch('/builds/' + buildId + '/revision/message', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'x-csrf-token': csrfToken,
        },
        body: JSON.stringify({ message: message }),
      });

      removeTypingIndicator();

      if (!response.ok) {
        var errData = await response.json().catch(function () { return {}; });
        var friendlyError = 'Something went wrong. Please try sending your message again.';
        if (response.status === 429) {
          friendlyError = 'You are sending messages too quickly. Please wait a moment and try again.';
        } else if (response.status === 503) {
          friendlyError = 'We are experiencing high demand right now. Please wait about 30 seconds and try again.';
        } else if (response.status === 403) {
          friendlyError = 'Your session may have expired. Please refresh the page and try again.';
        } else if (errData.error) {
          friendlyError = errData.error;
        }
        appendMessage('assistant', friendlyError);
        setLoading(false);
        if (messageInput) setTimeout(function () { messageInput.focus(); }, 0);
        return;
      }

      var data = await response.json();
      appendMessage('assistant', data.reply);

      if (data.revisionReady) {
        revisionReady = true;
        var inputArea = document.getElementById('chat-input-area');
        if (inputArea) inputArea.style.display = 'none';
        if (confirmSection) confirmSection.style.display = '';
      }
    } catch (err) {
      removeTypingIndicator();
      appendMessage('assistant', 'Network error. Please check your connection and try again.');
      setLoading(false);
      if (messageInput) setTimeout(function () { messageInput.focus(); }, 0);
      return;
    }

    setLoading(false);
    if (messageInput && !revisionReady) {
      setTimeout(function () { messageInput.focus(); }, 0);
    }
  }

  if (sendBtn) {
    sendBtn.addEventListener('click', sendMessage);
  }

  if (messageInput) {
    messageInput.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        sendMessage();
      }
    });

    if (!revisionReady) messageInput.focus();
  }
})();
