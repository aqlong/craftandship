/* global window, document */
(function () {
  'use strict';

  var featureId = window.FEATURE_ID;
  var csrfToken = window.FEATURE_CSRF_TOKEN;
  var scopeComplete = window.FEATURE_SCOPE_COMPLETE;
  var buildId = window.FEATURE_BUILD_ID;
  var devMode = window.FEATURE_DEV_MODE;

  var chatMessages = document.getElementById('chat-messages');
  var messageInput = document.getElementById('message-input');
  var sendBtn = document.getElementById('send-btn');
  var scopePanel = document.getElementById('scope-panel');
  var scopeContent = document.getElementById('scope-content');

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
    // in views/feature-chat.ejs so dynamically-appended messages get the
    // warm-paper / ink styling. (Legacy chat-message / chat-bubble classes
    // were the dark-mode aesthetic and aren't loaded under layout-harbor.)
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
    if (indicator) {
      indicator.remove();
    }
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

  function renderScope(scope) {
    if (!scopeContent || !scope) return;

    scopeContent.innerHTML = '';

    function addField(label, value) {
      var div = document.createElement('div');
      div.className = 'flex flex-col py-2.5 border-b border-border';
      div.innerHTML = '<label class="text-xs uppercase tracking-wider text-muted mb-0.5">' + escapeHtml(label) + '</label>' +
        '<span class="text-sm">' + escapeHtml(String(value || '')) + '</span>';
      scopeContent.appendChild(div);
    }

    addField('Description', scope.description);
    addField('Scope', scope.summary);

    var costDisplay = scope.estimated_cost === 4900 ? '$49' :
      scope.estimated_cost === 9900 ? '$99' : '$199';
    var sizeLabel = scope.size.charAt(0).toUpperCase() + scope.size.slice(1);
    addField('Size', sizeLabel);
    addField('Estimated Cost', costDisplay);

    // Add approve button
    var approveForm = document.createElement('form');
    approveForm.action = '/features/' + featureId + '/approve';
    approveForm.method = 'POST';
    approveForm.className = 'mt-4';
    approveForm.innerHTML =
      '<input type="hidden" name="_csrf" value="' + escapeHtml(csrfToken) + '" />' +
      '<button type="submit" class="btn btn-primary btn-lg btn-block">' +
      'Approve and Continue to Payment' +
      '</button>';
    scopeContent.appendChild(approveForm);
  }

  async function sendMessage() {
    if (!messageInput) return;

    var message = messageInput.value.trim();
    if (!message || scopeComplete) return;

    messageInput.value = '';
    appendMessage('user', message);
    setLoading(true);

    showTypingIndicator();

    try {
      var response = await fetch('/features/' + featureId + '/message', {
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
        appendMessage('assistant', errData.error || 'Something went wrong. Please try again.');
        setLoading(false);
        if (messageInput && !scopeComplete) {
          setTimeout(function () { messageInput.focus(); }, 0);
        }
        return;
      }

      var data = await response.json();

      appendMessage('assistant', data.reply);

      if (data.scopeComplete && data.scope) {
        scopeComplete = true;
        renderScope(data.scope);

        if (scopePanel) {
          scopePanel.style.display = '';
        }

        var inputArea = document.getElementById('chat-input-area');
        if (inputArea) {
          inputArea.style.display = 'none';
        }

        setTimeout(function () {
          if (scopePanel) {
            scopePanel.scrollIntoView({ behavior: 'smooth' });
          }
        }, 500);
      }
    } catch (err) {
      removeTypingIndicator();
      appendMessage('assistant', 'Network error. Please check your connection and try again.');
      setLoading(false);
      if (messageInput && !scopeComplete) {
        setTimeout(function () { messageInput.focus(); }, 0);
      }
      return;
    }

    setLoading(false);
    if (messageInput && !scopeComplete) {
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

    messageInput.focus();
  }
})();
