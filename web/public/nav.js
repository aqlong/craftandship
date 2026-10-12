// Scroll-aware nav: adds .nav-scrolled class when page is scrolled
(function () {
  var nav = document.getElementById('main-nav');
  if (!nav) return;
  var scrolled = false;
  function check() {
    var isScrolled = window.scrollY > 10;
    if (isScrolled !== scrolled) {
      scrolled = isScrolled;
      nav.classList.toggle('nav-scrolled', scrolled);
    }
  }
  window.addEventListener('scroll', check, { passive: true });
  check();
})();
