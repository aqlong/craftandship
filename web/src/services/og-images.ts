// Per-page OG image mapping.
//
// Background: every page used to share `og:image=https://craftandship.io/og-default.png`,
// so every link share on social/iMessage/Slack looked identical regardless
// of which page was being shared. The mapping below opts specific public
// pages into generated 1200x630 variants with the page title + brand
// mark; everything else still falls through to /og-default.png.
//
// To regenerate the PNG assets, run:
//   python3 scripts/generate_og_images.py
// The script writes into web/public/og-*.png. Commit the regenerated PNGs
// alongside any change to PER_PAGE_OG_IMAGES below so the asset stays in
// sync with the route map.

export const DEFAULT_OG_IMAGE = '/og-default.png';

// Path → OG image filename. Keys must be exact request paths (no trailing
// slashes, no query string). Resolution is exact-match only; nested routes
// must list themselves explicitly. Keep the list small and deliberate;
// see the SEO audit + roadmap for the curated set.
export const PER_PAGE_OG_IMAGES: Record<string, string> = {
  '/': '/og-default.png',
  '/pricing': '/og-pricing.png',
  '/the-companion': '/og-companion.png',
  '/how-it-works': '/og-how-it-works.png',
  '/gallery': '/og-gallery.png',
  '/about': '/og-about.png',
  '/about/ocean': '/og-about-ocean.png',
  '/contact': '/og-contact.png',
  '/terms': '/og-terms.png',
  '/privacy': '/og-privacy.png',
};

// Resolve the OG image for a given request path. Returns DEFAULT_OG_IMAGE
// when no override exists. Pure function; safe to call from middleware.
export function resolveOgImage(path: string): string {
  if (!path) return DEFAULT_OG_IMAGE;
  return PER_PAGE_OG_IMAGES[path] || DEFAULT_OG_IMAGE;
}
