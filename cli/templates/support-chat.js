(() => {
  const widget = document.createElement('div');
  widget.id = 'support-chat-widget';
  widget.innerHTML = `
    <button id="support-toggle" style="position:fixed;bottom:20px;right:20px;width:56px;height:56px;border-radius:28px;background:var(--primary,#2563eb);color:#fff;border:none;cursor:pointer;font-size:24px;box-shadow:0 4px 12px rgba(0,0,0,0.3);z-index:9999;display:flex;align-items:center;justify-content:center" aria-label="Open support chat">💬</button>
    <div id="support-panel" style="display:none;position:fixed;bottom:86px;right:20px;width:340px;max-height:460px;border-radius:12px;background:#1a1a2e;border:1px solid #333;box-shadow:0 8px 32px rgba(0,0,0,0.4);z-index:9999;overflow:hidden;font-family:system-ui">
      <div style="padding:12px 16px;background:#252540;border-bottom:1px solid #333;display:flex;justify-content:space-between;align-items:center">
        <span style="color:#fff;font-weight:600;font-size:14px">Support</span>
        <button id="support-close" style="background:none;border:none;color:#888;cursor:pointer;font-size:18px">&times;</button>
      </div>
      <div id="support-messages" style="padding:12px;max-height:320px;overflow-y:auto;font-size:13px"></div>
      <div style="padding:8px 12px;border-top:1px solid #333;display:flex;gap:8px">
        <input id="support-input" placeholder="Ask a question..." style="flex:1;padding:8px;border-radius:6px;border:1px solid #444;background:#0d0d1a;color:#fff;font-size:13px">
        <button id="support-send" style="padding:8px 14px;border-radius:6px;background:var(--primary,#2563eb);color:#fff;border:none;cursor:pointer;font-size:13px">Send</button>
      </div>
    </div>`;
  document.body.appendChild(widget);

  const toggle = document.getElementById('support-toggle');
  const panel = document.getElementById('support-panel');
  const close = document.getElementById('support-close');
  const input = document.getElementById('support-input');
  const send = document.getElementById('support-send');
  const msgs = document.getElementById('support-messages');
  let history = JSON.parse(sessionStorage.getItem('support_history') || '[]');

  function addMsg(role, text) {
    const div = document.createElement('div');
    div.style.cssText = `margin-bottom:8px;padding:8px 10px;border-radius:8px;${role === 'user' ? 'background:#252540;margin-left:40px;text-align:right' : 'background:#0d0d1a;margin-right:40px'};color:#ddd;line-height:1.4`;
    div.textContent = text;
    msgs.appendChild(div);
    msgs.scrollTop = msgs.scrollHeight;
  }

  history.forEach(m => addMsg(m.role, m.content));

  toggle.onclick = () => { panel.style.display = panel.style.display === 'none' ? 'block' : 'none'; };
  close.onclick = () => { panel.style.display = 'none'; };

  async function sendMessage() {
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    addMsg('user', text);
    history.push({ role: 'user', content: text });
    sessionStorage.setItem('support_history', JSON.stringify(history));
    try {
      const res = await fetch('/api/support/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text, history: history.slice(-20) }),
      });
      const data = await res.json();
      addMsg('assistant', data.reply || 'Sorry, I could not process that.');
      history.push({ role: 'assistant', content: data.reply });
      sessionStorage.setItem('support_history', JSON.stringify(history));
    } catch (e) {
      addMsg('assistant', 'Connection error. Please try again.');
    }
  }

  send.onclick = sendMessage;
  input.onkeydown = (e) => { if (e.key === 'Enter') sendMessage(); };
})();
