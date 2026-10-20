/**
 * Harbor → compose translator.
 *
 * Converts the Opus-extracted spec from `services/spec_extraction.ts`
 * into a v1-shape spec that the v2 worker's `compose.intake.translate_v1_spec()`
 * accepts. **Pure TypeScript. No AI calls. Deterministic by construction.**
 *
 * Why this layer exists
 * =====================
 * Opus produces specs with rich CONTENT but ad-libbed STRUCTURE: pages
 * are arrays of { name, slug, sections: [{type, content}, ...] }, design
 * uses descriptive color names ("cream and oat"), data_models may be
 * partially named, archetypes may be guessed wrong. The v2 compose
 * pipeline expects:
 *
 *   - Either a v2-shape spec (pages as a dict keyed by page_id, each
 *     with archetype-specific blocks like `hero`, `offerings`,
 *     `testimonials`, etc.): this is what `scaffold_spec()` produces.
 *   - Or a v1-shape spec ({app_name, description, data_models,
 *     user_roles, _seed_settings, custom-pages-with-paths}) which the
 *     worker auto-detects + runs through `translate_v1_spec()`.
 *
 * We target **v1-shape** because:
 *   1. The worker's auto-detect step (the validate stage of `_run_pipeline`
 *      in queue/worker_pg.py, which sends a spec without an `archetype` key
 *      through `translate_v1_spec()`) does the v1 → v2 work for us, and that
 *      path is well-tested by all production CAS intake.
 *   2. v1-shape is a SMALLER target surface (1 description + a few
 *      _seed_settings keys + data_models + custom_pages) than v2-shape
 *      (full page-block dictionaries per archetype).
 *   3. Archetype lives in `_harbor_meta.archetype_hint`, NOT at the
 *      top level. The worker calls `detect_archetype()` which honors
 *      the hint as a strong signal when it is one of the 9 known
 *      archetype ids (the same enum spec_extraction's prompt restricts
 *      Opus to). This is a 2026-04-27 contract revision: previously the
 *      hint was advisory + the worker re-classified from text. Theo
 *      (a piano teacher) e2e showed text-only matching missed
 *      practitioners whose descriptions don't contain the right
 *      keywords; Opus reading 30+ memory rows + last 30 turns is a
 *      higher-quality signal than substring keyword match. The hint
 *      wins when valid; text-matching is the fallback when the hint
 *      is missing or unknown.
 *
 * Contract
 * ========
 * Input:  ExtractedSpec from services/spec_extraction.ts.
 * Output: V1ShapeSpec ready for INSERT INTO builds.spec; the worker's
 *         validate stage converts it to v2 via translate_v1_spec().
 *
 * Hard guarantees:
 *   - Output is deterministic given the same input. No timestamps, no
 *     random IDs, no Date.now().
 *   - Output never contains nested `pages[].sections[]`. All section
 *     content is hoisted to `_seed_settings` or normalized custom-pages.
 *   - Output never contains the top-level `archetype` key. Archetype
 *     is carried as `_harbor_meta.archetype_hint` and honored by the
 *     worker's `detect_archetype()` as a strong signal (2026-04-27).
 *   - Output's `_harbor_meta.services` + `_harbor_meta.testimonials`
 *     flow into the rendered home page via the worker's
 *     `translate_v1_spec` (2026-04-27): the worker maps
 *     `{title, description?, price?}` → compose offerings shape and
 *     `{quote, author?}` → compose testimonials shape, bypassing the
 *     archetype's executive-coaching defaults so a piano-teacher /
 *     tutor / instructor harbor build sees their own words.
 *   - Output preserves customer's exact words via `_seed_settings`.
 *   - Unknown section types produce warnings (returned alongside the
 *     translated spec) but do NOT block translation.
 *   - data_models without a name are dropped with a warning (compose
 *     can't emit schema without a name).
 *   - Empty or absent pages → archetype defaults take over downstream.
 *
 * Edge cases handled
 * ==================
 *   - Empty `pages` array        → uses archetype defaults
 *   - Empty `data_models`        → uses archetype defaults
 *   - data_model missing `name`  → dropped + warning
 *   - data_model missing `fields`→ ok (compose's `_default_fields` covers)
 *   - section type unknown       → dropped + warning
 *   - section content malformed  → dropped + warning
 *   - colors as english strings  → dropped (worker picks archetype palette default)
 *   - tier missing or unknown    → defaults to 'build'
 *   - app_name missing/empty     → "harbor-<short-id>"
 *   - admin_email missing        → null (caller fills it from session)
 *   - a spec that contradicts itself (a page marked auth_required at a path
 *     something else serves publicly, a public form that adds rows to a
 *     model visitors can read, a model on another model's table, a role
 *     name that cannot be stored): the build engine refuses such a spec, so
 *     each case is resolved here toward less exposure, with a warning (see
 *     `dropPagesTheEngineWouldRefuse`, `holdPublicFormRowsBack`,
 *     `dropCopiedPagesAtProtectedPaths`, `modelSlugProblem`,
 *     `normalizeAllowedRoles`, `engineOwnedFieldProblem`, `resolveModelLinks`,
 *     `nameVisitorFields`, `scrubPersonalStockPhotos`).
 *     compose/tests/test_translator_output_builds.py
 *     runs this file's output through the engine.
 *
 * Future-cast: when Haven's persona evolves to negotiate things like
 * booking calendars, intake forms, payment buttons, or recurring
 * services, the translator emits structured hints into the v1 spec
 * (e.g. a `BookingIntent` data_model, a v1 custom-page with `form_target`)
 * so the v2 compose engine has a deterministic place to look for them.
 * The actual rendering of those hints is compose's job; never ours.
 */

export type ArchetypeId =
  | 'healer-wellness'
  | 'coach-consultant'
  | 'therapist-counselor'
  | 'fitness-studio'
  | 'salon-spa'
  | 'restaurant-cafe'
  | 'tradesperson-contractor'
  | 'event-producer'
  | 'author'
  | 'local-service';

export const KNOWN_ARCHETYPES: ArchetypeId[] = [
  'healer-wellness',
  'coach-consultant',
  'therapist-counselor',
  'fitness-studio',
  'salon-spa',
  'restaurant-cafe',
  'tradesperson-contractor',
  'event-producer',
  'author',
  'local-service',
];

export type V1Tier = 'build' | 'grow' | 'scale';

export interface V1Field {
  name: string;
  type?: string;          // v1 type names: 'string' | 'long_text' | 'integer' | 'boolean' | 'date' | 'datetime' | 'image' | 'email' | 'phone' | 'url' | 'currency'
  sql_type?: string;
  required?: boolean;
  default?: string | number | boolean | null;
}

export interface V1DataModel {
  name: string;
  plural?: string;
  belongs_to?: string;
  fields?: V1Field[];
  public_listing_path?: string | null;
}

export interface V1UserRole {
  role: string;
  description?: string;
}

export interface V1CustomPage {
  /** Absolute path. Must start with '/'. */
  path: string;
  title?: string;
  purpose?: string;
  auth_required?: boolean;
  allowed_roles?: string[];
  form_target?: string;
  allowed_fields?: string[];
  submit_text?: string;
  success_message?: string;
}

export interface V1SeedSettings {
  site_name?: string;
  site_description?: string;
  hero_tagline?: string;
  hero_description?: string;
  hero_image_url?: string;
  about_title?: string;
  about_body?: string;
  about_photo_url?: string;
  contact_email?: string;
  contact_phone?: string;
  contact_address?: string;
  business_name?: string;
  nav_logo_url?: string;
  // Services-page hero overrides (added 2026-05-06). Page-level data,
  // NOT canonical site_settings keys. Both flow:
  //   _seed_settings -> translate_v1_spec answers -> scaffold_spec ->
  //   spec.pages.services.hero.{description, image_url}.
  // services_intro: optional paragraph replacing the generic
  //   "Every engagement is designed around what will actually move the
  //   needle." fallback. Empty/omitted falls through to default copy.
  // services_image_url: optional URL stamped onto the /services hero
  //   image_url; image_alt is auto-derived from site_name. Omitted means
  //   no image_url key on the hero (text-only).
  services_intro?: string;
  services_image_url?: string;
  // Social URLs all preserved; v1 uses a flat shape under _seed_settings.
  instagram_url?: string;
  facebook_url?: string;
  twitter_url?: string;
  linkedin_url?: string;
  youtube_url?: string;
  tiktok_url?: string;
}

export interface V1ShapeSpec {
  spec_version: string;
  app_name: string;
  description: string;
  admin_email?: string;
  tier: V1Tier;
  data_models?: V1DataModel[];
  user_roles?: V1UserRole[];
  pages?: V1CustomPage[];
  _seed_settings?: V1SeedSettings;
  /** Warnings from extraction + translation, surfaced to admin /reviews. */
  _extraction_warnings?: string[];
  _translation_warnings?: string[];
  /** Free-form metadata so future translators can add hints. */
  _harbor_meta?: {
    archetype_hint?: ArchetypeId;
    color_mood?: string;
    services?: Array<{ title: string; description?: string; price?: string }>;
    testimonials?: Array<{ quote: string; author?: string }>;
    booking_intents?: Array<{ label: string; duration_minutes?: number; price?: number }>;
    // ---- Design preferences (Phase 1, 2026-04-28) ----
    // All hints are advisory; compose/intake.py validates each against
    // its registry (palettes.json / font-pairs.json / page-layouts.json)
    // and drops unknowns with a _translation_warning. Hints carry no
    // built-in TS enum for the open-registry fields (palette, font_pair,
    // layout_variant) because the source of truth lives in the compose
    // data files, not in TS, and codegen drift would cost more than it
    // saves at this scale. Fields with closed value sets (density,
    // hero_style) ARE typed.
    palette_hint?: string;
    font_pair_hint?: string;
    layout_variant_hint?: string;
    density_hint?: 'airy' | 'standard' | 'dense';
    hero_style_hint?: 'centered' | 'split-image' | 'editorial' | 'image-led';
    /** Free-form mood words; lowercase, deduped, capped at 5 by translator. */
    mood_keywords?: string[];
    /** Anti-words: things customer explicitly said they do NOT want. Capped at 5. */
    avoid_keywords?: string[];
    /**
     * Fern e2e sample: when present, restricts the public nav
     * to exactly these absolute paths (`/` always implied for home; '/contact'
     * appended if absent). Closes the regression where archetype default
     * pages bloat the nav past what the keeper actually asked for.
     * Populated automatically from the keeper's pages array; admin can
     * override at dispatch time.
     */
    nav_paths?: string[];
    /**
     * Faithful-copy mode (v2-faithful-site-copy-mode, 2026-05-08).
     * When set, the keeper has an existing live website and wants the
     * rebuild to mirror it: verbatim text, same structure, same contact
     * mechanism. The translator + compose pipeline use copy_pages
     * (populated by the CRAWL stage in `services/site_copy_crawl.ts`) to
     * short-circuit archetype-default page composition. copy_source_url
     * is informational and audit-trail; copy_pages is the load-bearing
     * data.
     */
    copy_source_url?: string;
    copy_pages?: CopyPage[];
  };
}

/**
 * One page captured from a keeper's existing live website. The crawler
 * (services/site_copy_crawl.ts) populates this; compose/intake.py honors
 * it by overriding the archetype default page composition for any page
 * whose `path` matches.
 *
 * The contract is intentionally narrow:
 *   - `path` is absolute (starts with `/`, normalized lowercase). `/`
 *     means home. The compose layer maps `/about` → page_id `about`,
 *     `/services` → `services`, etc. Unknown paths land as custom_pages.
 *   - `markdown_blocks` is an ordered list of self-contained markdown
 *     strings (paragraphs, headings, lists, blockquotes). The compose
 *     pipeline renders them through a small allow-listed renderer
 *     (compose/markdown_safe.py) into HTML. Em dashes are preserved
 *     verbatim (brand-voice carve-out for source-imported text).
 *   - `hero` is optional. When present it overrides the archetype hero
 *     for that page. The renderer maps:
 *       - `heading`              → hero H1 (verbatim, no italic-accent
 *                                   substitution; we do not invent {word}
 *                                   markers in customer copy)
 *       - `body_html`            → multi-paragraph hero body. It is HTML,
 *                                   not markdown, and the widget emits it
 *                                   raw, so the engine reduces it to an
 *                                   attribute-free tag allowlist first
 *                                   (`sanitize_html_fragment` in
 *                                   compose/markdown_safe.py: p, br, em,
 *                                   strong, i, b, ul, ol, li, blockquote;
 *                                   everything else stays escaped text)
 *       - `credential_lines`     → bullet stack rendered under the
 *                                   heading (the "LMT, RYT-500" line
 *                                   that healers carry just below their
 *                                   name)
 *       - `cta_text_me_phone`    → digits-only US phone string; widget
 *                                   emits `<a href="sms:NNN..."` button
 *       - `cta_text` / `cta_href`→ generic primary CTA fallback when no
 *                                   sms link is captured
 *
 * Brand-voice em-dash carve-out: text imported verbatim from a source
 * URL is exempt from the no-em-dash rule. Em dashes inside any
 * `markdown_blocks` entry, hero `body_html`, or `credential_lines` are
 * preserved exactly as the customer wrote them. The CAS internal lint
 * (scripts/check_brand_voice.py) only scans CAS source, never customer
 * site output, so the carve-out is implicit at the emission boundary.
 */
