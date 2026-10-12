// Which requests are answered in JSON when the answer does not come from the
// route itself.
//
// A route knows what it returns. The code around the routes does not: the
// body parsers (a malformed or over-size body), the CSRF token check, the
// per-customer model limiter, the 404 handler and the app's error handler all
// have to decide, for a request they know nothing about, whether its caller is
// a page or a script. A script that is handed the HTML error page cannot read
// it, and shows its own "something went wrong" in place of the reason.
//
// isJsonRequest is the one place that decision is made. A request is a JSON
// request when any of these holds:
//
//   1. It says so: a JSON Content-Type on the request, or the x-csrf-token
//      header, which only this application's own fetch calls send (a form
//      carries its token in the body).
//   2. Its path is under a prefix where every route answers JSON: /api/,
//      /ops/api/, /webhooks/ and /admin/api/.
//   3. It is one of the POST endpoints the conversation, intake and chat
//      pages call with fetch (JSON_POST_ENDPOINTS). They are listed because
//      not every such call has a JSON body: the keeper's build button posts
//      no body at all, and the logo upload is multipart.
//   4. It asks for it in the query: ?format=json, which the recall page's
//      refresh uses.
//
// Everything else is a page or a form, and keeps the HTML answer.
//
// What does not use it: a route that redirects a logged-out caller to the
// login page (requireAuth, requireAdmin) does so for scripts too.

import type { Request, Response } from 'express';
import { clientErrorSentence } from './error-page';

const JSON_PATH_PREFIXES = ['/api/', '/ops/api/', '/webhooks/', '/admin/api/'] as const;

/** POST endpoints that answer JSON whatever the request's content type. */
const JSON_POST_ENDPOINTS: readonly RegExp[] = [
  // The Haven conversation page and its template picker.
  /^\/lighthouse\/(?:table\/turn|build|bypass|preview\/choose)$/,
  // The intake pages, signed in and anonymous.
  /^\/intake\/(?:start|message|[^/]+\/logo)$/,
  /^\/try\/(?:start|[^/]+\/message)$/,
  // The revision and feature chats, and the feedback widget.
  /^\/builds\/[^/]+\/revision\/message$/,
  /^\/features\/[^/]+\/message$/,
  /^\/feedback$/,
];

const JSON_CONTENT_TYPE = /^application\/(?:[a-z0-9.!#$&^_-]+\+)?json(?:\s*;|\s*$)/i;

type JsonRequestInput = Pick<Request, 'method' | 'headers'> & { originalUrl?: string; url?: string };

/** The request path as the client sent it, without the query string, whatever router the caller is mounted in. */
function fullPath(req: JsonRequestInput): string {
  const raw = req.originalUrl || req.url || '';
  const end = raw.indexOf('?');
  return end === -1 ? raw : raw.slice(0, end);
}

function queryAsksForJson(req: JsonRequestInput): boolean {
  const raw = req.originalUrl || req.url || '';
  const start = raw.indexOf('?');
  if (start === -1) return false;
  return raw
    .slice(start + 1)
    .split('&')
    .some((pair) => pair === 'format=json');
}

/** True when this request's caller expects JSON back. See the top of this file for the rule. */
export function isJsonRequest(req: JsonRequestInput): boolean {
  const contentType = req.headers['content-type'];
  if (typeof contentType === 'string' && JSON_CONTENT_TYPE.test(contentType.trim())) return true;
  if (req.headers['x-csrf-token']) return true;
  const path = fullPath(req);
  if (JSON_PATH_PREFIXES.some((prefix) => path.startsWith(prefix))) return true;
  if (req.method === 'POST' && JSON_POST_ENDPOINTS.some((pattern) => pattern.test(path))) return true;
  return queryAsksForJson(req);
}

const CLIENT_ERROR_CODES: Record<number, string> = {
  400: 'bad_request',
  403: 'forbidden',
  404: 'not_found',
  409: 'conflict',
  413: 'payload_too_large',
  415: 'unsupported_media_type',
  429: 'rate_limited',
};

/**
 * Answer a JSON request with an error the caller did not get from a route.
 * The sentence is under `error` (what the legacy chat clients display) and
 * `message` (what the Haven client displays), with a stable `code`. For a 4xx
 * the default sentence is the one the HTML error page prints for that status.
 */
export function sendJsonError(res: Response, status: number, sentence?: string): void {
  const text = sentence ?? (status >= 500 ? 'An unexpected error occurred.' : clientErrorSentence(status));
  const code = status >= 500 ? 'server_error' : CLIENT_ERROR_CODES[status] ?? 'request_refused';
  res.status(status).json({ error: text, message: text, code });
}
