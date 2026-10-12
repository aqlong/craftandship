# App Quality Checklist (8+ Rating)

Every generated app MUST hit these standards. Derived from an internal gold-reference event-site build (2026-04-07, rated 8/10).

## Auth & Security (8/10 baseline)

- [ ] bcrypt password hashing (cost factor 10+)
- [ ] CSRF hidden input in EVERY POST form (`<input type="hidden" name="_csrf" value="<%= csrfToken %>">`)
- [ ] Session-based CSRF validation middleware (imported from middleware/auth.js, NOT duplicated per route)
- [ ] Helmet with explicit CSP (script-src, style-src, font-src, img-src, frame-src, connect-src)
- [ ] `trust proxy` set for Railway reverse proxy
- [ ] Session cookies: httpOnly, secure in production, sameSite lax
- [ ] Rate limiting on auth endpoints (login, register, magic link)
- [ ] Email verification flow with token expiry (24hr)
- [ ] Magic link auth with SHA-256 hashed tokens, one-time use
- [ ] `req.session.save()` before every redirect
- [ ] POST-based logout with CSRF (never GET)
- [ ] Input trimming on all form handlers
- [ ] Parameterized SQL everywhere (no string concatenation)
- [ ] `data-confirm` attribute on all destructive actions (delete, reset)
- [ ] v1 only: the `TEST_MODE` login shortcut is gated on `NODE_ENV !== 'production'` in every emitted gate, and `server.js` exits at startup if `TEST_MODE` is set in production (`_harden_test_mode_gates`, `_ensure_test_mode_production_guard` in `cli/build.py`; the v2 engine emits no such shortcut)

## Database (7/10 baseline)

- [ ] Clean schema with proper types (SERIAL PKs, VARCHAR lengths, TIMESTAMP defaults)
- [ ] Foreign keys with ON DELETE CASCADE where appropriate
- [ ] Indexes on all frequently queried columns (email, token, date, status)
- [ ] UNIQUE constraints on natural keys (email, image_url)
- [ ] site_settings with key-value pattern and 60s in-memory cache
- [ ] ON CONFLICT DO NOTHING on all seed inserts
- [ ] Migrate.js only seeds when no users exist (idempotent)
- [ ] Minimum 15 realistic seed records per data table
- [ ] No placeholder text (no Lorem ipsum, no "Test item")

## UI/UX (9/10 baseline)

- [ ] Split-panel login/register with branded gradient left panel
- [ ] Glassmorphism nav with backdrop-filter blur and scroll shadow
- [ ] Premium card system (`card-premium`) with hover lift + shadow enhancement
- [ ] Stat cards with gradient top stripe on admin dashboard
- [ ] Hero sections with gradient backgrounds and pattern overlays
- [ ] Alternating section backgrounds (section-warm, section-earth) for visual rhythm
- [ ] Dual-font system: serif display font + sans-serif body font (Google Fonts)
- [ ] CSS custom properties for all brand colors (`var(--color-primary)`, etc.)
- [ ] Animation system: fadeInUp with staggered delays, hover lift on cards
- [ ] 44px minimum touch targets on ALL interactive elements (`min-h-[44px]`)
- [ ] Newsletter section with dark gradient background
- [ ] Footer with 3-column grid (brand, nav links, newsletter + social)
- [ ] Responsive grids that collapse to single column on mobile
- [ ] Mobile hamburger menu with full nav links
- [ ] Empty state patterns with icon + descriptive text
- [ ] Back navigation links ("Back to X") on detail/edit pages
- [ ] Form validation with inline error alerts (bg-red-50, role="alert")
- [ ] Image hover zoom effects on gallery cards
- [ ] `prefers-reduced-motion` respected in CSS

## Admin (8/10 baseline)

- [ ] Dashboard with clickable stat cards (each model gets a card)
- [ ] Quick action cards (New Event, New Photo, etc.)
- [ ] Recent activity panels on dashboard
- [ ] Admin tools: Change Password, CSV Import, Clear Data
- [ ] CSV import with table selection and column validation
- [ ] Site Settings page: business name, email, phone, address, social media URLs
- [ ] Settings link in admin dashboard
- [ ] Settings cache invalidation on save
- [ ] Admin-only middleware on all admin routes