export interface CopyPage {
  path: string;
  title?: string;
  markdown_blocks: string[];
  hero?: {
    heading?: string;
    body_html?: string;
    credential_lines?: string[];
    cta_text_me_phone?: string;
    cta_text?: string;
    cta_href?: string;
  };
}

export interface ExtractedSpecInput {
  spec_version?: string;
  app_name?: string;
  business_type?: string;
  archetype?: string;
  tier?: string;
  admin_email?: string;
  pages?: Array<Record<string, unknown>>;
  data_models?: Array<Record<string, unknown>>;
  user_roles?: Array<Record<string, unknown>>;
  design?: Record<string, unknown>;
  copy?: Record<string, unknown>;
  _extraction_warnings?: string[];
  /** Faithful-copy mode (v2-faithful-site-copy-mode). Either field
   *  populated by the admin/dispatch path AFTER running the crawler in
   *  `services/site_copy_crawl.ts`. The translator forwards both to
   *  `_harbor_meta` after light validation. */
  copy_source_url?: string;
  copy_pages?: Array<Record<string, unknown>>;
  [key: string]: unknown;
}

export interface TranslateResult {
  spec: V1ShapeSpec;
  warnings: string[];
}

const TIERS: V1Tier[] = ['build', 'grow', 'scale'];

/**
 * Map Opus's free-form section type names to handler ids. Unknown types
 * map to 'unknown' which is dropped with a warning.
 */
// Section type aliases: keys are normalized to lowercase + dashes
// converted to underscores (see normalizeSectionType below). So an
// Opus-emitted "quick-nav" matches "quick_nav" here, "Photo-Gallery"
// matches "photo_gallery", etc. This was the root cause of the
// sample-site build failure: Opus emitted dash-separated
// type names but the original lookup only matched literal keys.
const SECTION_TYPE_ALIASES: Record<string, string> = {
  hero: 'hero',
  banner: 'hero',
  intro: 'hero',
  // Quick-nav / featured-events / etc. are home-page convenience
  // sections in Opus's lexicon; map to cta since the home layout's
  // built-in cta_block carries the same intent (top-of-page link bar).
  quick_nav: 'cta',
  cta: 'cta',
  call_to_action: 'cta',
  call_to_actions: 'cta',
  about: 'about',
  about_bio: 'about',
  about_body: 'about',
  about_intro: 'about',
  about_section: 'about',
  bio: 'about',
  story: 'about',
  offerings: 'offerings',
  services: 'offerings',
  service_list: 'offerings',
  classes: 'offerings',
  menu: 'offerings',
  treatments: 'offerings',
  // Healer-wellness offerings noun. An end-to-end run (2026-05-01) had Opus
  // emit `sessions_list` + `sessions_overview` for an offerings-style list
  // of sessions; without these aliases the keeper's prices dropped and
  // `_harbor_meta.services` stayed empty (seen on the Fern & Stone
  // end-to-end sample).
  sessions: 'offerings',
  sessions_list: 'offerings',
  sessions_overview: 'offerings',
  session_list: 'offerings',
  session_overview: 'offerings',
  testimonials: 'testimonials',
  testimonial: 'testimonials',
  testimonials_carousel: 'testimonials',
  testimonial_section: 'testimonials',
  testimonials_section: 'testimonials',
  client_quotes: 'testimonials',
  quotes: 'testimonials',
  reviews: 'testimonials',
  review_list: 'testimonials',
  press: 'testimonials',
  gallery: 'gallery',
  photos: 'gallery',
  photo: 'gallery',
  photo_gallery: 'gallery',
  media_gallery: 'gallery',
  // Captured model output for the Fern & Stone sample emitted `gallery-grid` for the same gallery
  // widget (different layout hint, same content). Maps to gallery.
  gallery_grid: 'gallery',
  photo_grid: 'gallery',
  newsletter: 'newsletter',
  email_signup: 'newsletter',
  faq: 'faq',
  faqs: 'faq',
  questions: 'faq',
  visit: 'visit',
  hours: 'visit',
  location: 'visit',
  // Captured model output for the Fern & Stone sample emitted `map` for the contact-page location
  // block; maps to the same visit/location widget.
  map: 'visit',
  directions: 'visit',
  contact: 'contact',
  contact_form: 'contact',
  form: 'contact',
  pricing: 'pricing',
  prices: 'pricing',
  team: 'team',
  staff: 'team',
  instructors: 'team',
  practitioners: 'team',
  service_areas: 'service_areas',
  areas: 'service_areas',
  coverage: 'service_areas',
  upcoming_events: 'upcoming_events',
  events: 'upcoming_events',
  featured_events: 'upcoming_events',
  event_calendar: 'upcoming_events',
  event_list: 'upcoming_events',
  past_events: 'past_events',
  trust: 'trust',
  trust_badges: 'trust',
  badges: 'trust',
  booking: 'booking',
  book: 'booking',
  schedule: 'booking',
  calendar: 'booking',
  intake: 'intake_form',
  intake_form: 'intake_form',
  payment: 'payment',
  checkout: 'payment',
  // 2026-04-28 (end-to-end healer test): 8 section types Opus
  // emitted that got dropped because no alias existed. Each maps to the
  // closest existing widget so the keeper's words flow into the build
  // instead of falling back to archetype defaults.
  // - "overview" / "studio_snapshot" are both "this is the place" intro
  //   blocks; map to about (the bio/story widget) since the keeper is
  //   describing herself + her practice setting.
  overview: 'about',
  studio_snapshot: 'about',
  // - "credentials" is a credentials list; the about widget renders
  //   credentials inline alongside bio. Map there.
  credentials: 'about',
  certifications: 'about',
  qualifications: 'about',
  // - "what_to_expect" / "first_timer" / "preparation" is the
  //   first-timer guidance Opus collects from the keeper (drink water,
  //   wear comfortable clothes). The faq widget renders the same
  //   shape; map there.
  what_to_expect: 'faq',
  first_timer: 'faq',
  preparation: 'faq',
  // - "studio_details" is the sensory descriptors of a space (for example
  //   "wood floors, soft lighting"). About widget carries this naturally.
  studio_details: 'about',
  // - "description" is a generic "describe this thing" block; about.
  description: 'about',
  // - "signup" is a generic call-to-action for newsletter or booking.
  //   When emitted standalone, default to newsletter (email capture)
  //   since signup-style sections without a target usually mean "get
  //   on the list."
  signup: 'newsletter',
  signup_form: 'newsletter',
};

// Section types that map cleanly to a "drop with explanation" outcome:
// they're recognized concepts the keeper might mention but compose
// doesn't have a widget for, OR they're page-level concepts that don't
// fit the section-on-a-page model. Still drop, but with a more useful
// warning than "unknown section type".
const SECTION_TYPE_EXPLAINED_DROPS: Record<string, string> = {
  // Page-level concepts, not section-level. The page itself (e.g.
  // /events/:event-slug) handles the detail render via compose's
  // archetype default route layouts.
  event_detail: 'event_detail is a page-level concept; render handled by archetype routes',
  // No compose widget exists for video embeds yet. Drop for now;
  // compose's gallery widget covers photos. Future-cast: emit a
  // VideoEmbed data model + a video-grid widget.
  video_gallery: 'video_gallery widget not yet supported in compose; consider gallery widget for stills',
  videos: 'video gallery widget not yet supported in compose',
};

/**
 * Normalize Opus's section type strings to the canonical alias-map key.
 * Lowercases + converts dashes to underscores, so "quick-nav" ->
 * "quick_nav" matches the alias entry. Strips leading/trailing
 * whitespace as belt-and-suspenders against trailing newlines or stray
 * spaces from the LLM output.
 *
 * Also strips a trailing `_preview` or `_teaser` suffix (Opus often
 * emits home-page sections as `offerings_preview`, `gallery_preview`,
 * `events_preview` to distinguish them from the canonical full-page
 * sections of the same kind). The widget that renders the home-page
 * teaser is the same widget that renders the full-page section, so
 * mapping `<x>_preview` -> `<x>` lets the keeper's content flow into
 * the build instead of dropping with a warning. Closes the empirical
 * root cause seen on the Fern & Stone end-to-end sample: the captured model
 * output included `offerings_preview` + `gallery_preview` sections
 * which dropped as unknown, leaving `_harbor_meta.services` empty.
 */
export function normalizeSectionType(rawType: string): string {
  const base = rawType.toLowerCase().trim().replace(/-/g, '_');
  // Strip `_preview` / `_teaser` suffix so `offerings_preview` lands on
  // `offerings` (which is in SECTION_TYPE_ALIASES). Defensive against the
  // very unlikely case that a section's actual canonical name ends in
  // `_preview` (none in the current maps); add an exemption later if a
  // real conflict surfaces.
  return base.replace(/_(preview|teaser)$/, '');
}

const FUTURE_FRIENDLY_DATA_MODELS: Record<string, V1DataModel> = {
  // When Opus mentions booking/scheduling and no archetype default
  // captures it, emit a BookingIntent data model so admin can wire it
  // up post-launch. Compose's _default_fields handles unspecified columns.
  BookingIntent: {
    name: 'BookingIntent',
    plural: 'booking-intents',
    public_listing_path: null,
    fields: [
      { name: 'requester_name', type: 'string', required: true },
      { name: 'requester_email', type: 'email', required: true },
      { name: 'requester_phone', type: 'string' },
      { name: 'preferred_window', type: 'string' },
      { name: 'notes', type: 'long_text' },
      { name: 'status', type: 'string', default: 'pending' },
    ],
  },
  IntakeSubmission: {
    name: 'IntakeSubmission',
    plural: 'intake-submissions',
    public_listing_path: null,
    fields: [
      { name: 'submitter_name', type: 'string', required: true },
      { name: 'submitter_email', type: 'email', required: true },
      { name: 'payload', type: 'long_text' },
    ],
  },
};

/**
 * Pure entry point. Caller (lighthouse-build route) takes the result.spec
 * and INSERTs it into builds.spec; result.warnings goes to the response
 * + admin /reviews surface.
 *
 * Throws on inputs that can't be reasonably translated (missing both
 * app_name + business_type, completely empty input). Otherwise produces
 * a v1-shape spec + a warning list.
 */
