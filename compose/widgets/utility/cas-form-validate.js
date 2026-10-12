/*! compose:widget utility/cas-form-validate
 * Client-side form validation for CAS v2 generated apps. Shipped as
 * /public/js/cas-form-validate.js and loaded from every layout.
 *
 * Why: native HTML5 pattern/required enforcement is inconsistent across
 * browsers: Chrome shows a terse tooltip that many users miss, Safari
 * sometimes skips pattern on type="tel", and none of them support
 * per-field INLINE error messages out of the box. This layer:
 *   1. Hooks into every <form class="cas-form">.
 *   2. On submit, checks every input/select/textarea against
 *      required, pattern, minlength, maxlength, type=email/url.
 *   3. On any failure: preventDefault, mark bad fields, render inline
 *      error messages under each bad field, and scroll to the first.
 *   4. Clears the error state for a field as soon as the user fixes it.
 *
 * Uses the pattern/required/min/max attrs ALREADY emitted by the
 * form/field.ejs partial: zero extra wiring needed per-widget.
 */
(function () {
  'use strict';

  var EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
  var URL_RE = /^https?:\/\/.+/;

  function labelFor(el) {
    var id = el.id;
    if (id) {
      var lab = document.querySelector('label[for="' + id + '"]');
      if (lab) {
        var txt = (lab.textContent || '').replace(/\s*\*\s*$/, '').trim();
        if (txt) return txt;
      }
    }
    return (el.getAttribute('placeholder') || el.name || 'This field').replace(/_/g, ' ');
  }

  function describeError(el) {
    var val = (el.value == null ? '' : String(el.value)).trim();
    var required = el.hasAttribute('required');
    var pattern = el.getAttribute('pattern');
    var minlenAttr = el.getAttribute('minlength');
    var maxlenAttr = el.getAttribute('maxlength');
    var type = (el.getAttribute('type') || '').toLowerCase();

    if (required && !val) return 'is required';
    if (!val) return null; // optional + empty is fine

    if (minlenAttr) {
      var minlen = parseInt(minlenAttr, 10);
      if (!isNaN(minlen) && val.length < minlen) {
        return 'must be at least ' + minlen + ' characters';
      }
    }
    if (maxlenAttr) {
      var maxlen = parseInt(maxlenAttr, 10);
      if (!isNaN(maxlen) && val.length > maxlen) {
        return 'must be under ' + maxlen + ' characters';
      }
    }
    if (type === 'email' && !EMAIL_RE.test(val)) {
      return 'must be a valid email address';
    }
    if (type === 'url' && !URL_RE.test(val)) {
      return 'must be a valid URL starting with http:// or https://';
    }
    if (pattern) {
      try {
        var re = new RegExp('^(?:' + pattern + ')$');
        if (!re.test(val)) {
          if (type === 'tel') return 'must be a valid phone number';
          if (type === 'email') return 'must be a valid email address';
          if (type === 'url') return 'must be a valid URL';
          return 'format is invalid';
        }
      } catch (e) { /* bad regex; skip */ }
    }
    return null;
  }

  function getOrMakeErrorNode(el) {
    // Attach an error span as the next sibling of the input's parent
    // label. Keyed by `data-cas-error-for` so we can find + update it.
    var field = el.closest('.cas-field') || el.parentElement;
    if (!field) return null;
    var node = field.querySelector('[data-cas-error-for="' + el.name + '"]');
    if (!node) {
      node = document.createElement('div');
      node.setAttribute('data-cas-error-for', el.name);
      node.setAttribute('role', 'alert');
      node.style.cssText = 'display:none;margin-top:4px;font-size:12px;line-height:1.4;color:var(--color-error, #991b1b);';
      field.appendChild(node);
    }
    return node;
  }

  function setError(el, msg) {
    el.setAttribute('aria-invalid', 'true');
    var node = getOrMakeErrorNode(el);
    if (node) {
      node.textContent = labelFor(el) + ' ' + msg + '.';
      node.style.display = 'block';
    }
  }

  function clearError(el) {
    el.removeAttribute('aria-invalid');
    var field = el.closest('.cas-field') || el.parentElement;
    if (!field) return;
    var node = field.querySelector('[data-cas-error-for="' + el.name + '"]');
    if (node) { node.style.display = 'none'; node.textContent = ''; }
  }

  function validateForm(form) {
    var firstBad = null;
    var els = form.querySelectorAll('input, select, textarea');
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (el.type === 'hidden' || el.disabled || el.readOnly) continue;
      if (el.name === '_csrf') continue;
      var err = describeError(el);
      if (err) {
        setError(el, err);
        if (!firstBad) firstBad = el;
      } else {
        clearError(el);
      }
    }
    return { ok: !firstBad, firstBad: firstBad };
  }

  function bindForm(form) {
    if (form.__casBound) return;
    form.__casBound = true;
    form.addEventListener('submit', function (e) {
      var result = validateForm(form);
      if (!result.ok) {
        e.preventDefault();
        e.stopPropagation();
        if (result.firstBad) {
          result.firstBad.focus();
          try {
            result.firstBad.scrollIntoView({ behavior: 'smooth', block: 'center' });
          } catch (err) { /* older Safari */ }
        }
      }
    }, true);
    // Clear error state as the user types/changes a field.
    form.addEventListener('input', function (e) {
      var el = e.target;
      if (!el || !el.name) return;
      if (el.getAttribute('aria-invalid') === 'true' && !describeError(el)) clearError(el);
    });
    form.addEventListener('change', function (e) {
      var el = e.target;
      if (!el || !el.name) return;
      var err = describeError(el);
      if (err) setError(el, err);
      else clearError(el);
    });
  }

  function init() {
    var forms = document.querySelectorAll('form.cas-form');
    for (var i = 0; i < forms.length; i++) bindForm(forms[i]);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
  // Expose for dynamic forms added later.
  window.__cas_form_validate = { bind: bindForm, validate: validateForm };

  // Delegated confirm guard. Replaces the CSP-blocked inline
  // onsubmit="return confirm(...)" on delete / destructive forms: those
  // inline handlers are rejected by the app's script-src nonce CSP (nonces
  // don't apply to attributes), so the confirmation never fired and the
  // action ran unconfirmed. Any <form data-confirm="message"> now prompts
  // here on submit (capture phase so it can cancel before the POST).
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (form && form.nodeName === 'FORM' && form.hasAttribute('data-confirm')) {
      if (!window.confirm(form.getAttribute('data-confirm'))) {
        e.preventDefault();
        e.stopPropagation();
      }
    }
  }, true);

  // Delegated "Go Back" handler for <a data-cas-back> (error pages). A
  // javascript: href cannot be used: the script-src nonce CSP blocks it.
  // The link keeps a real href as the fallback when there is no history.
  document.addEventListener('click', function (e) {
    var el = e.target;
    while (el && el !== document) {
      if (el.nodeName === 'A' && el.hasAttribute('data-cas-back')) {
        if (window.history.length > 1) {
          e.preventDefault();
          window.history.back();
        }
        return;
      }
      el = el.parentNode;
    }
  });
})();
