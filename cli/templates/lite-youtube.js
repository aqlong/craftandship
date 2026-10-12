/**
 * lite-youtube - Privacy-friendly YouTube embeds.
 * Renders a thumbnail preview; on click loads the actual YouTube player.
 * Uses youtube-nocookie.com for privacy (no tracking cookies).
 *
 * A small independent reimplementation of the lite-youtube-embed pattern
 * (https://github.com/paulirish/lite-youtube-embed, Apache-2.0): same element
 * name, `videoid` attribute and play-button icon path; class names, DOM and
 * styles differ from upstream.
 */
class LiteYouTube extends HTMLElement {
  connectedCallback() {
    this.videoId = this.getAttribute('videoid');
    if (!this.videoId) return;

    // Set thumbnail background
    this.style.backgroundImage = `url(https://i.ytimg.com/vi/${this.videoId}/hqdefault.jpg)`;

    // Create play button
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.setAttribute('aria-label', 'Play video');
    btn.className = 'lite-youtube-play';
    btn.innerHTML = '<svg viewBox="0 0 68 48" width="68" height="48"><path d="M66.52 7.74c-.78-2.93-2.49-5.41-5.42-6.19C55.79.13 34 0 34 0S12.21.13 6.9 1.55C3.97 2.33 2.27 4.81 1.48 7.74.06 13.05 0 24 0 24s.06 10.95 1.48 16.26c.78 2.93 2.49 5.41 5.42 6.19C12.21 47.87 34 48 34 48s21.79-.13 27.1-1.55c2.93-.78 4.64-3.26 5.42-6.19C67.94 34.95 68 24 68 24s-.06-10.95-1.48-16.26z" fill="red"/><path d="M45 24L27 14v20" fill="white"/></svg>';
    this.appendChild(btn);

    // On click, load the iframe
    this.addEventListener('click', () => this._loadIframe(), { once: true });
  }

  _loadIframe() {
    const iframe = document.createElement('iframe');
    iframe.src = `https://www.youtube-nocookie.com/embed/${this.videoId}?autoplay=1`;
    iframe.setAttribute('frameborder', '0');
    iframe.setAttribute('allowfullscreen', '');
    iframe.setAttribute('allow', 'accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture');
    iframe.setAttribute('loading', 'lazy');
    this.innerHTML = '';
    this.appendChild(iframe);
    this.classList.add('lite-youtube-activated');
  }
}
customElements.define('lite-youtube', LiteYouTube);