export function translateExtractedSpecToV1(
  input: ExtractedSpecInput
): TranslateResult {
  if (!input || typeof input !== 'object') {
    throw new Error('translateExtractedSpecToV1: input must be an object');
  }

  const warnings: string[] = [];

  // ---- App name ----
  const appName = sanitizeAppName(input.app_name);
  if (!appName) {
    warnings.push('app_name missing or invalid; falling back to harbor- prefix');
  }

  // ---- Tier ----
  const tier: V1Tier = TIERS.includes(input.tier as V1Tier)
    ? (input.tier as V1Tier)
    : 'build';
  if (input.tier && !TIERS.includes(input.tier as V1Tier)) {
    warnings.push(`unknown tier "${input.tier}"; defaulted to "build"`);
  }

  // ---- Archetype hint ----
  const archetypeHint: ArchetypeId | undefined = KNOWN_ARCHETYPES.includes(
    input.archetype as ArchetypeId
  )
    ? (input.archetype as ArchetypeId)
    : undefined;
  if (input.archetype && !archetypeHint) {
    warnings.push(
      `unknown archetype "${input.archetype}"; worker will re-classify from description`
    );
  }

  // ---- Description (drives detect_archetype + hero fallback) ----
  const description = buildDescription(input, archetypeHint);

  // ---- Seed settings (customer's exact words) ----
  const { seedSettings, services, testimonials, bookingIntents, sectionWarnings } =
    extractFromPagesAndCopy(input);
  warnings.push(...sectionWarnings);

  // ---- Data models ----
  const { dataModels, dataModelWarnings } = normalizeDataModels(
    input.data_models,
    bookingIntents.length > 0
  );
  warnings.push(...dataModelWarnings);

  // ---- User roles ----
  const userRoles = normalizeUserRoles(input.user_roles);

  // ---- Custom pages ----
  const customPages = extractCustomPages(input.pages, dataModels, warnings);

  // ---- Public booking form (alignment fix, 2026-04-29) ----
  // When the keeper's conversation surfaced booking intent (a "booking"
  // section was emitted), the translator already added a BookingIntent
  // data_model. Without this block, BookingIntent shows up as an admin
  // tab the keeper can manually log requests into, but no public form
  // exists for visitors to submit. That's an admin/public mismatch the
  // flagged in review.
  //
  // Fix: inject a custom_page at /book-a-session with form_target=
  // BookingIntent. Compose's _custom_pages_filter + form_target plumbing
  // (compose/build.py) emits a public GET (renders a form) + POST
  // (validates + INSERTs into booking_intents). The same machinery
  // already powers /contact for ContactInquiry; we are just declaring
  // the page so it gets the same treatment.
  //
  // Skip if the keeper's spec already declared a /book-a-session,
  // /book, or any page with form_target=BookingIntent. We don't want
  // to duplicate when Opus emitted one explicitly.
  const hasBookingPage = customPages.some(
    (p) =>
      p.path === '/book-a-session' ||
      p.path === '/book' ||
      p.form_target === 'BookingIntent'
  );
  const hasBookingModel = dataModels.some((m) => m.name === 'BookingIntent');
  if (bookingIntents.length > 0 && hasBookingModel && !hasBookingPage) {
    customPages.push({
      path: '/book-a-session',
      title: 'Book a session',
      purpose:
        "A simple form so visitors can request a session. Lands in admin's Booking Intents.",
      form_target: 'BookingIntent',
      submit_text: 'Send my request',
      success_message:
        'Thank you. Your request landed; we will reach out to confirm a time.',
    });
  }

  // ---- Rows visitors submit are held back ----
  // Runs after every form page exists (the one injected above included).
  holdPublicFormRowsBack(customPages, dataModels, warnings);

  // ---- Columns a visitor may fill in ----
  // Runs after the rows are held back (it may have added the `published`
  // field) and names, on each public form page, the columns of its model.
  nameVisitorFields(customPages, dataModels, warnings);

  // ---- Final spec ----
  const spec: V1ShapeSpec = {
    spec_version: '1.0',
    app_name: appName ?? `harbor-${shortId()}`,
    description,
    tier,
  };
  if (input.admin_email && typeof input.admin_email === 'string') {
    // The engine refuses a spec whose admin_email is not a plain address, so
    // an extracted value that is not one is left out instead of failing the
    // build. (The build worker writes the customer's account email into
    // admin_email before it runs the engine in any case.)
    const adminEmail = input.admin_email.trim().toLowerCase();
    if (isEngineSafeAdminEmail(adminEmail)) spec.admin_email = adminEmail;
    else warnings.push('admin_email dropped (not a plain email address)');
  }
  if (dataModels.length > 0) spec.data_models = dataModels;
  if (userRoles.length > 0) spec.user_roles = userRoles;
  if (customPages.length > 0) spec.pages = customPages;
  if (Object.keys(seedSettings).length > 0) spec._seed_settings = seedSettings;
  if (Array.isArray(input._extraction_warnings) && input._extraction_warnings.length > 0) {
    spec._extraction_warnings = input._extraction_warnings.slice(0, 50);
  }
  if (warnings.length > 0) spec._translation_warnings = warnings;

  const meta: NonNullable<V1ShapeSpec['_harbor_meta']> = {};
  if (archetypeHint) meta.archetype_hint = archetypeHint;
  const colorMood = readColorMood(input.design);
  if (colorMood) meta.color_mood = colorMood;
  if (services.length > 0) meta.services = services;
  if (testimonials.length > 0) meta.testimonials = testimonials;
  if (bookingIntents.length > 0) meta.booking_intents = bookingIntents;
  // Design preferences (Phase 1): normalized + capped here; Python
  // re-validates against compose/data registries and drops unknowns.
  const design = readObject(input.design);
  const paletteHint = normalizeFreeStringHint(design.palette_hint);
  if (paletteHint) meta.palette_hint = paletteHint;
  const fontPairHint = normalizeFreeStringHint(design.font_pair_hint);
  if (fontPairHint) meta.font_pair_hint = fontPairHint;
  const layoutVariantHint = normalizeFreeStringHint(design.layout_variant_hint);
  if (layoutVariantHint) meta.layout_variant_hint = layoutVariantHint;
  const densityHint = normalizeDensityHint(design.density_hint);
  if (densityHint) meta.density_hint = densityHint;
  const heroStyleHint = normalizeHeroStyleHint(design.hero_style_hint);
  if (heroStyleHint) meta.hero_style_hint = heroStyleHint;
  const moodKeywords = normalizeKeywordList(design.mood_keywords);
  if (moodKeywords.length > 0) meta.mood_keywords = moodKeywords;
  const avoidKeywords = normalizeKeywordList(design.avoid_keywords);
  if (avoidKeywords.length > 0) meta.avoid_keywords = avoidKeywords;
  // Fern e2e sample: when keeper declared explicit pages,
  // populate nav_paths with their absolute paths so the build-time nav
  // emitter restricts the archetype default_pages to keeper intent.
  // Derived from raw input.pages (NOT customPages; that's filtered to
  // non-archetype-default slugs only; keeper's archetype-overlapping
  // pages like /about /journal /contact still belong in nav_paths).
  // Build always re-implies `/` and `/contact`.
  if (Array.isArray(input.pages) && input.pages.length > 0) {
    const navPaths: string[] = [];
    const seen = new Set<string>();
    for (const p of input.pages) {
      if (!p || typeof p !== 'object') continue;
      const o = p as Record<string, unknown>;
      const raw = String(o.slug ?? o.path ?? o.name ?? '').toLowerCase().trim();
      if (!raw) continue;
      const slug = stripSurroundingSlashes(raw);
      if (!slug || slug.includes(':')) continue;
      const abs = slug === 'home' ? '/' : `/${slug}`;
      if (!seen.has(abs)) {
        navPaths.push(abs);
        seen.add(abs);
      }
    }
    if (navPaths.length > 0) meta.nav_paths = navPaths;
  }

  // Faithful-copy mode (v2-faithful-site-copy-mode, 2026-05-08).
  // copy_source_url + copy_pages are populated upstream by the CRAWL
  // stage. We validate shape and forward; compose/intake.py honors
  // copy_pages by short-circuiting archetype default page composition
  // for any page whose normalized path matches.
  const copySourceUrl = normalizeCopySourceUrl(input.copy_source_url);
  if (copySourceUrl) meta.copy_source_url = copySourceUrl;
  const copyPages = dropCopiedPagesAtProtectedPaths(
    normalizeCopyPages(input.copy_pages, warnings),
    customPages,
    warnings
  );
  if (copyPages.length > 0) meta.copy_pages = copyPages;
  // The copied pages are read after the spec was assembled, so a warning
  // about one of them may be the first warning there is.
  if (warnings.length > 0) spec._translation_warnings = warnings;

  if (Object.keys(meta).length > 0) spec._harbor_meta = meta;

  // Last, over the whole spec: nothing in it names a stock photo the engine
  // would refuse. Its warnings are added to the list the spec carries.
  const scrubbed: string[] = [];
  scrubPersonalStockPhotos(spec, scrubbed);
  if (scrubbed.length > 0) {
    warnings.push(...scrubbed);
    spec._translation_warnings = warnings;
  }

  return { spec, warnings };
}

// ============================================================
// Helpers
// ============================================================

// Slash trimming and query cutting, written as loops. The values they run on
// (page slugs, listing paths, copied-page paths) come from the model's spec
// output and from an admin's dispatch overrides, and nothing bounds their
// length before this point. The regular expressions these replace
// (`/\/+$/`, `/^\/+|\/+$/g`, `/[?#].*$/`) rescan the rest of a long run of
// slashes, or of a long line, from each of its positions when the run does
// not reach the end of the string: time proportional to the square of the
// length. Each loop below reads a character at most twice.

const CODE_SLASH = 0x2f;

/** `value` without the "/" characters at its end. Same result as `value.replace(/\/+$/, '')`. */
function stripTrailingSlashes(value: string): string {
  let end = value.length;
  while (end > 0 && value.charCodeAt(end - 1) === CODE_SLASH) end -= 1;
  return value.slice(0, end);
}

/** `value` without the "/" characters at its start and end. Same result as `value.replace(/^\/+|\/+$/g, '')`. */
function stripSurroundingSlashes(value: string): string {
  let start = 0;
  let end = value.length;
  while (start < end && value.charCodeAt(start) === CODE_SLASH) start += 1;
  while (end > start && value.charCodeAt(end - 1) === CODE_SLASH) end -= 1;
  return value.slice(start, end);
}

function isLineBreak(code: number): boolean {
  return code === 0x0a || code === 0x0d || code === 0x2028 || code === 0x2029;
}

/**
 * `value` cut at its query string or fragment. Same result as
 * `value.replace(/[?#].*$/, '')`: the "." there does not cross a line break,
 * so the cut is at the first "?" or "#" that comes after the last line break
 * (a value with a line break in it is refused by the caller's own check
 * afterwards; this only keeps the two in step).
 */
function stripQueryAndFragment(value: string): string {
  let lineStart = value.length;
  while (lineStart > 0 && !isLineBreak(value.charCodeAt(lineStart - 1))) lineStart -= 1;
  for (let index = lineStart; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    if (code === 0x3f || code === 0x23) return value.slice(0, index);
  }
  return value;
}

// The engine requires app_name to match [a-z][a-z0-9-]{0,62} (`_APP_NAME_RE`
// in compose/build.py) and refuses the build otherwise. A name that slugs to
// something starting with a digit ("9 Sample Studio") is therefore prefixed
// rather than passed through. So is a name that starts with the platform's
// reserved prefix (`_RESERVED_APP_NAME_PREFIX` in compose/build.py): the app
// name becomes the site's Railway project name, and the platform refuses to
// delete any project named with that prefix, so the engine refuses it too.
const RESERVED_APP_NAME_PREFIX = 'craftandship';

function sanitizeAppName(raw: unknown): string | null {
  if (typeof raw !== 'string') return null;
  const trimmed = raw.trim().toLowerCase();
  if (!trimmed) return null;
  const slug = trimmed.replace(/[^a-z0-9-]/g, '-').replace(/-{2,}/g, '-').replace(/^-|-$/g, '');
  if (!slug || slug.length < 2) return null;
  const usable = /^[a-z]/.test(slug) && !slug.startsWith(RESERVED_APP_NAME_PREFIX);
  return (usable ? slug : `site-${slug}`).slice(0, 60);
}

/** Engine: `_ADMIN_EMAIL_RE` (plus its 254 character limit). */
const ENGINE_ADMIN_EMAIL_RE = /^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){1,8}$/;

/** True when the build engine will accept this as `admin_email`. Exported for tests. */
export function isEngineSafeAdminEmail(value: string): boolean {
  return value.length <= 254 && ENGINE_ADMIN_EMAIL_RE.test(value);
}

function shortId(): string {
  // Deterministic placeholder; caller should override app_name when
  // a real one is available. We deliberately don't use Date.now or
  // random so tests stay deterministic.
  return 'unnamed';
}

function buildDescription(
  input: ExtractedSpecInput,
  archetypeHint: ArchetypeId | undefined
): string {
  // detect_archetype reads description text + data_model names + action
  // names + page slugs to classify. We seed it with business_type +
  // about_body + archetype hint so the classifier has good signal.
  const parts: string[] = [];
  if (typeof input.business_type === 'string' && input.business_type.trim()) {
    parts.push(input.business_type.trim());
  }
  const copy = readObject(input.copy);
  const aboutBody = readString(copy.about_body);
  if (aboutBody) parts.push(aboutBody);
  const heroSubtext = readString(copy.hero_subtext);
  if (heroSubtext && !aboutBody) parts.push(heroSubtext);
  if (archetypeHint && parts.length === 0) {
    parts.push(archetypeHint.replace('-', ' ') + ' practice');
  }
  if (parts.length === 0) {
    return 'Harbor keeper site';
  }
  // Cap at ~600 chars; detect_archetype + the hero description fallback
  // both sample from this string.
  return parts.join('. ').slice(0, 600);
}

interface PageExtractResult {
  seedSettings: V1SeedSettings;
  services: Array<{ title: string; description?: string; price?: string }>;
  testimonials: Array<{ quote: string; author?: string }>;
  bookingIntents: Array<{ label: string; duration_minutes?: number; price?: number }>;
  sectionWarnings: string[];
}

/**
 * Walk Opus's `pages[].sections[]` + top-level `copy` and hoist content
 * into a flat _seed_settings dict. Return services/testimonials lists
 * separately so they can land under archetype defaults.
 */
