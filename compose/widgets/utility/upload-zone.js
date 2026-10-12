// compose:widget utility/upload-zone-js
// Client-side runtime for admin/upload-zone widget. POSTs to /admin/upload
// with CSRF + file; on success sets the hidden input to the returned URL.
(function () {
  function getCsrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) return meta.getAttribute('content');
    var input = document.querySelector('form input[name="_csrf"]');
    return input ? input.value : '';
  }
  function show(el) { if (el) el.style.display = 'block'; }
  function hide(el) { if (el) el.style.display = 'none'; }

  window.__uz_clear = function (baseId) {
    document.getElementById(baseId + '-hidden').value = '';
    document.getElementById(baseId + '-img').src = '';
    hide(document.getElementById(baseId + '-preview'));
    show(document.getElementById(baseId + '-prompt'));
  };

  window.__uz_drop = function (event, baseId, fieldName) {
    event.preventDefault();
    event.currentTarget.style.borderColor = 'var(--color-border)';
    var file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
    if (!file) return;
    window.__uz_upload(file, baseId, fieldName);
  };

  // Wire each upload zone's interactions via addEventListener instead of
  // inline on*= attributes (those are blocked by the app's script-src nonce
  // CSP). The widget HTML carries stable ids (<base>-prompt / -file) + a
  // data-field on the .upload-zone wrapper, so we can bind without inline JS.
  function wireZones() {
    var zones = document.querySelectorAll('.upload-zone[data-field]');
    for (var i = 0; i < zones.length; i++) {
      (function (wrap) {
        if (wrap.__uzBound) return;
        wrap.__uzBound = true;
        var fieldName = wrap.getAttribute('data-field');
        var baseId = (wrap.id || '').replace(/-wrap$/, '');
        if (!baseId) return;
        var prompt = document.getElementById(baseId + '-prompt');
        var fileInput = document.getElementById(baseId + '-file');
        var removeBtn = wrap.querySelector('.upload-preview button');
        if (prompt && fileInput) {
          prompt.addEventListener('click', function () { fileInput.click(); });
          prompt.addEventListener('dragover', function (e) {
            e.preventDefault(); prompt.style.borderColor = 'var(--color-primary)';
          });
          prompt.addEventListener('dragleave', function () {
            prompt.style.borderColor = 'var(--color-border)';
          });
          prompt.addEventListener('drop', function (e) {
            window.__uz_drop(e, baseId, fieldName);
          });
        }
        if (fileInput) {
          fileInput.addEventListener('change', function () {
            window.__uz_upload(fileInput.files[0], baseId, fieldName);
          });
        }
        if (removeBtn) {
          removeBtn.addEventListener('click', function () { window.__uz_clear(baseId); });
        }
      })(zones[i]);
    }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wireZones);
  } else {
    wireZones();
  }

  window.__uz_upload = function (file, baseId, fieldName) {
    if (!file) return;
    var progress = document.getElementById(baseId + '-progress');
    show(progress);
    progress.textContent = 'Uploading…';
    var fd = new FormData();
    fd.append('file', file);
    fd.append('_csrf', getCsrfToken());
    fd.append('field_name', fieldName);
    fetch('/admin/upload', {
      method: 'POST',
      headers: { 'x-csrf-token': getCsrfToken() },
      body: fd,
      credentials: 'same-origin'
    })
    .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
    .then(function (resp) {
      if (!resp.ok || !resp.j || !resp.j.url) {
        progress.textContent = 'Upload failed: ' + (resp.j && resp.j.error || 'unknown');
        progress.style.color = 'var(--color-error)';
        return;
      }
      document.getElementById(baseId + '-hidden').value = resp.j.url;
      document.getElementById(baseId + '-img').src = resp.j.url;
      hide(document.getElementById(baseId + '-prompt'));
      show(document.getElementById(baseId + '-preview'));
      progress.textContent = 'Uploaded.';
      setTimeout(function () { hide(progress); }, 1500);
    })
    .catch(function (e) {
      progress.textContent = 'Upload failed: ' + e.message;
      progress.style.color = 'var(--color-error)';
    });
  };
})();
