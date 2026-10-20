// Sentry initialization: must be imported BEFORE any other app code in server.ts
//
// Because it is the first import, this is also the first code that reads the
// environment. web/.env is loaded here, so a SENTRY_DSN set only in that file
// is seen; app.ts and db/index.ts call dotenv.config() again, which never
// overrides a variable that is already set.
import dotenv from 'dotenv';
import * as Sentry from '@sentry/node';
import { loggablePath, redactSecretParams } from './utils/recent-errors';

dotenv.config();

type ScrubbableEvent = Pick<Sentry.Event, 'request' | 'contexts' | 'spans' | 'breadcrumbs' | 'message' | 'exception'>;

/**
 * The request headers an event or a span may keep; every other one is
 * deleted. Requests to this app carry credentials in headers: the ops API key
 * and the worker status hook's secret as `Authorization: Bearer`, Stripe's and
 * Svix's webhook signatures, the CSRF token and the session cookie. A list of
 * what may stay, rather than of what must go, also keeps out a header the app
 * starts to read later. Lower case, as Node reports header names.
 */
const KEPT_REQUEST_HEADERS: ReadonlySet<string> = new Set([
  'user-agent',
  'accept',
  'accept-language',
  'content-type',
  'content-length',
  'host',
  'sec-fetch-site',
  'sec-fetch-mode',
  'sec-fetch-dest',
  'referer',
]);

/** Span attributes the SDK writes for request headers: `http.request.header.<name>`, dashes as underscores. */
const REQUEST_HEADER_ATTRIBUTE = /^http\.request\.header\.(.+)$/i;

/** Attributes a span or breadcrumb may carry: URLs, query strings, request headers, and a request's body or cookies among them. */
function scrubAttributes(data: Record<string, unknown> | undefined): void {
  if (!data) return;
  for (const [name, value] of Object.entries(data)) {
    const header = REQUEST_HEADER_ATTRIBUTE.exec(name);
    if (header && !KEPT_REQUEST_HEADERS.has(header[1].toLowerCase().replace(/_/g, '-'))) {
      delete data[name];
      continue;
    }
    // `token(?!s\b)`: a token, not a model call's token count (gen_ai.usage.input_tokens).
    if (/body|cookie|authorization|token(?!s\b)|signature|api[-_]?key/i.test(name)) {
      data[name] = '[redacted]';
      continue;
    }
    if (typeof value !== 'string') continue;
    data[name] = /query/i.test(name) ? '[redacted]' : redactSecretParams(value);
  }
}

/**
 * Removes what a request URL can leak from an event before it leaves the
 * process. The password reset, email verification and unsubscribe links carry
 * their token in the query string, and a 5xx on one of those URLs would
 * otherwise send a live token to Sentry: the request URL keeps its path only,
 * the query string and the Referer are dropped or cut the same way, and any
 * URL inside the error message or span or breadcrumb data has its secret
 * parameters redacted.
 *
 * It also removes what the visitor sent. The SDK's HTTP integration records
 * the body of an incoming request (up to 10 kB by default) and attaches it to
 * the event, with `sendDefaultPii: false` as well; here that body is a
 * message to the companion, a password on the login form, or a crisis
 * disclosure. The body and the cookies are deleted from the event, and the
 * same from span data.
 *
 * And it removes the credentials a request carries in its headers. With
 * `sendDefaultPii: false` the SDK still copies the request's headers onto the
 * event, Authorization and Cookie included, on a transaction as well as on an
 * error. Every request header outside KEPT_REQUEST_HEADERS is deleted from the
 * event and from span data, and span or breadcrumb data whose name speaks of
 * authorization, a token, a signature or an API key is redacted. (The SDK is
 * also told, below, not to record request bodies or attach request headers at
 * all; this is the check that holds whatever the SDK's defaults become.)
 * Exported for tests.
 */
export function scrubSentryEvent<T extends ScrubbableEvent>(event: T): T {
  const request = event.request;
  if (request) {
    if (typeof request.url === 'string') request.url = loggablePath(request.url);
    if (request.query_string !== undefined) request.query_string = '[redacted]';
    delete request.data;
    delete request.cookies;
    if (request.headers) {
      for (const name of Object.keys(request.headers)) {
        const lowerName = name.toLowerCase();
        if (!KEPT_REQUEST_HEADERS.has(lowerName)) delete request.headers[name];
        else if (lowerName === 'referer') request.headers[name] = loggablePath(request.headers[name]);
      }
    }
  }
  if (typeof event.message === 'string') event.message = redactSecretParams(event.message);
  for (const thrown of event.exception?.values ?? []) {
    if (typeof thrown.value === 'string') thrown.value = redactSecretParams(thrown.value);
  }
  scrubAttributes(event.contexts?.trace?.data as Record<string, unknown> | undefined);
  for (const span of event.spans ?? []) scrubAttributes(span.data as Record<string, unknown> | undefined);
  for (const breadcrumb of event.breadcrumbs ?? []) scrubAttributes(breadcrumb.data);
  return event;
}

/**
 * The options Sentry.init receives (the transport is the SDK's default).
 * Exported so a test can run the real SDK with exactly these options and an
 * in-memory transport.
 */
export function sentryInitOptions(dsn: string | undefined, nodeEnv: string | undefined): Sentry.NodeOptions {
  // Only an explicit development or test run is labelled as one; an unset
  // NODE_ENV is a production deployment everywhere else in this app.
  const developmentOrTest = nodeEnv === 'development' || nodeEnv === 'test';
  return {
    dsn,
    environment: nodeEnv || 'production',
    tracesSampleRate: developmentOrTest ? 1.0 : 0.2,
    sendDefaultPii: false,
    integrations: [
      // Replaces the default HTTP integration with one that does not record
      // the bodies of incoming requests (see scrubSentryEvent).
      Sentry.httpIntegration({ maxIncomingRequestBodySize: 'none' }),
      // Replaces the default request-data integration with one that does not
      // copy the request's headers onto events (see scrubSentryEvent).
      Sentry.requestDataIntegration({ include: { headers: false } }),
    ],
    beforeSend: (event) => scrubSentryEvent(event),
    beforeSendTransaction: (event) => scrubSentryEvent(event),
  };
}

if (process.env.SENTRY_DSN) {
  Sentry.init(sentryInitOptions(process.env.SENTRY_DSN, process.env.NODE_ENV));
}