function extractFromPagesAndCopy(input: ExtractedSpecInput): PageExtractResult {
  const seedSettings: V1SeedSettings = {};
  const services: PageExtractResult['services'] = [];
  const testimonials: PageExtractResult['testimonials'] = [];
  const bookingIntents: PageExtractResult['bookingIntents'] = [];
  const sectionWarnings: string[] = [];

  // Counters for the barren-spec guard. If most/all sections drop the
  // worker should know: a barren spec produces a build that lints
  // green but crashes post-deploy (root cause of the sample-site crash).
  let sectionsSeen = 0;
  let sectionsHandled = 0;

  // Top-level copy block (preferred source).
  const copy = readObject(input.copy);
  copyStringInto(seedSettings, 'hero_tagline', copy.hero_tagline);
  copyStringInto(seedSettings, 'hero_description', copy.hero_subtext);
  if (!seedSettings.hero_description) {
    copyStringInto(seedSettings, 'hero_description', copy.hero_description);
  }
  copyStringInto(seedSettings, 'about_body', copy.about_body);
  copyStringInto(seedSettings, 'about_title', copy.about_title);
  copyStringInto(seedSettings, 'site_name', copy.site_name);

  const pages = Array.isArray(input.pages) ? input.pages : [];
  for (const page of pages) {
    if (!page || typeof page !== 'object') {
      sectionWarnings.push(`page entry dropped: not an object`);
      continue;
    }
    const sections = readArray((page as Record<string, unknown>).sections);
    if (sections.length === 0) continue;
    for (const section of sections) {
      sectionsSeen++;
      if (!section || typeof section !== 'object') {
        sectionWarnings.push(`section entry dropped on page "${readString((page as Record<string, unknown>).slug) || 'unknown'}": not an object`);
        continue;
      }
      const rawType = readString((section as Record<string, unknown>).type);
      const normalized = normalizeSectionType(rawType);
      const explainedDrop = SECTION_TYPE_EXPLAINED_DROPS[normalized];
      const handlerKey = SECTION_TYPE_ALIASES[normalized] ?? (explainedDrop ? 'explained_drop' : 'unknown');
      const content = readObject((section as Record<string, unknown>).content);
      const pageSlug = readString((page as Record<string, unknown>).slug) || 'unknown';

      switch (handlerKey) {
        case 'hero': {
          sectionsHandled++;
          copyStringInto(seedSettings, 'hero_tagline', content.tagline ?? content.heading ?? content.title);
          copyStringInto(seedSettings, 'hero_description', content.subtext ?? content.description ?? content.body);
          // image_description -> deferred (compose seeds default photos)
          break;
        }
        case 'about': {
          sectionsHandled++;
          copyStringInto(seedSettings, 'about_body', content.body ?? content.text ?? content.content);
          copyStringInto(seedSettings, 'about_title', content.heading ?? content.title);
          break;
        }
        case 'cta': {
          sectionsHandled++;
          // CTA is rendered by every archetype's home layout via the
          // built-in cta_block. Nothing to hoist; drop silently.
          break;
        }
        case 'offerings': {
          sectionsHandled++;
          const items = readArray(content.items ?? content.list ?? content.entries);
          for (const it of items) {
            const obj = readObject(it);
            const title = readString(obj.title ?? obj.name ?? obj.label);
            if (!title) continue;
            services.push({
              title,
              description: readString(obj.description ?? obj.details) || undefined,
              price: readString(obj.price ?? obj.amount) || undefined,
            });
          }
          break;
        }
        case 'testimonials': {
          sectionsHandled++;
          const items = readArray(content.items ?? content.quotes ?? content.list);
          for (const it of items) {
            const obj = readObject(it);
            const quote = readString(obj.quote ?? obj.text ?? obj.body);
            if (!quote) continue;
            testimonials.push({
              quote,
              author: readString(obj.author ?? obj.name) || undefined,
            });
          }
          break;
        }
        case 'visit': {
          sectionsHandled++;
          copyStringInto(seedSettings, 'contact_address', content.address);
          copyStringInto(seedSettings, 'contact_phone', content.phone);
          break;
        }
        case 'contact': {
          sectionsHandled++;
          copyStringInto(seedSettings, 'contact_email', content.email ?? content.contact_email);
          copyStringInto(seedSettings, 'contact_phone', content.phone);
          break;
        }
        case 'booking': {
          sectionsHandled++;
          // Rich booking content: capture each path/option as a
          // BookingIntent so admin can wire calendar later.
          const paths = readArray(content.paths ?? content.options ?? content.tiers);
          if (paths.length > 0) {
            for (const p of paths) {
              const obj = readObject(p);
              const label = readString(obj.label ?? obj.name);
              if (!label) continue;
              const intent: { label: string; duration_minutes?: number; price?: number } = { label };
              const durationRaw = obj.duration_minutes ?? obj.duration;
              if (typeof durationRaw === 'number' && Number.isFinite(durationRaw)) {
                intent.duration_minutes = Math.round(durationRaw);
              }
              const priceRaw = obj.price ?? obj.amount ?? obj.cost;
              if (typeof priceRaw === 'number' && Number.isFinite(priceRaw)) {
                intent.price = priceRaw;
              }
              bookingIntents.push(intent);
            }
          } else {
            const label = readString(content.heading ?? content.label) || 'Book a session';
            bookingIntents.push({ label });
          }
          sectionWarnings.push(
            `page "${pageSlug}": booking section captured as BookingIntent data_model; calendar/payment integration is post-launch admin wiring`
          );
          break;
        }
        case 'intake_form': {
          sectionsHandled++;
          sectionWarnings.push(
            `page "${pageSlug}": intake form captured as IntakeSubmission data_model; admin wires the actual fields post-launch`
          );
          break;
        }
        case 'payment': {
          sectionsHandled++;
          sectionWarnings.push(
            `page "${pageSlug}": payment section dropped; CAS Harbor doesn't process customer payments. Route customers to existing payment platforms.`
          );
          break;
        }
        case 'gallery':
        case 'newsletter':
        case 'faq':
        case 'pricing':
        case 'team':
        case 'service_areas':
        case 'upcoming_events':
        case 'past_events':
        case 'trust': {
          sectionsHandled++;
          // These are all archetype-handled via default page layouts +
          // default data_models. Customer's specific items come from
          // the archetype seed (compose/intake.py:_default_services /
          // _default_testimonials etc). Nothing to hoist here.
          break;
        }
        case 'explained_drop': {
          // Recognized concept the keeper may have mentioned, but
          // compose has no widget for it OR it's a page-level idea
          // that doesn't fit the section-on-a-page model. Drop with
          // a useful explanation rather than the generic "unknown".
          // Counted as handled since the spec wasn't actually barren; 
          // we just chose not to map this concept onto a widget.
          sectionsHandled++;
          sectionWarnings.push(
            `page "${pageSlug}": ${explainedDrop} (raw type: "${rawType}")`
          );
          break;
        }
        case 'unknown':
        default: {
          sectionWarnings.push(
            `page "${pageSlug}": unknown section type "${rawType}" dropped`
          );
          break;
        }
      }
    }
  }

  // Barren-spec guard. If the keeper had real content to hoist (Opus
  // emitted sections) but every section dropped, the resulting build
  // will have empty pages backed only by archetype defaults. Compose's
  // v2 lint will pass on the thin spec but the deployed app may crash
  // on first request (root cause of the sample-site crash). Surface a
  // loud warning so admin/dispatch can refuse-or-flag before queueing.
  // Note: a spec with NO sections at all (empty pages array, no
  // sections) is fine: compose's archetype defaults are the entire
  // story. The barren-spec case is "Opus emitted sections AND we
  // dropped them all," not "no sections to begin with."
  if (sectionsSeen > 0 && sectionsHandled === 0) {
    sectionWarnings.push(
      `BARREN_SPEC: ${sectionsSeen} sections seen but 0 handled; build will have empty pages backed only by archetype defaults and may crash post-deploy. Investigate the dropped section types above.`
    );
  } else if (sectionsSeen > 4 && sectionsHandled / sectionsSeen < 0.25) {
    sectionWarnings.push(
      `THIN_SPEC: ${sectionsHandled}/${sectionsSeen} sections handled (< 25%); build may be missing significant content.`
    );
  }

  // Dedup services + testimonials by their identifying text. Opus often emits
  // the same offering across multiple sections (e.g. an "offerings" section AND
  // a "/services" page), so the same service gets pushed twice; the public site
  // then renders duplicate cards (observed on a real build: 7 cards for 4
  // services). Keep the first occurrence (richest, earliest) by case-insensitive
  // title; merge a missing price/description from a later duplicate.
  const dedupedServices = dedupeByKey(
    services,
    (s) => (s.title || '').trim().toLowerCase(),
    (first, dup) => ({
      ...first,
      price: first.price || dup.price,
      description: first.description || dup.description,
    })
  );
  const dedupedTestimonials = dedupeByKey(
    testimonials,
    (t) => (t.quote || '').trim().toLowerCase().slice(0, 120),
    (first) => first
  );

  // Design block can carry primary_color / mood; we forward color_mood
  // via _harbor_meta but the actual palette pick is left to compose's
  // archetype-default suggestion.
  return {
    seedSettings,
    services: dedupedServices,
    testimonials: dedupedTestimonials,
    bookingIntents,
    sectionWarnings,
  };
}

/**
 * Stable dedup of an array by a string key. Keeps the first item per key, in
 * original order; `merge` folds a later duplicate into the kept item (e.g. to
 * fill a missing price). Items with an empty key are kept as-is (not deduped).
 */
function dedupeByKey<T>(
  items: T[],
  keyOf: (item: T) => string,
  merge: (kept: T, dup: T) => T
): T[] {
  const byKey = new Map<string, number>(); // key -> index in result
  const result: T[] = [];
  for (const item of items) {
    const key = keyOf(item);
    if (!key) {
      result.push(item);
      continue;
    }
    if (byKey.has(key)) {
      const idx = byKey.get(key) as number;
      result[idx] = merge(result[idx], item);
    } else {
      byKey.set(key, result.length);
      result.push(item);
    }
  }
  return result;
}

// ---------------------------------------------------------------------------
// Model identifier rules shared with the build engine
// ---------------------------------------------------------------------------
//
// compose/build.py (`_spec_safety_errors`) is the injection gate for values
// that become route paths, file names and table names, and it REFUSES a spec
// that does not fit: it never repairs one. An extracted spec comes out of a
// model, so the translator applies the same rules first and DROPS what does
// not fit, with a warning, so that one odd value degrades a build instead of
// failing it. Each constant below mirrors the engine's; a test compares them
// with compose/build.py so the two cannot drift apart unnoticed.

/** A model's route slug. Engine: `_MODEL_SLUG_RE` (which allows up to 63 characters; 60 here). */
export const MODEL_PLURAL_RE = /^[a-z][a-z0-9-]{0,59}$/;

/** Engine: `_SAFE_PATH_RE`. Plain segments only: no dots, quotes, whitespace or Express pattern characters. */
const SAFE_ROUTE_PATH_RE = /^\/[A-Za-z0-9][A-Za-z0-9_-]*(?:\/[A-Za-z0-9][A-Za-z0-9_-]*){0,7}\/?$/;
const MAX_ROUTE_PATH_LENGTH = 120;

/** Engine: `_RESERVED_PUBLIC_PATHS`. Routes the generated site already serves. */
export const RESERVED_PUBLIC_PATHS: ReadonlySet<string> = new Set([
  '/login', '/logout', '/magic', '/set-password', '/request-magic-link',
  '/magic-link-sent', '/privacy', '/terms', '/health', '/contact',
  '/newsletter-signup', '/uploads', '/js', '/brand', '/fonts', '/images',
]);

/** Engine: `_RESERVED_TABLE_NAMES`. Tables the generated site creates for itself. */
export const RESERVED_TABLE_NAMES: ReadonlySet<string> = new Set([
  'users', 'user_sessions', 'site_settings', 'magic_link_tokens', 'slug_aliases',
]);

/** Engine: `_RESERVED_MODEL_SLUGS`. Slugs whose router, route or view directory the engine owns. */
export const RESERVED_MODEL_SLUGS: ReadonlySet<string> = new Set([
  'auth', 'legal', 'seo', 'pages', 'upload', 'admin-dashboard', 'admin-settings',
  'admin', 'login', 'logout', 'magic', 'health', 'privacy', 'terms',
  'set-password', 'request-magic-link', 'magic-link-sent', 'contact',
  'newsletter-signup', 'uploads', 'js', 'brand', 'fonts', 'images',
  'settings', 'change-password', 'clear-all', 'custom',
]);

/** Engine: `_RESERVED_CUSTOM_PAGE_PATHS`. Built-in routes a custom page may not claim. */
export const RESERVED_CUSTOM_PAGE_PATHS: ReadonlySet<string> = new Set([
  '/login', '/magic', '/magic/consume', '/logout', '/terms', '/privacy',
  '/set-password', '/request-magic-link', '/magic-link-sent',
  '/health', '/newsletter-signup', '/uploads', '/js', '/brand', '/fonts',
  '/images', '/admin',
]);

/** Engine: `_MAX_ROLE_NAME_LEN`. `users.role` in a generated site is VARCHAR(50). */
export const MAX_ROLE_NAME_LENGTH = 50;

// ---------------------------------------------------------------------------
// What the archetypes own
// ---------------------------------------------------------------------------
//
// The constants below restate facts from the engine's decision files
// (compose/data/archetypes.json and page-layouts.json). The worker may pick a
// different archetype than the hint (it re-classifies when the hint is not a
// known id), so each is the UNION over all archetypes: a rule that holds for
// every archetype holds for the one that is used. A test reads the decision
// files and compares.

/**
 * Route slug of each archetype default model -> that model's name. The engine
 * merges a spec's models into the archetype's defaults by NAME and then
 * refuses a spec in which two different models share a table, so a spec model
 * may use one of these slugs only when it has the same name (it then takes
 * the default's place).
 */
export const ARCHETYPE_MODEL_SLUGS: Readonly<Record<string, string>> = {
  'blog-posts': 'BlogPost',
  books: 'Book',
  'case-studies': 'CaseStudy',
  'class-types': 'ClassType',
  'contact-inquiries': 'ContactInquiry',
  events: 'Event',
  faqs: 'FAQ',
  instructors: 'Instructor',
  'menu-items': 'MenuItem',
  modalities: 'Modality',
  'newsletter-subscribers': 'NewsletterSubscriber',
  photos: 'Photo',
  'pricing-plans': 'PricingPlan',
  reviews: 'Review',
  'schedule-slots': 'ScheduleSlot',
  'service-areas': 'ServiceArea',
  services: 'Service',
  staff: 'StaffMember',
  testimonials: 'Testimonial',
};

