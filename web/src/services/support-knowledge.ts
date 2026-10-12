// services/support-knowledge.ts - Dynamic knowledge base for support chat.
//
// Reads pricing from the canonical TypeScript constants so the support chat
// always has current facts. When pricing changes, the support knowledge updates
// automatically.

import { createHash } from 'crypto';
import { FEATURE_DISPLAY_PRICES, FEATURE_SIZE_LABELS } from '../types';

// Harbor pricing constants. Mirror the values rendered by /pricing
// (harborSubscriptionPrice / harborSlidingScalePrice in app.ts). Keep in sync.
const HARBOR_DEFAULT_PRICE = '$39';
const HARBOR_FLOOR_PRICE = '$19';

export function generateKnowledge(): string {
  return `## What is Craft & Ship?
Craft & Ship builds custom websites for healers, therapists, breathwork facilitators, and event producers. You describe what you do in plain English through a conversation with our AI companion (Haven), your site is built from a library of tested building blocks matched to what you describe, and every website needs a recorded review approval before it goes live.

## Pricing

One subscription covers everything: a custom website, hosting, an AI companion who helps you find the words, unlimited revisions and rebuilds, and a recorded review approval for every website before it goes live.

- Choose any amount from ${HARBOR_DEFAULT_PRICE} a month down to ${HARBOR_FLOOR_PRICE} a month, whatever fits your practice.
- The first build is free in preview while you walk around it. You only pay when you say "make it public."
- Your website stays up if you ever pause or cancel.

### Feature Requests (for live websites)
After your website is live, you can request new features:
- ${FEATURE_SIZE_LABELS.small}: ${FEATURE_DISPLAY_PRICES.small}
- ${FEATURE_SIZE_LABELS.medium}: ${FEATURE_DISPLAY_PRICES.medium}
- ${FEATURE_SIZE_LABELS.large}: ${FEATURE_DISPLAY_PRICES.large}

## Technology Stack
Every website is built with Node.js, Express, PostgreSQL, and EJS templates, deployed to Railway with isolated infrastructure including a dedicated database.

## What is Included
- A custom website, shaped with you in conversation and built from a library of tested building blocks
- Every website needs a recorded review approval before it goes live
- Hosting and care included, with uptime watched day and night
- Unlimited revisions and rebuilds, kept across the years
- An AI companion (Haven) who helps you find the words, whenever you need her
- Yours: if you ever leave, we hand the whole site over intact

## How the Process Works
1. Begin a conversation at /lighthouse/table with Haven, the AI companion
2. Talk in plain English about your work, offerings, clients, and the rhythm of your week (no jargon, no checkboxes, no time limit)
3. When the spec is clear, your website is built (free, in preview)
4. Walk around your preview
5. When you are happy, click "Make it public" and pick the monthly amount that fits your practice
6. Your site goes live; Haven leaves a blessing turn on your conversation

## After Deployment
- Your website is live, hosted by us
- Request changes anytime; unlimited revisions and rebuilds
- Each site runs on its own isolated infrastructure with its own database
- Data is secure: parameterized queries, CSRF protection, secure sessions

## Build Failures and Retries
- If a build fails, you can retry it from the dashboard or status page at no extra cost.
- The platform automatically retries transient errors (network timeouts, infrastructure issues) up to 3 times.
- If a build fails after all retries, you will see a detailed error message and can request a rebuild.
- Rebuilds are free; they reuse your existing spec and run the full build pipeline again.

## Intake Tips
- Be specific when describing your work: your offerings, your clients, the rhythm of your week.
- You can share photos and texture references during the conversation.
- The conversation usually takes 5 to 15 turns; you can leave and resume anytime from your dashboard.

## Preview and Revisions
- After your website is built, we email you its preview address and it enters "preview" status while it needs a recorded review approval before it goes public.
- You can request unlimited free revisions during preview and across the years afterward.
- Every website needs a recorded review approval before it goes public.
- Click "Make it public" when the website feels right.

## Generated Website Features
- Every site includes admin login (magic link first, then a permanent password).
- All sites are responsive: they work on mobile and desktop.
- Admin can manage content, upload images, and reset site data from the admin panel.

## Account Management
- You can export all your data (profile, builds, conversations) as JSON from the dashboard.
- To delete your account, visit the account settings page; this permanently removes all your data.
- Password reset is available via the "Forgot Password" link on the login page.

## Support
- Email: support@craftandship.io
- The support chat (this widget) is available on every page`;
}

// SHA-256 hash of the current generateKnowledge() output. The startup task in
// server.ts compares this against the value persisted in platform_metadata; if
// they differ, the support_kb cache is truncated so stale answers never linger
// across deploys after pricing or copy changes.
export function getKnowledgeContentHash(): string {
  return createHash('sha256').update(generateKnowledge(), 'utf8').digest('hex');
}