## Contact & Communication (7/10 baseline)

- [ ] Contact form with validation (name, email, message required)
- [ ] Success redirect with green alert after submission
- [ ] Admin email notification via Resend API (fire-and-forget, console fallback)
- [ ] Newsletter subscription with duplicate checking
- [ ] Contact info from site_settings (email, phone, address)
- [ ] Social media links conditional on site_settings

## Media (8/10 baseline)

- [ ] Drag-and-drop image upload zone with progress bar
- [ ] Upload validation: file type check + 5MB size limit
- [ ] Auto-resize with sharp (1200px max, progressive JPEG quality 82)
- [ ] Base64 storage in uploads table (survives Railway redeploys)
- [ ] SVG placeholder fallback route for missing images
- [ ] YouTube video thumbnail proxy (ad-blocker safe, cached 24hr)
- [ ] Real stock photos (not placeholder images)

## Navigation (9/10 baseline)

- [ ] Sticky nav with glassmorphism blur effect
- [ ] Scroll shadow effect on nav (added via JS)
- [ ] Active nav link highlighting (`.nav-link-active`)
- [ ] Conditional Dashboard/Logout for authenticated users
- [ ] Footer with social media icons (conditional on site_settings)
- [ ] Copyright year auto-generated
- [ ] Brand logo + text in nav (text hidden on smallest screens)

## Error Handling (8/10 baseline)

- [ ] Dedicated 403, 404, 500 error page templates
- [ ] 404 uses premium design system with card and CTA
- [ ] Global error handler in server.js catches unhandled errors
- [ ] Every route has try/catch with console.error and 500 render
- [ ] Form validation re-renders with error messages (never blank 500s)
- [ ] parseInt validation on ID params with NaN check
- [ ] 404 on missing records (result.rows check before render)
- [ ] DB migration non-fatal (app starts even if migration fails)

## Performance (6/10 baseline: improvement target)

- [ ] Tailwind CSS compiled (postinstall build script)
- [ ] Site settings cached (60s TTL)
- [ ] Video thumbnail proxy cached (24hr)
- [ ] `.railwayignore` excludes node_modules and .git

## Accessibility (7/10 baseline)

- [ ] `lang="en"` on html element
- [ ] `aria-label` on all icon-only links/buttons
- [ ] 44px minimum touch targets
- [ ] `role="alert"` on success/error messages
- [ ] Focus-visible outline defined
- [ ] Form labels with `for` attributes linked to input IDs
- [ ] Semantic HTML (nav, main, footer, section, proper heading hierarchy)
- [ ] Print styles hide nav and footer

## SEO (6/10 baseline)

- [ ] Meta description on every page
- [ ] OG title and description tags
- [ ] robots.txt blocking /admin/ and /api/
- [ ] Semantic heading hierarchy (h1-h3)
- [ ] Descriptive page titles

## PWA (4/10 baseline)

- [ ] manifest.json with name, theme_color, icons
- [ ] Service worker registration
- [ ] SVG favicon

---

## Gaps to 10/10 (Pipeline Improvements Needed)

### P0: Must Fix
1. Add `compression` middleware (gzip responses)
2. Add `maxAge` on express.static for browser caching
3. Add `loading="lazy"` on gallery/media images
4. Fix canonical URL (dynamic, not hardcoded "/")
5. 500.ejs should use premium design system
6. Add double-submit prevention (global submit guard)
7. Use `<%= %>` not `<%- %>` for settings URLs (XSS prevention)
8. Add pagination on admin list pages
9. Import CSRF validation from middleware/auth.js (no duplication)

### P1: Should Add
10. Newsletter unsubscribe mechanism (GDPR)
11. Gallery lightbox/modal
12. Search on admin list pages
13. OG image meta tag
14. Schema.org structured data for events
15. sitemap.xml auto-generation
16. Mobile menu close-on-click
17. Skip-to-content link
18. aria-expanded on mobile toggle

### P2: Nice to Have
19. Dark mode (prefers-color-scheme)
20. Toast notification system
21. Image alt text from upload form
22. Event categories/tags
23. Admin dashboard charts
24. Bulk actions on lists