/**
 * Tables an archetype page shows to visitors (a widget with source
 * `db:<table>` in page-layouts.json). A model on one of these tables is
 * public without the spec saying so. Engine: `_public_row_sources`.
 */
export const ARCHETYPE_SHOWN_TABLES: ReadonlySet<string> = new Set([
  'blog_posts', 'books', 'case_studies', 'events', 'instructors', 'menu_items',
  'photos', 'reviews', 'schedule_slots', 'service_areas', 'staff', 'testimonials',
]);

/**
 * Archetype pages whose listing links to a public page per row at
 * `/<page>/<id or slug>` (a `_DETAIL_PAGE_WIDGETS` widget in the engine). The
 * site answers every path one level below these without a login.
 */
export const ARCHETYPE_DETAIL_ROUTE_PAGES: ReadonlySet<string> = new Set([
  'blog', 'books', 'case-studies', 'events', 'journal',
]);

/** Every `default_pages` entry of every archetype: paths an archetype serves publicly. */
export const ARCHETYPE_PAGE_SLUGS: ReadonlySet<string> = new Set([
  'about', 'approach', 'blog', 'books', 'case-studies', 'classes', 'contact',
  'events', 'faq', 'fees-insurance', 'gallery', 'home', 'instructors',
  'journal', 'menu', 'pricing', 'reviews', 'schedule', 'service-areas',
  'services', 'team', 'visit', 'work-with-me',
]);

/** The table the engine gives a model: its slug with "-" as "_". */
function tableOfModel(model: V1DataModel): string {
  return (model.plural ?? engineSlugForModelName(model.name)).replace(/-/g, '_');
}

/** True when the spec lists the model publicly itself (`public_listing_path`). */
function hasPublicListing(model: V1DataModel): boolean {
  return typeof model.public_listing_path === 'string' && model.public_listing_path !== '';
}

/**
 * True when visitors can read the model's rows: the spec lists it, or an
 * archetype page shows its table. Engine: `_public_row_sources`, over every
 * archetype.
 */
function isShownToVisitors(model: V1DataModel): boolean {
  return hasPublicListing(model) || ARCHETYPE_SHOWN_TABLES.has(tableOfModel(model));
}

/**
 * True when the engine gives this field a BOOLEAN column: an explicit
 * `sql_type` wins, otherwise the `type` decides (`_normalize_v1_field` in
 * compose/intake.py, then `_sql_type_for` in compose/build.py).
 */
function isBooleanField(field: V1Field): boolean {
  return fieldKind(field) === 'boolean';
}

/**
 * What a column type is, as far as the engine's own SQL is concerned: the
 * kinds of `_sql_type_kind` in compose/build.py (the base names of
 * `_SAFE_SQL_TYPE_RE` folded together; "[]" makes an array).
 */
export type SqlKind =
  | 'text' | 'integer' | 'serial' | 'numeric' | 'boolean' | 'date' | 'time' | 'timestamp'
  | 'json' | 'uuid' | 'array' | 'other';

const SQL_BASE_KINDS: Readonly<Record<string, SqlKind>> = {
  text: 'text', varchar: 'text', char: 'text',
  integer: 'integer', int: 'integer', bigint: 'integer', smallint: 'integer',
  serial: 'serial', bigserial: 'serial',
  numeric: 'numeric', decimal: 'numeric', real: 'numeric', 'double precision': 'numeric',
  boolean: 'boolean', bool: 'boolean',
  date: 'date', time: 'time', timestamp: 'timestamp', timestamptz: 'timestamp',
  json: 'json', jsonb: 'json', uuid: 'uuid',
};

/** Engine: `_sql_type_kind`. */
export function sqlTypeKind(sqlType: string): SqlKind {
  const text = sqlType.trim().toLowerCase();
  if (text.endsWith('[]')) return 'array';
  const base = text.split('(')[0].replace(/[ \t\n\r\f\v]+/g, ' ').trim();
  return SQL_BASE_KINDS[base] ?? 'other';
}

/**
 * The v1 `type` words the engine's own translator turns into a column type
 * (`_V1_FIELD_TYPE_TO_SQL` in compose/intake.py), as kinds. Any other word
 * is TEXT there.
 */
const V1_TYPE_KINDS: Readonly<Record<string, SqlKind>> = {
  string: 'text', text: 'text', integer: 'integer', int: 'integer',
  number: 'numeric', numeric: 'numeric', float: 'numeric',
  boolean: 'boolean', bool: 'boolean', email: 'text', phone: 'text', url: 'text',
  timestamp: 'timestamp', datetime: 'timestamp', date: 'date', time: 'time', password: 'text',
};

/**
 * The kind of column the engine creates for a field: an explicit `sql_type`
 * wins, otherwise the `type` decides (`_normalize_v1_field` in
 * compose/intake.py, then `_sql_type_for` in compose/build.py).
 */
export function fieldKind(field: V1Field): SqlKind {
  if (field.sql_type) return sqlTypeKind(field.sql_type);
  return V1_TYPE_KINDS[field.type ?? ''] ?? 'text';
}

/**
 * Why a field named like a column the engine owns or reads with a fixed
 * meaning is dropped, or null. The engine refuses a model that declares
 * such a column with a type it cannot work with
 * (`_ENGINE_COLUMN_KINDS` in compose/build.py), so the translator leaves it
 * out: the engine adds `id` and `created_at` itself.
 */
function engineOwnedFieldProblem(field: V1Field): string | null {
  const kind = fieldKind(field);
  if (field.name === 'id') return 'the engine adds the primary key itself';
  if (field.name === 'created_at' && kind !== 'date' && kind !== 'timestamp') {
    return 'the engine adds created_at as a timestamp, and only a date or timestamp column of that name is accepted';
  }
  if (field.name === 'published_at' && kind === 'json') {
    return 'a json column cannot order a listing';
  }
  return null;
}

/**
 * The route the engine serves a copied page at. `_copy_path_to_page_id` in
 * compose/intake.py turns the copied path into a page id (everything outside
 * a-z, 0-9 and "_" becomes "-", at most 64 characters, "home" for the root),
 * and a page id is served at `/<id>`, "home" at "/". Called on paths that
 * `normalizeCopyPath` has cut to 200 characters.
 */
export function copiedPageRoute(path: string): string {
  const lowered = path.trim().toLowerCase();
  if (!lowered || lowered === '/') return '/';
  const trimEnds = (value: string): string => value.replace(/^[-_]+/, '').replace(/[-_]+$/, '');
  const id = trimEnds(trimEnds(lowered.replace(/[^a-z0-9_]+/g, '-')).slice(0, 64)) || 'page';
  return id === 'home' ? '/' : `/${id}`;
}

/** A path as Express compares it: lower case, no trailing slash. Engine: `_path_key`. */
function pathKey(path: string): string {
  return stripTrailingSlashes(path).toLowerCase() || '/';
}

/**
 * Why a model's `public_listing_path` is unacceptable, or null when it is
 * fine. Engine: `_public_listing_path_error`. "/" is refused because it would
 * mount the resource router over the whole site; /admin (in any letter case,
 * since Express matches mount paths case-insensitively) because the engine
 * mounts the admin surface there; and the built-in routes because the listing
 * would shadow them.
 */
export function publicListingPathProblem(path: unknown): string | null {
  if (
    typeof path !== 'string' ||
    path.length > MAX_ROUTE_PATH_LENGTH ||
    path === '/' ||
    !SAFE_ROUTE_PATH_RE.test(path)
  ) {
    return 'not a plain absolute path';
  }
  const lowered = stripTrailingSlashes(path).toLowerCase();
  if (lowered === '/admin' || lowered.startsWith('/admin/')) {
    return 'under /admin, which the engine mounts itself';
  }
  if (RESERVED_PUBLIC_PATHS.has(lowered)) {
    return 'collides with a built-in route';
  }
  return null;
}

/** The slug the engine derives from a PascalCase model name. Engine: `_plural`. */
export function engineSlugForModelName(name: string): string {
  const kebab = name.replace(/([a-z])([A-Z])/g, '$1-$2').toLowerCase();
  if (kebab.endsWith('y') && !'aeiou'.includes(kebab.slice(-2, -1))) {
    return `${kebab.slice(0, -1)}ies`;
  }
  if (kebab.endsWith('s')) return `${kebab}es`;
  return `${kebab}s`;
}

/** Table of each archetype default model -> that model's name (see ARCHETYPE_MODEL_SLUGS). */
const ARCHETYPE_TABLE_OWNERS: ReadonlyMap<string, string> = new Map(
  Object.entries(ARCHETYPE_MODEL_SLUGS).map(([slug, owner]) => [slug.replace(/-/g, '_'), owner])
);

/**
 * Why the model `name` cannot be emitted with this slug, or null. The first
 * three checks are the engine's gate, in its order. The last one is what the
 * engine checks after it has merged the spec's models into the archetype's:
 * a table an archetype model owns can only be used by a spec model of the
 * same name (compared without regard to letter case, as the merge does).
 */
function modelSlugProblem(slug: string, seenTables: ReadonlyMap<string, string>, name: string): string | null {
  const table = slug.replace(/-/g, '_');
  if (RESERVED_TABLE_NAMES.has(table)) return `maps to the reserved table "${table}"`;
  if (RESERVED_MODEL_SLUGS.has(slug)) return `maps to the reserved route slug "${slug}"`;
  const owner = seenTables.get(table);
  if (owner !== undefined) return `maps to the same table as "${owner}" ("${table}")`;
  const archetypeOwner = ARCHETYPE_TABLE_OWNERS.get(table);
  if (archetypeOwner !== undefined && archetypeOwner.toLowerCase() !== name.toLowerCase()) {
    return `maps to the table of the archetype model "${archetypeOwner}" ("${table}")`;
  }
  return null;
}

/**
 * True for a model one of the site's two built-in forms may store into: the
 * contact form (the model named ContactInquiry) or the newsletter signup (the
 * model named NewsletterSubscriber, else one with "subscriber" in its name).
 * Engine: `_contact_form_model`, `_newsletter_model`.
 */
function isBuiltInFormModel(name: string): boolean {
  const lowered = name.toLowerCase();
  return lowered === 'contactinquiry' || lowered.includes('subscriber');
}

interface DataModelResult {
  dataModels: V1DataModel[];
  dataModelWarnings: string[];
}

function normalizeDataModels(
  raw: unknown,
  needsBookingIntent: boolean
): DataModelResult {
  const dataModels: V1DataModel[] = [];
  const warnings: string[] = [];
  const items = Array.isArray(raw) ? raw : [];
  const seenNames = new Set<string>();
  /** table name -> the model that claimed it */
  const seenTables = new Map<string, string>();
  for (const item of items) {
    if (!item || typeof item !== 'object') {
      warnings.push(`data_model entry dropped: not an object`);
      continue;
    }
    const obj = item as Record<string, unknown>;
    const name = sanitizeModelName(obj.name);
    if (!name) {
      warnings.push(`data_model entry dropped: missing or invalid name`);
      continue;
    }
    if (seenNames.has(name)) {
      warnings.push(`data_model "${name}" duplicate; later entry dropped`);
      continue;
    }
    seenNames.add(name);
    const fields = normalizeFields(obj.fields, name, warnings);
    const model: V1DataModel = { name };
    // plural / belongs_to / public_listing_path end up in generated route and
    // schema code downstream, so only plain identifier-like values pass. The
    // rules are the build engine's own (see MODEL_PLURAL_RE above).
    if (typeof obj.plural === 'string' && obj.plural.trim()) {
      const plural = obj.plural.trim().toLowerCase();
      if (MODEL_PLURAL_RE.test(plural)) model.plural = plural;
      else warnings.push(`data_model "${name}": plural dropped (not a plain slug)`);
    }
    if (typeof obj.belongs_to === 'string' && obj.belongs_to.trim()) {
      const parent = obj.belongs_to.trim();
      if (/^[A-Za-z][A-Za-z0-9_ -]{0,59}$/.test(parent)) model.belongs_to = parent;
      else warnings.push(`data_model "${name}": belongs_to dropped (not a plain model name)`);
    }
    if (fields.length > 0) model.fields = fields;
    if (obj.public_listing_path === null) model.public_listing_path = null;
    else if (typeof obj.public_listing_path === 'string' && obj.public_listing_path !== '') {
      const problem = publicListingPathProblem(obj.public_listing_path);
      if (problem === null) model.public_listing_path = obj.public_listing_path;
      else warnings.push(`data_model "${name}": public_listing_path dropped (${problem})`);
    }
    // What visitors send through the contact form or the newsletter signup is
    // never listed publicly. (The engine refuses a build whose public form
    // stores into a publicly shown model that cannot hold a row back.)
    if (hasPublicListing(model) && isBuiltInFormModel(name)) {
      delete model.public_listing_path;
      warnings.push(
        `data_model "${name}": public_listing_path dropped (the contact form or the newsletter signup stores into this model)`
      );
    }

    // The engine derives a route slug and a table from each model and refuses
    // the spec when either is one it owns, when two models share a table, or
    // when the table belongs to an archetype model of another name. An
    // explicit plural that causes that is dropped first (the name may give a
    // usable slug); a model that still collides is dropped whole.
    let slug = model.plural ?? engineSlugForModelName(name);
    let collision = modelSlugProblem(slug, seenTables, name);
    if (collision !== null && model.plural) {
      warnings.push(`data_model "${name}": plural dropped (${collision})`);
      delete model.plural;
      slug = engineSlugForModelName(name);
      collision = modelSlugProblem(slug, seenTables, name);
    }
    if (collision !== null) {
      warnings.push(`data_model "${name}" dropped: ${collision}`);
      seenNames.delete(name);
      continue;
    }
    seenTables.set(slug.replace(/-/g, '_'), name);
    dataModels.push(model);
  }

  // Auto-add BookingIntent if conversation surfaced booking and no
  // model captures it (by name or by table).
  if (
    needsBookingIntent &&
    !seenNames.has('BookingIntent') &&
    !seenTables.has((FUTURE_FRIENDLY_DATA_MODELS.BookingIntent.plural as string).replace(/-/g, '_'))
  ) {
    dataModels.push(FUTURE_FRIENDLY_DATA_MODELS.BookingIntent);
  }

  resolveModelLinks(dataModels, warnings);

  return { dataModels, dataModelWarnings: warnings };
}

