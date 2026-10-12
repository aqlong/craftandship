export type BuildTier = 'build' | 'grow' | 'scale';

export type BuildStatus =
  | 'intake'
  | 'payment_pending'
  | 'queued'
  | 'building'
  | 'testing'
  | 'deploying'
  | 'preview'
  | 'live'
  | 'failed'
  | 'abandoned';

export interface Customer {
  id: string;
  email: string;
  password: string;
  name: string;
  stripe_customer_id: string | null;
  created_at: Date;
}

export interface Build {
  id: string;
  customer_id: string;
  tier: BuildTier;
  status: BuildStatus;
  app_name: string | null;
  spec: object | null;
  spec_path: string | null;
  live_url: string | null;
  preview_url: string | null;
  preview_approved_at: Date | null;
  gallery_opted_in: boolean;
  gallery_featured: boolean;
  gallery_description: string | null;
  gallery_category: string | null;
  parent_build_id: string | null;
  rebuild_request: string | null;
  is_revision: boolean;
  qa_status: 'pending' | 'passed' | 'warned' | 'skipped' | null;
  qa_results: Array<{ name: string; passed: boolean; error: string | null }> | null;
  error_message: string | null;
  stripe_session_id: string | null;
  stripe_payment_intent: string | null;
  queue_job_id: string | null;
  created_at: Date;
  updated_at: Date;
}

export interface IntakeMessage {
  id: string;
  build_id: string;
  role: 'user' | 'assistant';
  content: string;
  created_at: Date;
}

export interface AppSpec {
  spec_version: string;
  app_name: string;
  description: string;
  user_roles: UserRole[];
  pages: Page[];
  data_models: DataModel[];
  actions: Action[];
  design?: {
    primary_color: string;
    secondary_color: string;
    accent_color: string;
    style: string;
    logo_data?: string;
  };
  settings: {
    auth: boolean;
    seed_data: boolean;
  };
}

export interface UserRole {
  role: string;
  description: string;
  can: string[];
}

export interface Page {
  path: string;
  title: string;
  purpose: string;
  auth_required: boolean;
  allowed_roles: string[];
}

export interface DataModel {
  name: string;
  fields: ModelField[];
  belongs_to: string | null;
  has_many: string[];
}

export interface ModelField {
  name: string;
  type: string;
  required: boolean;
}

export interface Action {
  name: string;
  description: string;
  trigger: string;
  role: string;
  steps: string[];
}

export interface BuildLog {
  id: number;
  build_id: string;
  stage: string;
  message: string;
  created_at: Date;
}

export interface AppHealth {
  status: string;
  last_checked: string | null;
  latency_ms: number | null;
  consecutive_failures: number;
}

export interface BuildApiResponse {
  status: BuildStatus;
  live_url: string | null;
  preview_url: string | null;
  error: string | null;
  admin_email: string | null;
  queue_position: number | null;
  progress_message: string;
  health: AppHealth | null;
  estimate_minutes: number | null;
  review_status: 'pending' | 'approved' | 'changes_requested' | null;
  reviewed_at: string | null;
  review_notes: string | null;
  retry_count: number;
  /**
   * What the build cost the platform in API spend. Internal: present only in
   * the response to an admin session, never sent to the build's owner.
   */
  total_api_cost_usd?: string | null;
  // reviewed_by intentionally omitted: the customer UI names no reviewer
  // ("Reviewed"), and the column holds an admin email address that must not
  // reach the customer-facing client.
}

export interface IntakeMessageResponse {
  reply: string;
  specComplete: boolean;
  spec: AppSpec | null;
}

export interface RevisionMessageResponse {
  reply: string;
  revisionReady: boolean;
  revisionSummary: string | null;
}

export type FeatureRequestStatus =
  | 'drafting'
  | 'scoped'
  | 'payment_pending'
  | 'queued'
  | 'building'
  | 'live'
  | 'failed';

export type FeatureSize = 'small' | 'medium' | 'large';

export interface FeatureRequest {
  id: string;
  build_id: string;
  customer_id: string;
  status: FeatureRequestStatus;
  description: string | null;
  scope_summary: string | null;
  estimated_cost: number | null;
  tier: string;
  created_at: Date;
  updated_at: Date;
}

export interface FeatureMessage {
  id: string;
  feature_request_id: string;
  role: 'user' | 'assistant';
  content: string;
  created_at: Date;
}

export interface FeatureScopeResponse {
  reply: string;
  scopeComplete: boolean;
  scope: FeatureScope | null;
}

export interface FeatureScope {
  description: string;
  size: FeatureSize;
  estimated_cost: number;
  summary: string;
}

export const FEATURE_PRICES: Record<FeatureSize, number> = {
  small: 4900,
  medium: 9900,
  large: 19900,
};

export const FEATURE_DISPLAY_PRICES: Record<FeatureSize, string> = {
  small: '$49',
  medium: '$99',
  large: '$199',
};

export const FEATURE_SIZE_LABELS: Record<FeatureSize, string> = {
  small: 'Small (bug fix, styling change, minor addition)',
  medium: 'Medium (new page, new functionality, API integration)',
  large: 'Large (major feature, significant redesign)',
};

// Tier pricing constants (TIER_PRICES, TIER_MONTHLY_PRICES, TIER_DISPLAY_*,
// TIER_PRIORITY, TIER_BUILD_ESTIMATES, TIER_LIMITS, TIER_REVISION_LIMITS) were
// removed 2026-05-01 along with the pre-Harbor /payment flow. Harbor sells one
// subscription product ($39 sliding to $19, see app.ts harborSubscriptionPrice
// / harborSlidingScalePrice). The `BuildTier` type alias above is kept only
// for DB column type safety on the `builds.tier` column; existing rows are all
// 'build' and the column is no longer rendered in any customer-facing surface.

// Build time estimate (single value; was tier-varying via TIER_BUILD_ESTIMATES).
// v2 compose engine builds in ~5-15 min; pad to 15 for the customer status page.
export const DEFAULT_BUILD_ESTIMATE_MINUTES = 15;

// Minimum complexity thresholds (warn if spec is too simple)
export const MIN_COMPLEXITY = {
  minPages: 2,
  minDataModels: 1,
};

declare module 'express-session' {
  interface SessionData {
    customerId: string;
    customerEmail: string;
    customerName: string;
    csrfToken: string;
    utmSource: string;
    utmMedium: string;
    utmCampaign: string;
    anonSessionId: string;
    anonBuildId: string;
    claimedAnonBuildId: string;
  }
}
