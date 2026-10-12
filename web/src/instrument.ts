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

/** Attributes a span or breadcrumb may carry: URLs, query strings, and a request's body or cookies among them. */
function scrubAttributes(data: Record<string, unknown> | undefined): void {
  if (!data) return;
  for (const [name, value] of Object.entries(data)) {
    if (/body|cookie/i.test(name)) {
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
 * disclosure. The body and the cookies are deleted from the event, the
 * Cookie header with them, and the same from span data. (The integration is
 * also told not to record bodies at all, below; this is the check that holds
 * whatever the SDK's defaults become.) Exported for tests.
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
        if (/^refer+er$/i.test(name)) request.headers[name] = loggablePath(request.headers[name]);
        if (/^(?:set-)?cookie$/i.test(name)) delete request.headers[name];
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

if (process.env.SENTRY_DSN) {
  // Only an explicit development or test run is labelled as one; an unset
  // NODE_ENV is a production deployment everywhere else in this app.
  const nodeEnv = process.env.NODE_ENV;
  const developmentOrTest = nodeEnv === 'development' || nodeEnv === 'test';
  Sentry.init({
    dsn: process.env.SENTRY_DSN,
    environment: nodeEnv || 'production',
    tracesSampleRate: developmentOrTest ? 1.0 : 0.2,
    sendDefaultPii: false,
    // Replaces the default HTTP integration with one that does not record
    // the bodies of incoming requests (see scrubSentryEvent).
    integrations: [Sentry.httpIntegration({ maxIncomingRequestBodySize: 'none' })],
    beforeSend: (event) => scrubSentryEvent(event),
    beforeSendTransaction: (event) => scrubSentryEvent(event),
  });
}