/** The names the engine takes `belongs_to` to mean for one model: its name or its plural, lower-cased. */
function modelNeedles(model: V1DataModel): string[] {
  const needles = [model.name.toLowerCase()];
  if (model.plural) needles.push(model.plural.toLowerCase());
  return needles;
}

/** The column the engine adds to a child for its link to the parent model `parentName`. Engine: `_belongs_to_info`. */
export function foreignKeyColumn(parentName: string): string {
  return `${parentName.replace(/([a-z])([A-Z])/g, '$1-$2').toLowerCase().replace(/-/g, '_')}_id`;
}

/**
 * The models a `belongs_to` can mean: the spec's own and the archetypes'
 * default models (the engine merges both lists and resolves the name in it).
 */
function candidateParents(model: V1DataModel, models: V1DataModel[]): string[] {
  const needle = (model.belongs_to ?? '').trim().toLowerCase();
  if (!needle) return [];
  const names = new Set<string>();
  for (const candidate of models) {
    if (modelNeedles(candidate).includes(needle)) names.add(candidate.name);
  }
  for (const [slug, archetypeName] of Object.entries(ARCHETYPE_MODEL_SLUGS)) {
    if (archetypeName.toLowerCase() === needle || slug === needle) names.add(archetypeName);
  }
  return Array.from(names);
}

/**
 * Settle what the engine would refuse about the links between models
 * (`_engine_column_errors`, `_models_in_creation_order` in compose/build.py):
 *
 *   - a field named like the column that holds the link to the parent
 *     (`<parent>_id`) is dropped, whatever its type: the engine adds that
 *     column as an integer that REFERENCES the parent, and refuses a field of
 *     that name with any other type;
 *   - models that refer to each other in a ring through `belongs_to` have no
 *     order in which each table exists before the one that points to it: the
 *     link that closes the ring is dropped, the models stay.
 */
function resolveModelLinks(models: V1DataModel[], warnings: string[]): void {
  for (const model of models) {
    if (!model.belongs_to || !model.fields) continue;
    const linkColumns = candidateParents(model, models).map(foreignKeyColumn);
    const kept = model.fields.filter((field) => !linkColumns.includes(field.name));
    if (kept.length === model.fields.length) continue;
    warnings.push(
      `data_model "${model.name}": field dropped (the engine adds the column that links it to "${model.belongs_to}" itself)`
    );
    if (kept.length > 0) model.fields = kept;
    else delete model.fields;
  }
  const parentOf = (model: V1DataModel): V1DataModel | undefined => {
    const needle = (model.belongs_to ?? '').trim().toLowerCase();
    if (!needle) return undefined;
    return models.find((candidate) => modelNeedles(candidate).includes(needle));
  };
  for (const start of models) {
    const chain = new Set<V1DataModel>();
    let current: V1DataModel | undefined = start;
    while (current && !chain.has(current)) {
      chain.add(current);
      const parent = parentOf(current);
      if (!parent || parent === current) break;
      if (chain.has(parent)) {
        warnings.push(
          `data_model "${current.name}": belongs_to "${current.belongs_to}" dropped (the models belong to each other in a ring, and a table cannot point to one that is created after it)`
        );
        delete current.belongs_to;
        break;
      }
      current = parent;
    }
  }
  // The author archetype's own Testimonial belongs to Book: a spec model
  // named Book that belongs to Testimonial would close a ring through it.
  for (const model of models) {
    const needle = (model.belongs_to ?? '').trim().toLowerCase();
    if (model.name.toLowerCase() === 'book' && ['testimonial', 'testimonials'].includes(needle)) {
      warnings.push(
        `data_model "${model.name}": belongs_to "${model.belongs_to}" dropped (the archetype's Testimonial belongs to Book, so the two would belong to each other)`
      );
      delete model.belongs_to;
    }
  }
}

function sanitizeModelName(raw: unknown): string | null {
  if (typeof raw !== 'string') return null;
  const trimmed = raw.trim();
  if (!trimmed) return null;
  // Pascal case the model name for compose convention. Strip non-alnum.
  const parts = trimmed.split(/[\s_-]+/).filter(Boolean);
  if (parts.length === 0) return null;
  const joined = parts
    .map((p) => p.charAt(0).toUpperCase() + p.slice(1))
    .join('');
  // Only allow [A-Za-z0-9].
  const safe = joined.replace(/[^A-Za-z0-9]/g, '');
  if (!safe || !/^[A-Z]/.test(safe)) return null;
  return safe.slice(0, 60);
}

/**
 * Column types the translator will pass through as `sql_type`. compose/build.py
 * interpolates `sql_type` verbatim into the generated app's `db/schema.sql`,
 * and the value originates in model output (spec extraction), which a visitor
 * can steer. Anything that is not a plain column type is dropped, so a value
 * like `TEXT); DROP TABLE x; --` can never reach the DDL. When dropped, the
 * field's `type` still drives compose's own type mapping.
 *
 * ASCII only, like the engine's `_SAFE_SQL_TYPE_RE` (compiled with re.ASCII):
 * the space class is spelled out because `\s` in JavaScript also matches a
 * no-break space and the other Unicode spaces, which the engine refuses.
 * `\d` and the `i` flag (without `u`) are already ASCII-only in JavaScript.
 */
const SAFE_SQL_TYPE_RE =
  /^(text|varchar|char|integer|int|bigint|smallint|serial|bigserial|boolean|bool|numeric|decimal|real|double precision|date|time|timestamp|timestamptz|json|jsonb|uuid)(\([ \t\n\r\f\v]*\d{1,4}([ \t\n\r\f\v]*,[ \t\n\r\f\v]*\d{1,4})?[ \t\n\r\f\v]*\))?(\[\])?$/i;

export function isSafeSqlType(raw: string): boolean {
  return raw.length <= 40 && SAFE_SQL_TYPE_RE.test(raw.trim());
}

function normalizeFields(
  raw: unknown,
  modelName: string,
  warnings: string[]
): V1Field[] {
  if (!Array.isArray(raw)) return [];
  const out: V1Field[] = [];
  const seen = new Set<string>();
  for (const f of raw) {
    if (!f || typeof f !== 'object') {
      warnings.push(`${modelName}: field entry dropped (not an object)`);
      continue;
    }
    const obj = f as Record<string, unknown>;
    const name = sanitizeFieldName(obj.name);
    if (!name) {
      warnings.push(`${modelName}: field dropped (missing or invalid name)`);
      continue;
    }
    if (seen.has(name)) {
      warnings.push(`${modelName}: duplicate field "${name}" dropped`);
      continue;
    }
    seen.add(name);
    const field: V1Field = { name };
    const type = readString(obj.type ?? obj.field_type);
    if (type) field.type = type.toLowerCase();
    const sql = readString(obj.sql_type);
    if (sql) {
      if (isSafeSqlType(sql)) {
        field.sql_type = sql;
      } else {
        warnings.push(
          `${modelName}.${name}: sql_type dropped (not a plain column type: "${sql
            .replace(/[^\x20-\x7E]/g, ' ')
            .slice(0, 40)}")`
        );
      }
    }
    if (obj.required === true) field.required = true;
    if ('default' in obj) {
      const d = obj.default;
      if (typeof d === 'string' || typeof d === 'number' || typeof d === 'boolean' || d === null) {
        field.default = d;
      }
    }
    const owned = engineOwnedFieldProblem(field);
    if (owned !== null) {
      warnings.push(`${modelName}.${name}: field dropped (${owned})`);
      continue;
    }
    if (name === 'email' && fieldKind(field) !== 'text') {
      // The newsletter signup stores the address as text; any `email` column
      // is one a person types an address into.
      field.type = 'email';
      delete field.sql_type;
      warnings.push(`${modelName}.${name}: type set to email (an email column holds text)`);
    }
    out.push(field);
  }
  return out;
}

function sanitizeFieldName(raw: unknown): string | null {
  if (typeof raw !== 'string') return null;
  const trimmed = raw.trim();
  if (!trimmed) return null;
  // snake_case for fields.
  const safe = trimmed
    .replace(/[\s-]+/g, '_')
    .replace(/[^A-Za-z0-9_]/g, '')
    .toLowerCase();
  if (!safe || !/^[a-z_]/.test(safe)) return null;
  return safe.slice(0, 60);
}

function normalizeUserRoles(raw: unknown): V1UserRole[] {
  if (!Array.isArray(raw)) return [];
  const out: V1UserRole[] = [];
  const seen = new Set<string>();
  for (const r of raw) {
    if (!r || typeof r !== 'object') continue;
    const obj = r as Record<string, unknown>;
    const role = readString(obj.role);
    if (!role) continue;
    if (seen.has(role)) continue;
    seen.add(role);
    out.push({ role, description: readString(obj.description) || undefined });
  }
  return out;
}

/**
 * Pages that already exist as defaults across one or more archetypes.
 * Submitted pages with these slugs collide with the archetype default
 * page layout; we drop them to avoid double-rendering. The archetype
 * default takes precedence + the customer copy seeded via _seed_settings
 * still flows through.
 *
 * This list is older than ARCHETYPE_PAGE_SLUGS and is not the same set: it
 * lacks `books` (so a public /books page reaches the engine, which lets the
 * author archetype's own page win and keeps the page for every other
 * archetype) and it has `past-events` and `modalities`, which are a default
 * page of no archetype. A page marked auth_required is checked against the
 * exact set (`dropPagesTheEngineWouldRefuse`).
 */
const ARCHETYPE_DEFAULT_SLUGS = new Set([
  'home',
  'about',
  'contact',
  'services',
  'service-areas',
  'team',
  'gallery',
  'reviews',
  'blog',
  'journal',
  'events',
  'past-events',
  'work-with-me',
  'classes',
  'schedule',
  'instructors',
  'pricing',
  'menu',
  'visit',
  'case-studies',
  'modalities',
  'fees-insurance',
  'approach',
  'faq',
]);

/**
 * Slugs that, when encountered as custom pages, auto-wire to a known
 * data_model's form_target if no explicit target was provided. Lets
 * the translator turn a free-form `/book` page into a real submission
 * form deterministically.
 */
const SLUG_TO_AUTO_FORM_TARGET: Record<string, string[]> = {
  book: ['BookingIntent', 'Booking', 'Session', 'Appointment'],
  booking: ['BookingIntent', 'Booking', 'Session', 'Appointment'],
  schedule: ['BookingIntent', 'Booking', 'Session', 'Appointment'],
  apply: ['Application', 'Inquiry', 'ContactInquiry'],
  intake: ['IntakeSubmission', 'IntakeResponse', 'Intake'],
};

/**
 * A page's `allowed_roles` as the engine accepts them: trimmed role names of
 * at most MAX_ROLE_NAME_LENGTH characters, each once. The engine refuses the
 * whole spec on an entry it cannot check (an empty string, a name too long to
 * be stored), so such an entry is dropped here instead. Dropping it never
 * widens access: when a page that asks for a login is left with no role at
 * all, it is given `admin`, not "any signed-in account".
 */
function normalizeAllowedRoles(
  raw: unknown,
  authRequired: boolean,
  pageLabel: string,
  warnings: string[]
): string[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const roles: string[] = [];
  let dropped = 0;
  for (const entry of raw) {
    const role = typeof entry === 'string' ? entry.trim() : '';
    if (!role || role.length > MAX_ROLE_NAME_LENGTH) {
      dropped += 1;
      continue;
    }
    if (!roles.includes(role)) roles.push(role);
  }
  if (dropped === 0) return roles;
  if (roles.length === 0 && authRequired) {
    warnings.push(
      `custom page "${pageLabel}": none of its allowed_roles is a usable role name; the page is limited to the admin`
    );
    return ['admin'];
  }
  warnings.push(`custom page "${pageLabel}": ${dropped} allowed_roles entr${dropped === 1 ? 'y' : 'ies'} dropped (not a usable role name)`);
  return roles;
}

