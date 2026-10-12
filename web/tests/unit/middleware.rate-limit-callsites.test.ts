import { describe, it, expect } from 'vitest';
import fs from 'fs';
import path from 'path';

// Every rate limiter in web/src must be built by boundedRateLimit
// (src/middleware/companion-rate-limit.ts). That factory is what gives a
// limiter a store with a ceiling on its keys and a ceiling on the length of
// each key; express-rate-limit used directly gives it neither (its memory
// store keeps every key it is sent, about 250 bytes each plus the key).
//
// This file reads the source tree. It imports nothing from src on purpose:
// the rule it checks is about which files may reach the library at all, and
// it has to be able to say so about a tree that does not have the factory.

const srcRoot = path.join(__dirname, '../../src');
const FACTORY_FILE = path.join('middleware', 'companion-rate-limit.ts');

function listTs(dir: string): string[] {
  const out: string[] = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...listTs(full));
    else if (entry.name.endsWith('.ts')) out.push(full);
  }
  return out;
}

interface SourceFile {
  relative: string;
  lines: string[];
}

const sources: SourceFile[] = listTs(srcRoot).map((file) => ({
  relative: path.relative(srcRoot, file),
  lines: fs.readFileSync(file, 'utf-8').split('\n'),
}));

/** Lines of code: a line that is only a comment says nothing about what runs. */
const isComment = (line: string): boolean => /^\s*(\/\/|\*|\/\*)/.test(line);

function linesMatching(pattern: RegExp, keep: (file: SourceFile) => boolean = () => true): string[] {
  const hits: string[] = [];
  for (const file of sources) {
    if (!keep(file)) continue;
    file.lines.forEach((line, index) => {
      if (!isComment(line) && pattern.test(line)) hits.push(`${file.relative}:${index + 1}: ${line.trim()}`);
    });
  }
  return hits;
}

// A call of the library's default export under its usual name. The lookbehind
// keeps out boundedRateLimit( and makeCompanionTurnRateLimit(, whose names
// end the same way.
const DIRECT_CALL = /(?<![A-Za-z0-9_$.])rateLimit\s*\(/;
// The package named as a module specifier, however it is imported.
const LIBRARY_SPECIFIER = /['"]express-rate-limit['"]/;

describe('rate limiter call sites in web/src', () => {
  it('finds the source tree and the factory', () => {
    expect(sources.length).toBeGreaterThan(40);
    expect(sources.map((file) => file.relative)).toContain(FACTORY_FILE);
  });

  it('only the factory module imports express-rate-limit (a type-only import aside)', () => {
    const offenders = linesMatching(
      LIBRARY_SPECIFIER,
      (file) => file.relative !== FACTORY_FILE
    ).filter((hit) => !/:\s*import type\b/.test(hit));
    expect(offenders).toEqual([]);
  });

  it('rateLimit( is called in exactly one place, inside boundedRateLimit, with the bounded store', () => {
    expect(linesMatching(DIRECT_CALL, (file) => file.relative !== FACTORY_FILE)).toEqual([]);

    const factory = sources.find((file) => file.relative === FACTORY_FILE) as SourceFile;
    const calls = factory.lines
      .map((line, index) => ({ line, index }))
      .filter(({ line }) => !isComment(line) && DIRECT_CALL.test(line));
    expect(calls).toHaveLength(1);

    // The one call is the body of boundedRateLimit, and its options name the
    // bounded store and the key wrapper.
    const text = factory.lines.join('\n');
    const start = text.indexOf('export function boundedRateLimit(');
    expect(start).toBeGreaterThan(-1);
    const end = text.indexOf('\n}\n', start);
    const body = text.slice(start, end);
    expect(body).toMatch(DIRECT_CALL);
    expect(body).toContain('store: new BoundedRateLimitStore()');
    expect(body).toContain('boundedKey(');
    const callLine = text.slice(0, text.indexOf('return rateLimit(', start)).split('\n').length - 1;
    expect(calls[0].index).toBe(callLine);
  });

  it("the library's own memory store is not used anywhere", () => {
    expect(linesMatching(/\bMemoryStore\b/)).toEqual([]);
  });

  it('every limiter the app is known to have is built by the factory', () => {
    const built = linesMatching(/\b(?:boundedRateLimit|makeCompanionTurnRateLimit|makeAnonDailyTurnLimit|makeCustomerLlmDailyLimit|makeAuthRateLimit|makeSignupRateLimit|publicFormLimit)\s*\(/);
    const files = new Set(built.map((hit) => hit.split(':')[0]));
    for (const expected of [
      'app.ts',
      path.join('middleware', 'auth-rate-limit.ts'),
      path.join('middleware', 'anon-rate-limit.ts'),
      path.join('middleware', 'companion-rate-limit.ts'),
      path.join('middleware', 'llm-rate-limit.ts'),
      path.join('routes', 'builds.ts'),
      path.join('routes', 'features.ts'),
      path.join('routes', 'feedback.ts'),
      path.join('routes', 'intake.ts'),
      path.join('routes', 'lighthouse.ts'),
      path.join('routes', 'ops-api.ts'),
      path.join('routes', 'support.ts'),
    ]) {
      expect(files, expected).toContain(expected);
    }
  });

  it('no key generator hands the limiter raw request text', () => {
    // The factory cuts whatever it is given, so this is a second line: the
    // shapes that used to put `req.ip`, a header or a body field straight
    // into a key should not come back.
    const raw = linesMatching(/keyGenerator\s*:.*\breq\.(?:ip|headers|body|query|params|originalUrl|url)\b(?!\s*\))/).filter(
      (hit) => !/rateLimitIpKey\(req\.ip\)/.test(hit)
    );
    expect(raw).toEqual([]);
  });
});
