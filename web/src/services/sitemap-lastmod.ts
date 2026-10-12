// Per-URL sitemap lastmod, from the last commit that touched each route's
// view file, without ever making a request wait for git.
//
// Why per-URL: identical lastmod on every URL is a low-value freshness signal
// at best and a spam tell at worst. Search engines (Google in particular)
// explicitly say "lastmod is optional; only include accurate values".
//
// How it is computed:
//   - Once per process, in the background (warmSitemapLastmods): for each
//     view file, `git log -1 --format=%aI -- <view-file>` run as a child
//     process with a deadline, one after another. Where git gives no date (no
//     git binary, no .git directory as in a published tarball or a container
//     image, a file with no commit, a run that passed its deadline), the
//     file's modification time is used, and where there is no such file, the
//     date this process started.
//   - server.ts starts that computation after the port is bound. A request
//     for /sitemap.xml starts it too if nothing has (getLastmodForUrl), and
//     never waits for it: until a file's date is known the answer is the date
//     this process started.
//
// Nothing here runs synchronously on a request: no execFileSync, no
// statSync. The web process has one event loop, and the crisis handler shares
// it with this route.
//
// Bounds: each git run gets GIT_TIMEOUT_MS and is killed at the deadline; its
// output is capped at GIT_MAX_OUTPUT_BYTES (a date is 25 characters); there
// are as many runs as view files below, once per process.

import { execFile } from 'child_process';
import path from 'path';
import fs from 'fs';

// Map sitemap URL -> repo-relative view file. Keep alongside the URL list in
// app.ts so the two cannot drift; a URL that is in the sitemap and not here
// gets the date this process started.
export const URL_TO_VIEW_FILE: Record<string, string> = {
  '/': 'web/views/index.ejs',
  '/pricing': 'web/views/pricing.ejs',
  '/the-companion': 'web/views/the-companion.ejs',
  '/how-it-works': 'web/views/how-it-works.ejs',
  '/gallery': 'web/views/gallery.ejs',
  '/about': 'web/views/about.ejs',
  '/about/ocean': 'web/views/about-ocean.ejs',
  '/contact': 'web/views/contact.ejs',
  '/terms': 'web/views/terms.ejs',
  '/privacy': 'web/views/privacy.ejs',
};

export const GIT_TIMEOUT_MS = 3_000;
export const GIT_MAX_OUTPUT_BYTES = 4_096;

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

/** Where the dates come from. Tests point these at a scratch directory and a stand-in program. */
export interface LastmodSource {
  /** The directory the view paths are relative to, and git's working directory. */
  root: string;
  /** The git program to run. */
  gitBinary: string;
  /** Deadline for one git run, in milliseconds. */
  timeoutMs: number;
}

// Resolve the repo root from this file's location: web/src/services/<this>.ts
// -> ../../.. = repo root. The compiled layout (web/dist/services) is the
// same depth.
function defaultSource(): LastmodSource {
  return { root: path.resolve(__dirname, '..', '..', '..'), gitBinary: 'git', timeoutMs: GIT_TIMEOUT_MS };
}

function isoDate(when: Date): string {
  return when.toISOString().slice(0, 10);
}

/** The fallback of last resort: the date this process started (a deployment's files are no newer). */
const PROCESS_START_DATE = isoDate(new Date());

let source: LastmodSource = defaultSource();
const resolved = new Map<string, string>();
let warming: Promise<void> | null = null;

/**
 * The last commit date of one file, or null. Never throws, and answers by
 * the deadline whatever the child does: at `timeout`, execFile kills the
 * child (SIGKILL, which cannot be ignored) and closes its end of the output
 * pipes, so its callback fires even when a process the child started still
 * holds a pipe open. `maxBuffer` ends a child that prints more than a date
 * could be, the same way.
 */
function gitDate(viewRelPath: string, from: LastmodSource): Promise<string | null> {
  return new Promise((resolve) => {
    try {
      const child = execFile(
        from.gitBinary,
        ['log', '-1', '--format=%aI', '--', viewRelPath],
        {
          cwd: from.root,
          encoding: 'utf8',
          timeout: from.timeoutMs,
          killSignal: 'SIGKILL',
          maxBuffer: GIT_MAX_OUTPUT_BYTES,
          windowsHide: true,
        },
        (err, stdout) => {
          if (err) {
            resolve(null);
            return;
          }
          const date = String(stdout).trim().slice(0, 10);
          resolve(ISO_DATE.test(date) ? date : null);
        }
      );
      child.stdin?.end();
    } catch {
      // A working directory that does not exist, or a program name the
      // platform refuses, is reported by a throw on some platforms.
      resolve(null);
    }
  });
}

/** The file's modification date, or null when it cannot be read. */
async function fileDate(absPath: string): Promise<string | null> {
  try {
    return isoDate((await fs.promises.stat(absPath)).mtime);
  } catch {
    return null;
  }
}

async function computeAll(from: LastmodSource, views: readonly string[]): Promise<void> {
  // One after another: at most one child process at a time.
  for (const viewRelPath of views) {
    const date =
      (await gitDate(viewRelPath, from)) ?? (await fileDate(path.join(from.root, viewRelPath))) ?? PROCESS_START_DATE;
    // A test may have pointed the module elsewhere while this was running.
    if (from === source) resolved.set(viewRelPath, date);
  }
}

/**
 * Start computing the dates, once per process. Returns the promise of the
 * run, for the caller that wants to know when it is done (server.ts does not
 * wait; tests do). It never rejects.
 */
export function warmSitemapLastmods(): Promise<void> {
  if (!warming) {
    warming = computeAll(source, [...new Set(Object.values(URL_TO_VIEW_FILE))]).catch(() => undefined);
  }
  return warming;
}

/**
 * The lastmod for a sitemap URL, from what is known right now. Synchronous
 * and immediate: it starts the background computation if nothing has, and
 * answers with the date this process started until the file's date is in.
 */
export function getLastmodForUrl(url: string): string {
  const viewFile = URL_TO_VIEW_FILE[url];
  if (!viewFile) return PROCESS_START_DATE;
  void warmSitemapLastmods();
  return resolved.get(viewFile) ?? PROCESS_START_DATE;
}

/** The date used while a file's own date is not known, and for a URL with no view file. */
export function sitemapFallbackDate(): string {
  return PROCESS_START_DATE;
}

// Test seams: forget what was computed, optionally reading from another
// directory or running another program from then on.
export function _resetSitemapLastmodCache(override?: Partial<LastmodSource>): void {
  source = { ...defaultSource(), ...override };
  resolved.clear();
  warming = null;
}