function extractCustomPages(
  raw: unknown,
  dataModels: V1DataModel[],
  warnings: string[]
): V1CustomPage[] {
  if (!Array.isArray(raw)) return [];
  const modelsByName = new Map(dataModels.map((m) => [m.name, m]));
  const out: V1CustomPage[] = [];
  /** path -> index in `out`, to find a page that is declared twice */
  const indexByPath = new Map<string, number>();
  for (const p of raw) {
    if (!p || typeof p !== 'object') continue;
    const obj = p as Record<string, unknown>;
    const rawSlug = readString(obj.slug ?? obj.path ?? obj.name).toLowerCase();
    if (!rawSlug) continue;
    const slug = stripSurroundingSlashes(rawSlug);
    if (!slug) continue;
    const authRequired = obj.auth_required === true;
    if (ARCHETYPE_DEFAULT_SLUGS.has(slug)) {
      // The archetype's own page answers there, and it is public. Say so when
      // the submitted page asked for a login: that request cannot be honored.
      if (authRequired) {
        warnings.push(
          `custom page "${rawSlug}" is marked auth_required but was dropped: the archetype's own public page owns that path`
        );
      }
      continue;
    }
    // Parametric paths (e.g. /events/:event-slug, /blog/:slug) cannot be
    // honored by compose's custom-page emitter: the slug-from-path helper
    // produces an invalid view filename containing `:`, the emitted
    // Express route shadows the archetype's default `/:identifier` detail
    // handler (which already renders a SELECT-by-slug detail view for any
    // listing data_model, see compose/build.py:emit_routes_pages_js
    // db_widget_id detail handler), and the keeper-declared parametric
    // page would 404 because no view file exists for the templated path.
    // Drop with a warning. The keeper-listed parent listing page (e.g.
    // /events) already supplies the detail route via the archetype's
    // db-source listing widget.
    if (slug.includes(':')) {
      warnings.push(
        `custom page "${rawSlug}" with parametric segment dropped; ` +
          `archetype-default detail handler at the parent listing path renders per-record detail pages`
      );
      continue;
    }
    const path = `/${slug}`;
    const explicitTarget = readString(obj.form_target);
    let formTarget: string | undefined =
      explicitTarget && modelsByName.has(explicitTarget) ? explicitTarget : undefined;
    if (!formTarget) {
      // A form target the translator picks itself is never a model visitors
      // can read: a booking form must not add rows to a public list of
      // sessions. (An explicit target is the spec's own choice;
      // `holdPublicFormRowsBack` deals with that case.)
      const candidates = SLUG_TO_AUTO_FORM_TARGET[slug] ?? [];
      const passedOver: string[] = [];
      for (const c of candidates) {
        const candidate = modelsByName.get(c);
        if (!candidate) continue;
        if (isShownToVisitors(candidate)) {
          passedOver.push(c);
          continue;
        }
        formTarget = c;
        break;
      }
      if (!formTarget && passedOver.length > 0) {
        warnings.push(
          `custom page "${rawSlug}": no form was attached (the model "${passedOver[0]}" it would store into is shown to visitors)`
        );
      }
    }
    const purpose = readString(obj.purpose ?? obj.description) || undefined;

    // Empty-stub guard: when the model emitted sections for this page but
    // every one of them drops via SECTION_TYPE_EXPLAINED_DROPS or unknown
    // (i.e. nothing landed in SECTION_TYPE_ALIASES), AND no form_target
    // resolves, AND there's no substantive purpose text to fill the
    // hero/centered body, the custom-page emitter
    // (compose/build.py:emit_custom_page_views) would render only a
    // centered title with an empty body. Drop the page with an explanatory
    // warning instead of shipping an empty stub. Sections-handled pages
    // still pass through (their content hoists to global state); pages with
    // NO sections at all also still pass through (they're keeper-defined
    // placeholders the keeper can fill via the admin surface).
    const sections = readArray((obj as Record<string, unknown>).sections);
    if (sections.length > 0 && !formTarget) {
      let renderableSections = 0;
      for (const section of sections) {
        if (!section || typeof section !== 'object') continue;
        const rawType = readString((section as Record<string, unknown>).type);
        if (!rawType) continue;
        const normalized = normalizeSectionType(rawType);
        if (SECTION_TYPE_ALIASES[normalized]) {
          renderableSections++;
        }
      }
      const hasUsefulPurpose = !!(purpose && purpose.length >= 40);
      if (renderableSections === 0 && !hasUsefulPurpose) {
        warnings.push(
          `custom page "${rawSlug}" dropped: ${sections.length} section(s) emitted ` +
            `but none mapped to a renderable widget (likely all explained-drops or unknown), ` +
            `no form_target resolved, and no substantive purpose text. ` +
            `Page would render as an empty stub (centered title only). ` +
            `Cross-archetype guard against the sample-site-v2 /video bug.`
        );
        continue;
      }
    }

    const allowedRoles = normalizeAllowedRoles(obj.allowed_roles, authRequired, rawSlug, warnings);
    // A list of roles is a restriction: a page that names an audience asks
    // for a login whether or not it also says so (the engine reads it that
    // way too, `_roles_imply_login` in compose/build.py). Serving it to
    // everyone would be the more exposed reading.
    const listsRoles = !authRequired && allowedRoles !== undefined && allowedRoles.length > 0;
    if (listsRoles) {
      warnings.push(
        `custom page "${rawSlug}" lists allowed_roles without auth_required; a list of roles is a restriction, so the page requires a login and one of those roles`
      );
    }
    const page: V1CustomPage = {
      path,
      title: readString(obj.title ?? obj.name) || undefined,
      purpose,
      auth_required: authRequired || listsRoles,
      allowed_roles: allowedRoles,
      form_target: formTarget,
      submit_text: readString(obj.submit_text) || undefined,
      success_message: readString(obj.success_message) || undefined,
    };

    // One page per path. The engine keeps the first of two pages with one
    // path, and refuses the spec when the first is public and a later one
    // asks for a login (the path would be public). Here the first is kept
    // too, and it takes the login requirement of the later one: a page that
    // any declaration wanted protected is protected.
    const earlier = indexByPath.get(path);
    if (earlier !== undefined) {
      const kept = out[earlier];
      if (page.auth_required && !kept.auth_required) {
        kept.auth_required = true;
        kept.allowed_roles = page.allowed_roles;
        warnings.push(
          `custom page "${path}" is declared more than once and one declaration is auth_required; the page was emitted once and requires a login`
        );
      } else {
        warnings.push(`custom page "${path}" is declared more than once; the later declaration was dropped`);
      }
      continue;
    }
    indexByPath.set(path, out.length);
    out.push(page);
  }
  return dropPagesTheEngineWouldRefuse(out, dataModels, warnings);
}

/**
 * Remove the custom pages the engine would refuse the spec over, and the one
 * model listing that conflicts with a protected page.
 *
 * The engine refuses (`_auth_required_page_errors` in compose/build.py) a spec
 * whose auth_required page sits at a path that something else answers
 * without a login: the spec contradicts itself and the engine will not pick
 * a side. A refused spec is a failed build, so the translator resolves each
 * contradiction it can see, always toward less exposure, and says so in a
 * warning:
 *
 *   - a protected page exactly at a model's public listing: the listing is
 *     withdrawn (`public_listing_path` becomes null) and the page is kept;
 *   - a protected page one level below a model's public listing, below an
 *     archetype listing page that has per-row pages, or at a path an
 *     archetype serves publicly: the page is dropped. Nothing the page would
 *     have held is emitted, so nothing it was meant to protect is public.
 *
 * A page at one of the site's built-in routes is dropped whether or not it
 * asks for a login: the built-in route answers there in either case.
 *
 * A protected page at the route of a page copied from the keeper's existing
 * site is the same contradiction; `dropCopiedPagesAtProtectedPaths` resolves
 * it once the copied pages are known.
 */
function dropPagesTheEngineWouldRefuse(
  pages: V1CustomPage[],
  dataModels: V1DataModel[],
  warnings: string[]
): V1CustomPage[] {
  for (const page of pages) {
    if (!page.auth_required) continue;
    const key = pathKey(page.path);
    dataModels.forEach((model, index) => {
      if (hasPublicListing(model) && pathKey(model.public_listing_path as string) === key) {
        dataModels[index] = { ...model, public_listing_path: null };
        warnings.push(
          `data_model "${model.name}": public_listing_path dropped (the custom page "${page.path}" at that path is auth_required)`
        );
      }
    });
  }
  const listingKeys = new Set(
    dataModels.filter(hasPublicListing).map((m) => pathKey(m.public_listing_path as string))
  );
  return pages.filter((page) => {
    const key = pathKey(page.path);
    if (RESERVED_CUSTOM_PAGE_PATHS.has(key)) {
      warnings.push(`custom page "${page.path}" dropped: the site's built-in route answers at that path`);
      return false;
    }
    if (!page.auth_required) return true;
    const segments = key.slice(1).split('/');
    const parent = key.slice(0, key.lastIndexOf('/'));
    let owner: string | null = null;
    if (segments.length === 1 && ARCHETYPE_PAGE_SLUGS.has(segments[0])) {
      owner = "an archetype's public page";
    } else if (segments.length === 2 && ARCHETYPE_DETAIL_ROUTE_PAGES.has(segments[0])) {
      owner = `the public per-row pages of the archetype's /${segments[0]} listing`;
    } else if (parent && listingKeys.has(parent)) {
      owner = `the public per-row pages of the model listed at ${parent}`;
    }
    if (owner === null) return true;
    warnings.push(
      `custom page "${page.path}" is marked auth_required but was dropped: ${owner} would answer at that path without a login`
    );
    return false;
  });
}

/**
 * Leave out a copied page whose route a custom page claims with a login.
 *
 * The engine serves a copied page without a login at the route
 * `copiedPageRoute` gives, and refuses a spec in which an auth_required page
 * has that same route (`_auth_required_page_errors`). The protected page is
 * kept and the copy is dropped: the path then asks for a login, which is
 * what the spec said about it, and the copied text is not published.
 */
function dropCopiedPagesAtProtectedPaths(
  copyPages: CopyPage[],
  customPages: V1CustomPage[],
  warnings: string[]
): CopyPage[] {
  const protectedKeys = new Set(
    customPages.filter((page) => page.auth_required).map((page) => pathKey(page.path))
  );
  if (protectedKeys.size === 0) return copyPages;
  return copyPages.filter((copied) => {
    const route = copiedPageRoute(copied.path);
    if (!protectedKeys.has(route)) return true;
    warnings.push(
      `copy_pages "${copied.path}" dropped: the custom page at "${route}" is auth_required, and the copied page would answer there without a login`
    );
    return false;
  });
}

/**
 * Make sure a row a visitor submits through a custom form page is not shown
 * on the site before the owner has seen it.
 *
 * The engine stores such a row with `published = FALSE` when the target
 * model has a boolean `published` column, and refuses the build when the
 * model is shown to visitors and has no such column (`_public_form_errors`
 * and `_publication_column` in compose/build.py). So for every public form
 * page whose target model is shown to visitors (`isShownToVisitors`) and has
 * no `published` field, the field is added here (boolean, default true, so
 * the rows the owner seeds and enters stay visible).
 *
 * Two kinds of target model cannot be given the field, and the page then
 * loses its form (it stays as an ordinary page):
 *   - a model with no field list: its columns are the engine's defaults for
 *     that model name, which the translator does not know;
 *   - a model whose `published` field is something other than a boolean (the
 *     date a book came out): the name is taken, and that column cannot hide
 *     a row.
 */
function holdPublicFormRowsBack(
  pages: V1CustomPage[],
  dataModels: V1DataModel[],
  warnings: string[]
): void {
  for (const page of pages) {
    if (!page.form_target || page.auth_required) continue;
    const index = dataModels.findIndex((m) => m.name === page.form_target);
    if (index < 0) continue;
    const model = dataModels[index];
    if (!isShownToVisitors(model)) continue;
    const fields = model.fields ?? [];
    const flag = fields.find((f) => f.name === 'published');
    if (flag && isBooleanField(flag)) continue;
    if (flag || fields.length === 0) {
      delete page.form_target;
      const why = flag
        ? 'its "published" field is not a yes/no field, so a submitted row could not be held back'
        : 'it has no field list of its own, so the translator cannot tell whether a submitted row could be held back';
      warnings.push(
        `custom page "${page.path}": form_target "${model.name}" dropped (the model is shown to visitors and ${why})`
      );
      continue;
    }
    dataModels[index] = {
      ...model,
      fields: [...fields, { name: 'published', type: 'boolean', default: true }],
    };
    warnings.push(
      `data_model "${model.name}": a "published" field was added (the form on "${page.path}" lets visitors add rows and the model is shown to visitors; a submitted row stays hidden until it is published in the admin)`
    );
  }
}

/**
 * Columns the engine never offers a visitor on a public form, whatever a
 * page's `allowed_fields` says. Engine: `_PUBLIC_FORM_SYSTEM_FIELDS`.
 */
const PUBLIC_FORM_SYSTEM_FIELDS: ReadonlySet<string> = new Set([
  'id', 'created_at', 'updated_at', 'approved', 'admin_notes',
  'status', 'published', 'published_at', 'reviewed_at', 'reviewed_by',
]);

