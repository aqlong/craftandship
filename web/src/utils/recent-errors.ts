// In-memory buffer of the most recent unhandled request errors, read by
// GET /ops/api/v1/errors. It lives in its own module so the ops router and the
// app's error handler both use it without importing each other.
//
// The buffer is a log, and a URL is the commonest place a log picks up a
// secret: the password reset, email verification and unsubscribe links all
// carry their token in the query string. So an entry records the request
// PATH only (loggablePath), and this module also holds the one helper that
// redacts secret-bearing parameters from text that has to keep its URLs
// (redactSecretParams), which the email log and the Sentry scrubber share.
// It imports nothing, so anything may import it.

export interface RecentError {
  timestamp: string;
  method: string;
  path: string;
  message: string;
  stack?: string;
  sessionInfo?: { customerId: string | null; customerEmail: string | null };
}

const MAX_RECENT_ERRORS = 20;
/** Longest path, message and stack an entry keeps (a URL may be 16 KB). */
export const MAX_LOGGED_PATH_CHARS = 200;
export const MAX_LOGGED_MESSAGE_CHARS = 500;
export const MAX_LOGGED_STACK_CHARS = 2000;
const recentErrors: RecentError[] = [];

const cut = (text: string, max: number): string => (text.length > max ? `${text.slice(0, max)}...` : text);

/**
 * The part of a request URL that is safe to log: the path, with the query
 * string and any fragment removed, cut to MAX_LOGGED_PATH_CHARS. Works on a
 * path ("/reset-password?token=abc") and on an absolute URL.
 */
export function loggablePath(url: string | undefined | null): string {
  if (!url) return '';
  const end = url.search(/[?#]/);
  return cut(end === -1 ? url : url.slice(0, end), MAX_LOGGED_PATH_CHARS);
}

// Parameter names whose values are credentials: "token" and anything ending
// in it (reset_token, access_token), and the usual names for keys, secrets,
// signatures, one-time codes and passwords.
const SECRET_PARAM = '(?:[a-z0-9_.-]*token|api[_-]?key|key|secret|signature|sig|code|password|session_id)';
// The separator before the name and the "=" after it, in the forms a URL
// takes when it is written into HTML or nested inside another URL: plain
// ("?" "&" "="), entity-encoded ("&amp;" "&#38;" "&#x26;"), and
// percent-encoded once or twice ("%3F" "%26" "%3D", "%253F" ...).
const SEPARATOR = '(?:[?&]|&amp;|&#0*38;|&#x0*26;|%(?:25)?3f|%(?:25)?26)';
const EQUALS = '(?:=|%(?:25)?3d)';
// A value runs to the next separator, quote, angle bracket or blank. "%" is
// allowed (an encoded value), so an encoded separator ends it explicitly.
const SECRET_PARAM_VALUE = new RegExp(
  `(${SEPARATOR}${SECRET_PARAM}${EQUALS})(?:(?!%(?:25)?(?:26|3f))[^\\s"'<>&#;])+`,
  'gi'
);

/**
 * Replaces the value of every secret-bearing URL parameter in `text` with
 * "[redacted]" and leaves the rest of the text, the links included, as it
 * was. For text that must keep its URLs (stored email HTML, error reports).
 * Where a URL is only being logged, prefer loggablePath: dropping the whole
 * query string needs no list of names.
 */
export function redactSecretParams(text: string): string {
  return text.replace(SECRET_PARAM_VALUE, '$1[redacted]');
}

/**
 * Append an error, dropping the oldest once the buffer holds
 * MAX_RECENT_ERRORS. The path loses its query string here, whatever the
 * caller passed, and the free-text fields are cut to a fixed length.
 */
export function recordRecentError(entry: RecentError): void {
  recentErrors.push({
    ...entry,
    path: loggablePath(entry.path),
    message: cut(redactSecretParams(String(entry.message ?? '')), MAX_LOGGED_MESSAGE_CHARS),
    stack: entry.stack === undefined ? undefined : cut(redactSecretParams(entry.stack), MAX_LOGGED_STACK_CHARS),
  });
  if (recentErrors.length > MAX_RECENT_ERRORS) recentErrors.shift();
}

export function getRecentErrors(): RecentError[] {
  return recentErrors;
}
