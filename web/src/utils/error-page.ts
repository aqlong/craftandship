// Error pages for responses the client caused (HTTP 4xx).
//
// views/500.ejs is the page for a server fault and prints "500". A refused
// request (forbidden, missing, conflicting, too large, rate limited) used to
// borrow that view, so a 403 body read "500 Something went sideways." These
// responses render views/error.ejs instead: the numeral is the status the
// response is sent with, and the heading and message say what happened.
//
// Rendering goes through this one function so the status on the wire and the
// numeral on the page are the same value by construction.

import type { Response } from 'express';

interface ClientErrorCopy {
  title: string;
  heading: string;
  message: string;
}

const CLIENT_ERROR_COPY: Record<number, ClientErrorCopy> = {
  400: {
    title: 'Bad Request',
    heading: 'That request could not be processed.',
    message: 'Something in it could not be read. Please check it and try again.',
  },
  403: {
    title: 'Forbidden',
    heading: 'That request was not allowed.',
    message: 'This page is not open to your account. If you think it should be, write to Ocean.',
  },
  404: {
    title: 'Not Found',
    heading: 'That page is missing.',
    message: 'It might have moved, or the link could be off by a letter.',
  },
  409: {
    title: 'Not Possible Right Now',
    heading: 'That is not possible right now.',
    message: 'The request does not fit where things stand. Check the page you came from and try again.',
  },
  413: {
    title: 'Too Large',
    heading: 'That was too large to accept.',
    message: 'What you sent is larger than we can take in one go. Please send less at a time.',
  },
  429: {
    title: 'Please Wait',
    heading: 'Please wait a moment.',
    message: 'There have been a lot of requests in a short time. Please try again in a little while.',
  },
};

const FALLBACK_COPY: ClientErrorCopy = {
  title: 'Request Not Completed',
  heading: 'That request could not be completed.',
  message: 'Please check it and try again.',
};

/**
 * The page's wording for a status as one sentence group ("heading message"),
 * for an answer that is not a page (utils/json-request.ts).
 */
export function clientErrorSentence(status: number): string {
  const copy = CLIENT_ERROR_COPY[status] ?? FALLBACK_COPY;
  return `${copy.heading} ${copy.message}`;
}

export interface ErrorPageOptions {
  /** Browser title. Defaults to a short name for the status. */
  title?: string;
  /** The sentence under the heading. Defaults to a plain line for the status. */
  error?: string;
  /** The page heading. Defaults to a plain line for the status. */
  heading?: string;
}

/**
 * Answer with a 4xx status and a page whose numeral and wording match it.
 * A 5xx belongs on views/500.ejs, so any other status is a programming error.
 */
export function renderErrorPage(res: Response, status: number, options: ErrorPageOptions = {}): void {
  if (!Number.isInteger(status) || status < 400 || status > 499) {
    throw new RangeError(`renderErrorPage is for client errors (4xx); got ${status}`);
  }
  const copy = CLIENT_ERROR_COPY[status] ?? FALLBACK_COPY;
  res.status(status).render('error', {
    title: options.title ?? copy.title,
    errorStatus: status,
    errorHeading: options.heading ?? copy.heading,
    errorMessage: options.error ?? copy.message,
  });
}
