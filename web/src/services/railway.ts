const RAILWAY_ENDPOINT = 'https://backboard.railway.com/graphql/v2';
// Cloudflare WAF blocks requests without a browser-like User-Agent (error 1010).
const RAILWAY_UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36';

export class RailwayApiError extends Error {
  readonly status?: number;
  readonly alreadyGone: boolean;
  readonly disabled: boolean;

  constructor(
    message: string,
    status?: number,
    alreadyGone = false,
    disabled = false,
  ) {
    super(message);
    this.name = 'RailwayApiError';
    this.status = status;
    this.alreadyGone = alreadyGone;
    this.disabled = disabled;
  }
}

// Railway writes require BOTH signals:
//   - NODE_ENV === 'production'           (the runtime is the real deal)
//   - CAS_ALLOW_RAILWAY_WRITES === 'true' (operator opted this env in)
//
// Either alone is not enough. Local CAS and staging often carry prod
// Railway project IDs from the nightly prod->local DB sync, and staging
// commonly runs NODE_ENV=production for Express performance; requiring
// the explicit env flag on top closes that loophole.
export function isRailwayWriteEnabled(): boolean {
  return (
    process.env.NODE_ENV === 'production' &&
    process.env.CAS_ALLOW_RAILWAY_WRITES === 'true'
  );
}

interface RailwayGraphQLResponse<T> {
  data?: T;
  errors?: Array<{ message?: string; extensions?: { code?: string } }>;
}

// A project is gone only when Railway says so with the GraphQL code NOT_FOUND
// (errors[].extensions.code). The message is not read: an access failure can
// use the same words ("Project not found or you do not have access"), and a
// caller that treats that as "already deleted" would remove its own record of
// a project that is still running.
function isNotFoundError(errors: RailwayGraphQLResponse<unknown>['errors']): boolean {
  if (!errors || errors.length === 0) return false;
  return errors.some((e) => (e.extensions?.code || '').toUpperCase() === 'NOT_FOUND');
}

interface RailwayReply {
  status: number;
  ok: boolean;
  rawBody: string;
  parsed: RailwayGraphQLResponse<unknown> | null;
}

// One GraphQL request to Railway, bounded by timeoutMs for the whole exchange
// (headers and body). The body is read in full and parsed when it is JSON.
async function sendRailwayQuery(
  query: string,
  variables: Record<string, unknown>,
  token: string,
  fetchImpl: typeof fetch,
  timeoutMs: number,
): Promise<RailwayReply> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let res: Response;
  try {
    res = await fetchImpl(RAILWAY_ENDPOINT, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
        'User-Agent': RAILWAY_UA,
      },
      body: JSON.stringify({ query, variables }),
      signal: controller.signal,
    });
  } catch (err) {
    clearTimeout(timer);
    throw new RailwayApiError(`Railway request failed: ${(err as Error).message}`);
  }
  let rawBody: string;
  try {
    rawBody = await res.text();
  } catch (err) {
    throw new RailwayApiError(`Railway request failed: ${(err as Error).message}`, res.status);
  } finally {
    clearTimeout(timer);
  }

  let parsed: RailwayGraphQLResponse<unknown> | null = null;
  try {
    parsed = rawBody ? (JSON.parse(rawBody) as RailwayGraphQLResponse<unknown>) : null;
  } catch {
    parsed = null;
  }
  return { status: res.status, ok: res.ok, rawBody, parsed };
}

export async function deleteRailwayProject(
  projectId: string,
  opts: { token?: string; timeoutMs?: number; fetchImpl?: typeof fetch } = {},
): Promise<void> {
  if (!isRailwayWriteEnabled()) {
    throw new RailwayApiError(
      'Railway writes disabled in this environment. They require both NODE_ENV=production and CAS_ALLOW_RAILWAY_WRITES=true.',
      undefined,
      false,
      true,
    );
  }
  const token = opts.token ?? process.env.RAILWAY_API_TOKEN;
  if (!token) throw new RailwayApiError('RAILWAY_API_TOKEN not set');
  const reply = await sendRailwayQuery(
    'mutation($id: String!) { projectDelete(id: $id) }',
    { id: projectId },
    token,
    opts.fetchImpl ?? fetch,
    opts.timeoutMs ?? 15000,
  );
  const { status, ok, rawBody, parsed } = reply;

  if (!ok) {
    if (parsed && isNotFoundError(parsed.errors)) {
      throw new RailwayApiError('Railway project not found (already deleted)', status, true);
    }
    throw new RailwayApiError(
      `Railway API ${status}: ${rawBody.slice(0, 300)}`,
      status,
    );
  }
  if (parsed?.errors?.length) {
    if (isNotFoundError(parsed.errors)) {
      throw new RailwayApiError('Railway project not found (already deleted)', status, true);
    }
    throw new RailwayApiError(
      `Railway GraphQL error: ${JSON.stringify(parsed.errors).slice(0, 300)}`,
      status,
    );
  }
  const deleted = parsed?.data as { projectDelete?: unknown } | undefined;
  if (deleted?.projectDelete !== true) {
    throw new RailwayApiError(
      `Railway projectDelete returned non-true: ${JSON.stringify(parsed?.data)}`,
      status,
    );
  }
}

/**
 * The name Railway gives a project, read back before anything is deleted from
 * it. Throws a RailwayApiError when the name cannot be read: alreadyGone only
 * on the NOT_FOUND code, and never for a reply that has no name.
 */
export async function readRailwayProjectName(
  projectId: string,
  opts: { token?: string; timeoutMs?: number; fetchImpl?: typeof fetch } = {},
): Promise<string> {
  const token = opts.token ?? process.env.RAILWAY_API_TOKEN;
  if (!token) throw new RailwayApiError('RAILWAY_API_TOKEN not set');
  const reply = await sendRailwayQuery(
    'query($id: String!) { project(id: $id) { name } }',
    { id: projectId },
    token,
    opts.fetchImpl ?? fetch,
    opts.timeoutMs ?? 15000,
  );
  const { status, ok, rawBody, parsed } = reply;

  if (!ok) {
    if (parsed && isNotFoundError(parsed.errors)) {
      throw new RailwayApiError('Railway project not found', status, true);
    }
    throw new RailwayApiError(`Railway API ${status}: ${rawBody.slice(0, 300)}`, status);
  }
  if (parsed?.errors?.length) {
    if (isNotFoundError(parsed.errors)) {
      throw new RailwayApiError('Railway project not found', status, true);
    }
    throw new RailwayApiError(
      `Railway GraphQL error: ${JSON.stringify(parsed.errors).slice(0, 300)}`,
      status,
    );
  }
  const project = (parsed?.data as { project?: { name?: unknown } | null } | undefined)?.project;
  const name = project?.name;
  if (typeof name !== 'string' || name.length === 0) {
    throw new RailwayApiError('Railway returned no name for the project', status);
  }
  return name;
}
