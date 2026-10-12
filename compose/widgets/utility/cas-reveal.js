/*! compose:widget utility/cas-reveal
 * Scroll-reveal runtime. Any element with class "cas-reveal" fades +
 * slides up when it enters the viewport. Uses IntersectionObserver;
 * falls back to immediate-reveal on old browsers. Respects
 * prefers-reduced-motion via CSS (animation disabled there).
 *
 * Also ships a safety fallback for the `.reveal` / `.in` pattern
 * commonly emitted by claude.ai/design output and other design
 * imports: 800ms after page ready, force `.in` on any remaining
 * `.reveal:not(.in)` so off-screen items, JS-race victims, or
 * elements caught mid-transition can never stay permanently
 * invisible. Same belt-and-suspenders is applied to
 * `.cas-reveal:not(.revealed)` so the native pattern is covered too.
 *
 * Ported from the CAS marketing site reveal pattern.
 */
(function () {
  'use strict';
  if (typeof window === 'undefined' || !document) return;

  var FALLBACK_MS = 800;

  function init() {
    var els = document.querySelectorAll('.cas-reveal');
    if (els.length) {
      // Fallback for no IntersectionObserver support; reveal immediately.
      if (!('IntersectionObserver' in window)) {
        for (var i = 0; i < els.length; i++) els[i].classList.add('revealed');
      } else {
        var io = new IntersectionObserver(function (entries) {
          entries.forEach(function (entry) {
            if (entry.isIntersecting) {
              entry.target.classList.add('revealed');
              io.unobserve(entry.target);
            }
          });
        }, { threshold: 0.12, rootMargin: '0px 0px -40px 0px' });
        els.forEach(function (el) { io.observe(el); });
      }
    }

    // Safety fallback: 800ms after page ready, force any still-hidden
    // reveal element visible. Covers both the native `.cas-reveal`
    // pattern and the imported-design `.reveal` / `.in` pattern.
    window.setTimeout(function () {
      var stragglersCas = document.querySelectorAll('.cas-reveal:not(.revealed)');
      for (var i = 0; i < stragglersCas.length; i++) {
        stragglersCas[i].classList.add('revealed');
      }
      var stragglersDesign = document.querySelectorAll('.reveal:not(.in)');
      for (var j = 0; j < stragglersDesign.length; j++) {
        stragglersDesign[j].classList.add('in');
      }
    }, FALLBACK_MS);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
