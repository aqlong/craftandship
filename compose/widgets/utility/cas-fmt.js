/* compose:utility cas-fmt, client-side <time> formatters.
 *
 * Keeps date rendering off the server (which lives in UTC on Railway) and
 * in the visitor's locale + timezone. Each widget that wants client-side
 * formatting emits a <time class="cas-fmt-*" datetime="<ISO>"><server
 * fallback></time> element; this script overwrites textContent with the
 * locale-formatted value once the DOM is ready.
 *
 * Idempotent: re-running on the same element reads the original value back
 * from the datetime attribute, not the (already-formatted) textContent.
 *
 * Classes:
 *   .cas-fmt-date              -> "Jan 15, 2026"
 *   .cas-fmt-date-long         -> "January 15, 2026"
 *   .cas-fmt-date-weekday      -> "Wed, Jan 15, 2026"
 *   .cas-fmt-datetime-compact  -> "Jan 15, 2026 · 4:30 PM"
 */
(function () {
  function _read(el) {
    var v = el.getAttribute('datetime');
    if (v == null || v === '') v = el.textContent.trim();
    return v;
  }
  function fmtDate(el) {
    var v = _read(el);
    var d = new Date(v);
    if (isNaN(d.getTime())) return;
    el.textContent = d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  }
  function fmtDateLong(el) {
    var v = _read(el);
    var d = new Date(v);
    if (isNaN(d.getTime())) return;
    el.textContent = d.toLocaleDateString(undefined, { year: 'numeric', month: 'long', day: 'numeric' });
  }
  function fmtDateWeekday(el) {
    var v = _read(el);
    var d = new Date(v);
    if (isNaN(d.getTime())) return;
    el.textContent = d.toLocaleDateString(undefined, { weekday: 'short', year: 'numeric', month: 'short', day: 'numeric' });
  }
  function fmtDateTimeCompact(el) {
    var v = _read(el);
    var d = new Date(v);
    if (isNaN(d.getTime())) return;
    var date = d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
    var time = d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
    el.textContent = date + ' · ' + time;
  }
  function run() {
    document.querySelectorAll('.cas-fmt-date').forEach(fmtDate);
    document.querySelectorAll('.cas-fmt-date-long').forEach(fmtDateLong);
    document.querySelectorAll('.cas-fmt-date-weekday').forEach(fmtDateWeekday);
    document.querySelectorAll('.cas-fmt-datetime-compact').forEach(fmtDateTimeCompact);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', run);
  } else {
    run();
  }
})();
