// The sitemap's per-URL lastmod (services/sitemap-lastmod.ts).
//
// What is checked, with a real child process each time (a stand-in program in
// a scratch directory takes git's place, so each case decides what "git"
// does):
//   - a request for a date never waits: it gets the fallback at once while
//     git is still running, and the event loop keeps turning;
//   - a git that never answers is cut off at its deadline, also when a
//     process it started keeps the output pipe open;
//   - without a usable answer from git (no repository, no program, too much
//     output, nonsense) the file's modification date is used, and without the
//     file the date this process started;
//   - git is run once per view file per process, however often dates are read;
//   - GET /sitemap.xml, through the app, answers while git hangs.

import { describe, it, expect, beforeAll, afterAll, afterEach, vi } from 'vitest';
import path from 'path';
import fs from 'fs';
import os from 'os';
import request from 'supertest';

vi.mock('../../src/db/index', () => ({ default: { query: vi.fn().mockResolvedValue({ rows: [] }), on: vi.fn() } }));

import app from '../../src/app';
import {
  getLastmodForUrl,
  warmSitemapLastmods,
  sitemapFallbackDate,
  URL_TO_VIEW_FILE,
  GIT_TIMEOUT_MS,
  GIT_MAX_OUTPUT_BYTES,
  _resetSitemapLastmodCache,
} from '../../src/services/sitemap-lastmod';

const VIEWS = [...new Set(Object.values(URL_TO_VIEW_FILE))];
const FILE_DATE = '2021-03-04';
const GIT_DATE = '2019-08-07';

let scratch: string;
let root: string;
let runs: string;

/** Write a stand-in for git: a shell script that notes each run, then does `body`. */
function standIn(name: string, body: string): string {
  const file = path.join(scratch, name);
  fs.writeFileSync(file, `#!/bin/sh\necho run >> "${runs}"\n${body}\n`, { mode: 0o755 });
  return file;
}
const runCount = (): number => (fs.existsSync(runs) ? fs.readFileSync(runs, 'utf8').split('\n').filter(Boolean).length : 0);

beforeAll(() => {
  scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'sitemap-lastmod-'));
  root = path.join(scratch, 'repo');
  runs = path.join(scratch, 'runs.log');
  // Every view file exists in the scratch root, last modified on FILE_DATE.
  for (const view of VIEWS) {
    const abs = path.join(root, view);
    fs.mkdirSync(path.dirname(abs), { recursive: true });
    fs.writeFileSync(abs, '<p>view</p>');
    const when = new Date(`${FILE_DATE}T12:00:00Z`);
    fs.utimesSync(abs, when, when);
  }
});

afterEach(() => {
  fs.rmSync(runs, { force: true });
  _resetSitemapLastmodCache();
});

afterAll(() => {
  fs.rmSync(scratch, { recursive: true, force: true });
});

describe('URL_TO_VIEW_FILE', () => {
  it('maps every canonical Harbor sitemap URL', () => {
    for (const u of ['/', '/pricing', '/the-companion', '/how-it-works', '/gallery', '/about', '/about/ocean', '/contact', '/terms', '/privacy']) {
      expect(URL_TO_VIEW_FILE[u]).toBeTruthy();
    }
  });

  it('every mapped view file actually exists on disk', () => {
    // Repo root: web/tests/unit/<this>.test.ts -> ../../.. = repo root.
    const repo = path.resolve(__dirname, '..', '..', '..');
    for (const [url, viewRel] of Object.entries(URL_TO_VIEW_FILE)) {
      expect(fs.existsSync(path.join(repo, viewRel)), `${url} -> ${viewRel} should exist`).toBe(true);
    }
  });
});

describe('a date is never waited for', () => {
  it('while git hangs, a read answers at once with the fallback and the event loop keeps turning; at the deadline the file dates take over', async () => {
    _resetSitemapLastmodCache({ root, gitBinary: standIn('git-hangs', 'exec sleep 30'), timeoutMs: 60 });

    const before = Date.now();
    const first = Object.keys(URL_TO_VIEW_FILE).map(getLastmodForUrl);
    expect(Date.now() - before).toBeLessThan(250);
    expect(new Set(first)).toEqual(new Set([sitemapFallbackDate()]));
    expect(sitemapFallbackDate()).toMatch(/^\d{4}-\d{2}-\d{2}$/);

    // The computation is under way and has not finished; timers still fire.
    let ticks = 0;
    const ticker = setInterval(() => {
      ticks += 1;
    }, 5);
    let done = false;
    const warm = warmSitemapLastmods().then(() => {
      done = true;
    });
    await new Promise((resolve) => setTimeout(resolve, 40));
    expect(done).toBe(false);
    expect(ticks).toBeGreaterThanOrEqual(2);

    // Every run is cut off at 60 ms: ten files, well under the 30 s each would sleep.
    await warm;
    clearInterval(ticker);
    expect(Date.now() - before).toBeLessThan(VIEWS.length * 60 + 2_000);
    for (const url of Object.keys(URL_TO_VIEW_FILE)) expect(getLastmodForUrl(url), url).toBe(FILE_DATE);
  });

  it('the deadline holds when a process git started keeps the output pipe open', async () => {
    // The script exits at once; the sleep it left behind still holds stdout.
    _resetSitemapLastmodCache({ root, gitBinary: standIn('git-leaves-a-child', 'sleep 4 &\nexit 0'), timeoutMs: 60 });
    const before = Date.now();
    await warmSitemapLastmods();
    expect(Date.now() - before).toBeLessThan(VIEWS.length * 60 + 2_000);
    expect(getLastmodForUrl('/pricing')).toBe(FILE_DATE);
  });

  it('GET /sitemap.xml answers while git hangs, with every URL and a well-formed date', async () => {
    _resetSitemapLastmodCache({ root, gitBinary: standIn('git-hangs-for-route', 'exec sleep 30'), timeoutMs: 80 });
    const before = Date.now();
    const res = await request(app).get('/sitemap.xml');
    expect(Date.now() - before).toBeLessThan(1_000);
    expect(res.status).toBe(200);
    const dates = [...res.text.matchAll(/<lastmod>([^<]*)<\/lastmod>/g)].map((m) => m[1]);
    expect(dates.length).toBeGreaterThanOrEqual(Object.keys(URL_TO_VIEW_FILE).length);
    for (const date of dates) expect(date).toBe(sitemapFallbackDate());
    // Once the background run is over, the same route serves the file dates.
    await warmSitemapLastmods();
    const later = await request(app).get('/sitemap.xml');
    expect(later.text).toContain(`<loc>https://craftandship.io/pricing</loc><lastmod>${FILE_DATE}</lastmod>`);
  });
});

