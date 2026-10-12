/* Image upload handler: auto-injected by _ensure_upload_form_ui() */
(function() {
  'use strict';
  document.querySelectorAll('.image-upload-zone').forEach(function(zone) {
    var targetId = zone.getAttribute('data-target');
    var hiddenInput = document.getElementById(targetId);
    if (!hiddenInput) return;

    var fileInput = zone.querySelector('.upload-file-input');
    var preview = zone.querySelector('.upload-preview');
    var previewImg = preview ? preview.querySelector('img') : null;
    var promptEl = zone.querySelector('.upload-prompt');
    var progressWrap = zone.querySelector('.upload-progress');
    var progressFill = progressWrap ? progressWrap.querySelector('.upload-bar') : null;
    var errorEl = zone.querySelector('.upload-error');
    var removeBtn = zone.querySelector('.upload-remove');
    var isRequired = zone.getAttribute('data-required') === 'true';

    // CSRF token from nearest form
    var form = zone.closest('form');
    var csrfEl = form ? form.querySelector('input[name="_csrf"]') : null;
    var csrf = csrfEl ? csrfEl.value : '';

    // Show existing image on page load (edit forms)
    if (hiddenInput.value && hiddenInput.value.trim()) {
      showPreview(hiddenInput.value);
    }

    // Click zone to open file browser
    zone.addEventListener('click', function(e) {
      if (e.target.closest('.upload-remove')) return;
      fileInput.click();
    });

    // Drag-and-drop
    ['dragenter', 'dragover'].forEach(function(evt) {
      zone.addEventListener(evt, function(e) {
        e.preventDefault();
        e.stopPropagation();
        zone.classList.add('upload-dragover');
      });
    });
    ['dragleave', 'drop'].forEach(function(evt) {
      zone.addEventListener(evt, function(e) {
        e.preventDefault();
        e.stopPropagation();
        zone.classList.remove('upload-dragover');
      });
    });
    zone.addEventListener('drop', function(e) {
      if (e.dataTransfer.files.length) uploadFile(e.dataTransfer.files[0]);
    });

    // File input change
    fileInput.addEventListener('change', function() {
      if (fileInput.files.length) uploadFile(fileInput.files[0]);
    });

    // Remove button
    if (removeBtn) {
      removeBtn.addEventListener('click', function(e) {
        e.preventDefault();
        e.stopPropagation();
        hiddenInput.value = '';
        hidePreview();
        fileInput.value = '';
      });
    }

    // Required field validation on form submit
    if (isRequired && form) {
      form.addEventListener('submit', function(e) {
        if (!hiddenInput.value || !hiddenInput.value.trim()) {
          e.preventDefault();
          showError('Please upload an image.');
          zone.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
      });
    }

    function uploadFile(file) {
      if (file.size > 5 * 1024 * 1024) {
        showError('File too large (max 5 MB).');
        return;
      }
      if (['image/jpeg','image/png','image/webp','image/gif'].indexOf(file.type) === -1) {
        showError('Only JPEG, PNG, WebP, and GIF allowed.');
        return;
      }

      clearError();
      showProgress();

      var fd = new FormData();
      fd.append('file', file);

      var xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/upload');
      if (csrf) xhr.setRequestHeader('x-csrf-token', csrf);

      xhr.upload.onprogress = function(e) {
        if (e.lengthComputable && progressFill) {
          progressFill.style.width = Math.round((e.loaded / e.total) * 90 + 10) + '%';
        }
      };

      xhr.onload = function() {
        hideProgress();
        if (xhr.status === 200) {
          try {
            var data = JSON.parse(xhr.responseText);
            if (data.url) {
              hiddenInput.value = data.url;
              showPreview(data.url);
            } else {
              showError(data.error || 'Upload failed.');
            }
          } catch(err) {
            showError('Upload failed.');
          }
        } else if (xhr.status === 429) {
          showError('Too many uploads. Wait a minute.');
        } else {
          showError('Upload failed (' + xhr.status + ').');
        }
      };

      xhr.onerror = function() {
        hideProgress();
        showError('Network error.');
      };

      xhr.send(fd);
    }

    function showPreview(url) {
      if (previewImg) previewImg.src = url;
      if (preview) preview.style.display = '';
      if (promptEl) promptEl.style.display = 'none';
    }

    function hidePreview() {
      if (preview) preview.style.display = 'none';
      if (promptEl) promptEl.style.display = '';
    }

    function showProgress() {
      if (progressWrap) progressWrap.style.display = '';
      if (progressFill) progressFill.style.width = '10%';
    }

    function hideProgress() {
      if (progressWrap) progressWrap.style.display = 'none';
      if (progressFill) progressFill.style.width = '0%';
    }

    function showError(msg) {
      if (errorEl) { errorEl.textContent = msg; errorEl.style.display = ''; }
    }

    function clearError() {
      if (errorEl) errorEl.style.display = 'none';
    }
  });
})();
