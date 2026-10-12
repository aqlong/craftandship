/* global window, document */
(function () {
  'use strict';

  const buildId = window.INTAKE_BUILD_ID;
  const csrfToken = window.INTAKE_CSRF_TOKEN;
  let specComplete = window.INTAKE_SPEC_COMPLETE;

  const chatMessages = document.getElementById('chat-messages');
  const messageInput = document.getElementById('message-input');
  const sendBtn = document.getElementById('send-btn');
  const specPanel = document.getElementById('spec-panel');
  const specContent = document.getElementById('spec-content');

  var userMessageCount = chatMessages
    ? chatMessages.querySelectorAll('.chat-message-user').length
    : 0;

  if (!chatMessages) return;

  scrollToBottom();

  function scrollToBottom() {
    if (chatMessages) {
      chatMessages.scrollTop = chatMessages.scrollHeight;
    }
  }

  function appendMessage(role, content) {
    // Harbor v2: row + bubble class names match the server-rendered markup
    // in views/intake.ejs so dynamically-appended messages get the warm-paper
    // / ink styling. (Legacy chat-message / chat-bubble classes were the
    // dark-mode aesthetic and aren't loaded under layout-harbor.)
    const msgDiv = document.createElement('div');
    msgDiv.className = 'harbor-chat-row harbor-chat-row--' + role;

    const bubble = document.createElement('div');
    bubble.className = 'harbor-chat-bubble harbor-chat-bubble--' + role;

    const text = document.createElement('pre');
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
    const indicator = document.createElement('div');
    indicator.className = 'harbor-chat-row harbor-chat-row--assistant chat-typing-indicator';
    indicator.id = 'typing-indicator';

    const bubble = document.createElement('div');
    bubble.className = 'harbor-chat-bubble harbor-chat-bubble--assistant typing-bubble';
    bubble.innerHTML = '<span class="typing-dot"></span><span class="typing-dot"></span><span class="typing-dot"></span>';

    indicator.appendChild(bubble);
    chatMessages.appendChild(indicator);
    scrollToBottom();
    return indicator;
  }

  function removeTypingIndicator() {
    const indicator = document.getElementById('typing-indicator');
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

  function renderSpec(spec) {
    if (!specContent || !spec) return;

    specContent.innerHTML = '';

    function addField(label, value) {
      const div = document.createElement('div');
      div.className = 'flex flex-col py-2.5 border-b border-border';
      div.innerHTML = '<label class="text-xs uppercase tracking-wider text-muted mb-0.5">' + escapeHtml(label) + '</label>' +
        '<span class="text-sm">' + escapeHtml(String(value || '')) + '</span>';
      specContent.appendChild(div);
    }

    function addListField(label, items) {
      if (!items || items.length === 0) return;
      const div = document.createElement('div');
      div.className = 'flex flex-col py-2.5 border-b border-border';
      const ul = document.createElement('ul');
      ul.className = 'text-sm list-disc pl-5 space-y-1 mt-1';
      items.forEach(function (text) {
        const li = document.createElement('li');
        li.textContent = text;
        ul.appendChild(li);
      });
      div.innerHTML = '<label class="text-xs uppercase tracking-wider text-muted mb-0.5">' + escapeHtml(label) + '</label>';
      div.appendChild(ul);
      specContent.appendChild(div);
    }

    addField('App Name', spec.app_name);
    addField('Description', spec.description);

    if (spec.pages && spec.pages.length > 0) {
      addListField('Pages', spec.pages.map(function (p) {
        return p.title + ' -' + p.purpose;
      }));
    }

    if (spec.user_roles && spec.user_roles.length > 0) {
      addListField('User Roles', spec.user_roles.map(function (ur) {
        return ur.role + ' -' + ur.description;
      }));
    }

    if (spec.data_models && spec.data_models.length > 0) {
      addListField('Data Models', spec.data_models.map(function (m) {
        return m.name;
      }));
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

  var stageLabels = ['App idea', 'Users and roles', 'Features and pages', 'Design preferences', 'Finalizing spec'];

  function updateProgress() {
    var step = Math.min(Math.floor(userMessageCount / 2) + 1, 5);
    var progressLabel = document.getElementById('progress-label');
    var stageLabel = document.getElementById('progress-stage-label');
    var dots = document.querySelectorAll('#progress-dots [data-dot]');

    if (progressLabel) {
      progressLabel.textContent = 'Step ' + step + ' of 5';
    }
    if (stageLabel) {
      stageLabel.textContent = stageLabels[step - 1] || '';
    }
    dots.forEach(function (dot) {
      var dotNum = parseInt(dot.getAttribute('data-dot'), 10);
      if (dotNum <= step) {
        dot.classList.remove('bg-border');
        dot.classList.add('bg-brand');
      } else {
        dot.classList.remove('bg-brand');
        dot.classList.add('bg-border');
      }
    });
  }

  function removeHelper() {
    var helper = document.getElementById('intake-helper');
    if (helper) {
      helper.remove();
    }
  }

  // Suggestion chip click handling
  var chips = document.querySelectorAll('.suggestion-chip');
  chips.forEach(function (chip) {
    chip.addEventListener('click', function () {
      var suggestion = chip.getAttribute('data-suggestion');
      if (messageInput && suggestion) {
        messageInput.value = suggestion;
        messageInput.focus();
      }
    });
  });

  async function sendMessage() {
    if (!messageInput) return;

    const message = messageInput.value.trim();
    if (!message || specComplete) return;

    messageInput.value = '';
    removeHelper();
    appendMessage('user', message);
    userMessageCount++;
    updateProgress();
    setLoading(true);

    const typingIndicator = showTypingIndicator();

    try {
      const response = await fetch('/intake/message', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'x-csrf-token': csrfToken,
        },
        body: JSON.stringify({ buildId, message }),
      });

      removeTypingIndicator();

      if (!response.ok) {
        const errData = await response.json().catch(function () { return {}; });
        var friendlyError = 'Something went wrong. Please try sending your message again.';
        if (response.status === 429) {
          friendlyError = 'You are sending messages too quickly. Please wait a moment and try again.';
        } else if (response.status === 503) {
          friendlyError = 'We are experiencing high demand right now. Please wait about 30 seconds and try again. Your progress is saved.';
        } else if (response.status === 403) {
          friendlyError = 'Your session may have expired. Please refresh the page and try again.';
        } else if (errData.error) {
          friendlyError = errData.error;
        }
        appendMessage('assistant', friendlyError);
        setLoading(false);
        if (messageInput && !specComplete) {
          setTimeout(function () { messageInput.focus(); }, 0);
        }
        return;
      }

      const data = await response.json();

      appendMessage('assistant', data.reply);

      if (data.specComplete && data.spec) {
        specComplete = true;
        renderSpec(data.spec);

        if (specPanel) {
          specPanel.style.display = '';
        }

        // Reveal confirm form + final spec block; hide any anon preview placeholder.
        if (specContent) {
          specContent.style.display = '';
        }
        var confirmNote = specPanel ? specPanel.querySelector('.intake__confirm') : null;
        if (confirmNote) {
          confirmNote.style.display = '';
        }
        var anonPreview = document.getElementById('anon-spec-preview');
        if (anonPreview) {
          anonPreview.style.display = 'none';
        }
        var panelTitle = specPanel ? specPanel.querySelector('h2') : null;
        if (panelTitle) {
          panelTitle.textContent = 'Your Website Spec';
        }

        var progressBar = document.getElementById('intake-progress');
        if (progressBar) {
          progressBar.style.display = 'none';
        }

        const inputArea = document.getElementById('chat-input-area');
        if (inputArea) {
          inputArea.style.display = 'none';
        }

        setTimeout(function () {
          if (specPanel) {
            specPanel.scrollIntoView({ behavior: 'smooth' });
          }
        }, 500);
      }
    } catch (err) {
      removeTypingIndicator();
      appendMessage('assistant', 'Network error. Please check your connection and try again.');
      setLoading(false);
      if (messageInput && !specComplete) {
        setTimeout(function () { messageInput.focus(); }, 0);
      }
      return;
    }

    setLoading(false);
    if (messageInput && !specComplete) {
      // Microtask delay ensures browser processes disabled=false before focus
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

  // Logo upload handler
  var logoInput = document.getElementById('logo-file-input');
  if (logoInput) {
    logoInput.addEventListener('change', async function () {
      var file = logoInput.files && logoInput.files[0];
      if (!file) return;

      var statusEl = document.getElementById('logo-status');
      var filenameEl = document.getElementById('logo-filename');
      var previewEl = document.getElementById('logo-preview');
      var previewImg = document.getElementById('logo-preview-img');
      var colorsEl = document.getElementById('logo-colors');
      var labelEl = document.getElementById('logo-upload-label');

      if (filenameEl) filenameEl.textContent = file.name;
      if (statusEl) statusEl.textContent = 'Processing...';
      if (labelEl) labelEl.textContent = 'Change File';

      var formData = new FormData();
      formData.append('logo', file);

      try {
        var response = await fetch('/intake/' + buildId + '/logo', {
          method: 'POST',
          headers: { 'x-csrf-token': csrfToken },
          body: formData,
        });

        var data = await response.json();

        if (!response.ok) {
          if (statusEl) statusEl.textContent = data.error || 'Upload failed.';
          return;
        }

        if (statusEl) statusEl.textContent = '';

        // Show preview
        // The server answers with a data: URL of the cleaned image. Only that
        // shape is assigned to src.
        if (
          previewEl && previewImg && typeof data.logo_preview === 'string' &&
          /^data:image\/(png|jpeg|webp|svg\+xml);base64,[A-Za-z0-9+\/=]+$/.test(data.logo_preview)
        ) {
          previewImg.src = data.logo_preview;
          previewEl.style.display = '';
        }

        // Show extracted colors
        if (colorsEl && data.colors) {
          var swatchPrimary = document.getElementById('swatch-primary');
          var swatchSecondary = document.getElementById('swatch-secondary');
          var swatchAccent = document.getElementById('swatch-accent');
          if (swatchPrimary) swatchPrimary.style.background = data.colors.primary_color;
          if (swatchSecondary) swatchSecondary.style.background = data.colors.secondary_color;
          if (swatchAccent) swatchAccent.style.background = data.colors.accent_color;
          colorsEl.style.display = '';
        }
      } catch (err) {
        if (statusEl) statusEl.textContent = 'Network error. Please try again.';
      }
    });
  }
})();