describe('where a date comes from', () => {
  it('git\'s answer wins over the file date', async () => {
    _resetSitemapLastmodCache({ root, gitBinary: standIn('git-answers', `echo "${GIT_DATE}T10:11:12-05:00"`) });
    await warmSitemapLastmods();
    for (const url of Object.keys(URL_TO_VIEW_FILE)) expect(getLastmodForUrl(url), url).toBe(GIT_DATE);
  });

  it.each([
    ['no repository (git exits 128, as in a published tarball or a container image)', 'echo "fatal: not a git repository" >&2\nexit 128'],
    ['a file with no commit (git prints nothing)', 'exit 0'],
    ['an answer that is not a date', 'echo "not-a-date-at-all"'],
    ['more output than a date can be', `head -c ${GIT_MAX_OUTPUT_BYTES * 8} /dev/zero | tr '\\0' 'x'`],
  ])('%s: the file\'s modification date', async (_label, body) => {
    _resetSitemapLastmodCache({ root, gitBinary: standIn('git-useless', body) });
    await warmSitemapLastmods();
    expect(runCount()).toBe(VIEWS.length);
    for (const url of Object.keys(URL_TO_VIEW_FILE)) expect(getLastmodForUrl(url), url).toBe(FILE_DATE);
  });

  it('no git program at all: the file\'s modification date', async () => {
    _resetSitemapLastmodCache({ root, gitBinary: path.join(scratch, 'there-is-no-git-here') });
    await warmSitemapLastmods();
    expect(getLastmodForUrl('/terms')).toBe(FILE_DATE);
  });

  it('no git and no such directory: the date this process started', async () => {
    _resetSitemapLastmodCache({ root: path.join(scratch, 'no-such-root'), gitBinary: path.join(scratch, 'there-is-no-git-here') });
    await warmSitemapLastmods();
    expect(getLastmodForUrl('/terms')).toBe(sitemapFallbackDate());
  });

  it('a URL with no view file gets the date this process started, and starts nothing', () => {
    _resetSitemapLastmodCache({ root, gitBinary: standIn('git-unused', `echo "${GIT_DATE}T00:00:00Z"`) });
    expect(getLastmodForUrl('/some-unknown-route-9999')).toBe(sitemapFallbackDate());
    expect(runCount()).toBe(0);
  });
});

describe('how often git runs', () => {
  it('once per view file per process, however many times the dates are read or the warm-up is asked for', async () => {
    _resetSitemapLastmodCache({ root, gitBinary: standIn('git-counted', `echo "${GIT_DATE}T00:00:00Z"`) });
    for (let i = 0; i < 25; i += 1) Object.keys(URL_TO_VIEW_FILE).forEach(getLastmodForUrl);
    await Promise.all([warmSitemapLastmods(), warmSitemapLastmods(), warmSitemapLastmods()]);
    for (let i = 0; i < 25; i += 1) Object.keys(URL_TO_VIEW_FILE).forEach(getLastmodForUrl);
    await warmSitemapLastmods();
    expect(runCount()).toBe(VIEWS.length);
  });

  it('the shipped deadline is seconds, not minutes', () => {
    expect(GIT_TIMEOUT_MS).toBeGreaterThanOrEqual(1_000);
    expect(GIT_TIMEOUT_MS).toBeLessThanOrEqual(5_000);
  });
});

describe('in this checkout', () => {
  it('with the real program and the real tree, every known URL ends up with a well-formed date', async () => {
    _resetSitemapLastmodCache();
    await warmSitemapLastmods();
    for (const url of Object.keys(URL_TO_VIEW_FILE)) {
      expect(getLastmodForUrl(url), url).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    }
  });
});