/**
 * Name the columns a visitor may fill in on every form page that is not
 * behind a login. The engine refuses such a page when it names none
 * (`_public_form_field_errors` in compose/build.py): without a list the form
 * would take every column of the model, and the model is declared for what
 * the owner enters. The list is the model's own fields the conversation
 * defined, less the engine's reserved columns and the password and image
 * fields. A page whose model has no field list of its own (the translator
 * cannot tell which columns the engine gives it), or none left to offer, loses
 * its form and stays an ordinary page.
 */
function nameVisitorFields(
  pages: V1CustomPage[],
  dataModels: V1DataModel[],
  warnings: string[]
): void {
  for (const page of pages) {
    if (!page.form_target || page.auth_required) continue;
    const model = dataModels.find((m) => m.name === page.form_target);
    if (!model) continue;
    const names = (model.fields ?? [])
      .filter((f) => !PUBLIC_FORM_SYSTEM_FIELDS.has(f.name) && f.type !== 'password' && f.type !== 'image')
      .map((f) => f.name);
    if (names.length === 0) {
      delete page.form_target;
      warnings.push(
        `custom page "${page.path}": form_target "${model.name}" dropped (the model has no field of its own that a visitor may fill in)`
      );
      continue;
    }
    page.allowed_fields = names;
  }
}

/**
 * The stock photos of a specific person or place that the engine never
 * uses for a customer's site (`_PERSONAL_PHOTO_PATTERN` in
 * compose/stock_photos.py, matched there against the file name, the alt text
 * and the tags; only the file name is known here, and every file the library
 * lists is matched by its name).
 */
const PERSONAL_STOCK_PHOTO_NAME_RE =
  /practitioner[\s_-]?portrait|facilitator|gathering|dome[\s_-]?venue|event[\s_-]?venue/i;

/** A reference to a stock photo that is a path of this site, not a part of another site's address. Engine: `_STOCK_PHOTO_REF_RE`. */
const STOCK_PHOTO_REF_RE = /(?<![A-Za-z0-9._~%:-])\/images\/stock\/([A-Za-z0-9][A-Za-z0-9._-]{0,127})/g;

export function isPersonalStockPhotoName(filename: string): boolean {
  return PERSONAL_STOCK_PHOTO_NAME_RE.test(filename);
}

/**
 * Remove, from every string of the spec, a path to a stock photo that shows
 * a specific person or place. The engine refuses a spec that names one
 * (`_personal_stock_photo_errors`), and a path like that comes from nowhere
 * the translator trusts: the library's own files are chosen by the engine.
 * An image setting that held one is left empty (the engine then picks its own
 * photo, or none).
 */
function scrubPersonalStockPhotos(root: unknown, warnings: string[]): void {
  const found = new Set<string>();
  const stack: unknown[] = [root];
  while (stack.length > 0) {
    const node = stack.pop();
    if (Array.isArray(node)) {
      node.forEach((item, index) => {
        if (typeof item === 'string') {
          const cleaned = withoutPersonalPhotos(item, found);
          if (cleaned !== item) node[index] = cleaned;
        } else if (item && typeof item === 'object') stack.push(item);
      });
    } else if (node && typeof node === 'object') {
      const record = node as Record<string, unknown>;
      for (const key of Object.keys(record)) {
        const value = record[key];
        if (typeof value === 'string') {
          const cleaned = withoutPersonalPhotos(value, found);
          if (cleaned !== value) record[key] = cleaned;
        } else if (value && typeof value === 'object') stack.push(value);
      }
    }
  }
  for (const name of found) {
    warnings.push(`a path to the stock photo "${name}" was removed (it shows a specific person or place; photos like that are never used for a customer's site)`);
  }
}

function withoutPersonalPhotos(text: string, found: Set<string>): string {
  if (!text.includes('/images/stock/')) return text;
  return text.replace(STOCK_PHOTO_REF_RE, (match, filename: string) => {
    if (!isPersonalStockPhotoName(filename)) return match;
    found.add(filename);
    return '';
  });
}

function readColorMood(raw: unknown): string | null {
  const obj = readObject(raw);
  const mood = readString(obj.mood);
  const palette = readString(obj.primary_color);
  if (mood && palette) return `${mood}; ${palette}`;
  return mood || palette || null;
}

// --- design-preference normalizers (Phase 1, 2026-04-28) ---
// Each is a pure function: same input → same output. They produce
// either a clean value or `undefined`/empty array; they do not throw.
// Validation against the actual compose registries (palettes.json,
// font-pairs.json, page-layouts.json) happens in compose/intake.py;
// this layer only cleans the shape so the registry lookup is safe.

const _DENSITY_VALUES = new Set(['airy', 'standard', 'dense']);
const _HERO_STYLE_VALUES = new Set(['centered', 'split-image', 'editorial', 'image-led']);
const _SAFE_HINT_RE = /^[a-z0-9][a-z0-9-]{1,49}$/;
const _KEYWORD_CAP = 5;
const _KEYWORD_RE = /^[a-z][a-z-]{1,19}$/;

export function normalizeFreeStringHint(raw: unknown): string | undefined {
  if (typeof raw !== 'string') return undefined;
  const v = raw.trim().toLowerCase();
  if (!v) return undefined;
  // Reject anything with characters that couldn't possibly be a registry id.
  // Python re-validates against the actual registries; this only filters
  // garbage shapes (whitespace, punctuation, emojis, prose sentences).
  if (!_SAFE_HINT_RE.test(v)) return undefined;
  return v;
}

export function normalizeDensityHint(
  raw: unknown
): 'airy' | 'standard' | 'dense' | undefined {
  if (typeof raw !== 'string') return undefined;
  const v = raw.trim().toLowerCase();
  return _DENSITY_VALUES.has(v) ? (v as 'airy' | 'standard' | 'dense') : undefined;
}

export function normalizeHeroStyleHint(
  raw: unknown
): 'centered' | 'split-image' | 'editorial' | 'image-led' | undefined {
  if (typeof raw !== 'string') return undefined;
  const v = raw.trim().toLowerCase();
  return _HERO_STYLE_VALUES.has(v)
    ? (v as 'centered' | 'split-image' | 'editorial' | 'image-led')
    : undefined;
}

export function normalizeKeywordList(raw: unknown): string[] {
  if (!Array.isArray(raw)) return [];
  const out: string[] = [];
  const seen = new Set<string>();
  for (const item of raw) {
    if (typeof item !== 'string') continue;
    const v = item.trim().toLowerCase();
    if (!v || seen.has(v)) continue;
    if (!_KEYWORD_RE.test(v)) continue;
    seen.add(v);
    out.push(v);
    if (out.length >= _KEYWORD_CAP) break;
  }
  return out;
}

// ============================================================
// Faithful-copy-mode helpers (v2-faithful-site-copy-mode, 2026-05-08)
// ============================================================

const _COPY_PAGES_CAP = 12;
const _COPY_BLOCKS_PER_PAGE_CAP = 60;
const _COPY_BLOCK_LENGTH_CAP = 8000;
const _CREDENTIAL_LINE_CAP = 8;
const _CREDENTIAL_LINE_LENGTH_CAP = 200;

export function normalizeCopySourceUrl(raw: unknown): string | undefined {
  if (typeof raw !== 'string') return undefined;
  const v = raw.trim();
  if (!v) return undefined;
  // Allow http(s) only. Anything else (file://, javascript:, data:) is
  // either dangerous or not a public source we could ever fetch.
  if (!/^https?:\/\//i.test(v)) return undefined;
  // Cap at a reasonable URL length so a runaway value doesn't pollute
  // the spec or DB column.
  return v.slice(0, 2000);
}

export function normalizeCopyPages(
  raw: unknown,
  warnings: string[]
): CopyPage[] {
  if (!Array.isArray(raw)) {
    if (raw !== undefined && raw !== null) {
      warnings.push('copy_pages dropped: not an array');
    }
    return [];
  }
  const out: CopyPage[] = [];
  const seenPaths = new Set<string>();
  for (const item of raw) {
    if (out.length >= _COPY_PAGES_CAP) {
      warnings.push(
        `copy_pages truncated at ${_COPY_PAGES_CAP} entries; remaining ignored`
      );
      break;
    }
    if (!item || typeof item !== 'object' || Array.isArray(item)) continue;
    const o = item as Record<string, unknown>;
    const path = normalizeCopyPath(o.path);
    if (!path) {
      warnings.push('copy_pages entry dropped: missing or malformed path');
      continue;
    }
    if (seenPaths.has(path)) {
      warnings.push(`copy_pages duplicate path "${path}" dropped`);
      continue;
    }
    const blocks = normalizeMarkdownBlocks(o.markdown_blocks);
    if (blocks.length === 0) {
      // No content is a strong signal the crawler captured nothing
      // useful for this URL; skip rather than emit an empty page that
      // would override the archetype default with literal nothing.
      warnings.push(
        `copy_pages "${path}" dropped: no markdown content captured`
      );
      continue;
    }
    const hero = normalizeCopyHero(o.hero);
    const title =
      typeof o.title === 'string' && o.title.trim()
        ? o.title.trim().slice(0, 200)
        : undefined;
    const entry: CopyPage = { path, markdown_blocks: blocks };
    if (title) entry.title = title;
    if (hero) entry.hero = hero;
    out.push(entry);
    seenPaths.add(path);
  }
  return out;
}

function normalizeCopyPath(raw: unknown): string | null {
  if (typeof raw !== 'string') return null;
  let v = raw.trim().toLowerCase();
  if (!v) return null;
  // Drop query + hash; we copy the page, not a particular fragment.
  v = stripQueryAndFragment(v);
  // Allow absolute path or pathless slug. Reject http(s) origins
  // (caller should pass the path component, not the full URL). Anchored at
  // the start of the string: one attempt, one pass.
  v = v.replace(/^https?:\/\/[^/]+/, '');
  if (!v.startsWith('/')) v = `/${v}`;
  // Strip trailing slash except for root.
  if (v.length > 1) v = stripTrailingSlashes(v);
  // Reject anything with parametric segments or unsafe chars.
  if (!/^\/[a-z0-9/_-]*$/.test(v)) return null;
  // Compose-friendly cap.
  return v.slice(0, 200);
}

function normalizeMarkdownBlocks(raw: unknown): string[] {
  if (!Array.isArray(raw)) return [];
  const out: string[] = [];
  for (const item of raw) {
    if (out.length >= _COPY_BLOCKS_PER_PAGE_CAP) break;
    if (typeof item !== 'string') continue;
    const trimmed = item.trim();
    if (!trimmed) continue;
    out.push(trimmed.slice(0, _COPY_BLOCK_LENGTH_CAP));
  }
  return out;
}

function normalizeCopyHero(raw: unknown): CopyPage['hero'] | undefined {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return undefined;
  const o = raw as Record<string, unknown>;
  const hero: NonNullable<CopyPage['hero']> = {};
  if (typeof o.heading === 'string' && o.heading.trim()) {
    hero.heading = o.heading.trim().slice(0, 400);
  }
  if (typeof o.body_html === 'string' && o.body_html.trim()) {
    hero.body_html = o.body_html.trim().slice(0, _COPY_BLOCK_LENGTH_CAP);
  }
  if (Array.isArray(o.credential_lines)) {
    const lines: string[] = [];
    for (const l of o.credential_lines) {
      if (lines.length >= _CREDENTIAL_LINE_CAP) break;
      if (typeof l !== 'string') continue;
      const t = l.trim();
      if (!t) continue;
      lines.push(t.slice(0, _CREDENTIAL_LINE_LENGTH_CAP));
    }
    if (lines.length > 0) hero.credential_lines = lines;
  }
  if (typeof o.cta_text_me_phone === 'string') {
    const digits = o.cta_text_me_phone.replace(/\D+/g, '');
    // US phone min length 7 digits (local), max 15 (E.164 international).
    if (digits.length >= 7 && digits.length <= 15) {
      hero.cta_text_me_phone = digits;
    }
  }
  if (typeof o.cta_text === 'string' && o.cta_text.trim()) {
    hero.cta_text = o.cta_text.trim().slice(0, 80);
  }
  if (typeof o.cta_href === 'string' && o.cta_href.trim()) {
    const h = o.cta_href.trim();
    // Allow only safe schemes / relative paths. No javascript:, data:.
    if (/^(\/|https?:|sms:|tel:|mailto:)/i.test(h)) {
      hero.cta_href = h.slice(0, 600);
    }
  }
  return Object.keys(hero).length > 0 ? hero : undefined;
}

// --- tiny safe-readers (avoid `any`, never throw) ---

function readObject(raw: unknown): Record<string, unknown> {
  if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
    return raw as Record<string, unknown>;
  }
  return {};
}

function readArray(raw: unknown): unknown[] {
  return Array.isArray(raw) ? raw : [];
}

function readString(raw: unknown): string {
  return typeof raw === 'string' ? raw.trim() : '';
}

function copyStringInto<K extends keyof V1SeedSettings>(
  target: V1SeedSettings,
  key: K,
  raw: unknown
): void {
  if (target[key]) return; // first non-empty wins
  const v = readString(raw);
  if (v) target[key] = v as V1SeedSettings[K];
}
