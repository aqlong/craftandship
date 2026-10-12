// DOM-construction safety for the browser scripts and the templates.
//
// The scripts in public/ are plain IIFEs with no module system, so (as in
// public.status-filter.test.ts) the functions under test are sliced out of the
// source and evaluated in a bare vm context. A slice that no longer matches
// fails loudly instead of passing silently.

import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync } from 'fs';
import { join, resolve } from 'path';
import vm from 'vm';

const PUBLIC_DIR = resolve(__dirname, '../../public');
const VIEWS_DIR = resolve(__dirname, '../../views');

function read(file: string): string {
  return readFileSync(file, 'utf8');
}

/** Slice `function <name>(...) { ... }` (closing brace at the function's own indent) out of a script. */
function extractFunction(source: string, name: string, where: string): string {
  const match = source.match(new RegExp(`(^|\\n)([ \\t]*)function\\s+${name}\\s*\\([^)]*\\)\\s*\\{[\\s\\S]*?\\n\\2\\}`));
  if (!match) throw new Error(`Could not extract ${name} from ${where}`);
  return match[0];
}

function loadFunction<T>(file: string, name: string): T {
  const sandbox: Record<string, unknown> = {};
  vm.createContext(sandbox);
  vm.runInContext(extractFunction(read(join(PUBLIC_DIR, file)), name, file), sandbox);
  return sandbox[name] as T;
}

function allViewFiles(dir: string = VIEWS_DIR): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...allViewFiles(full));
    else if (entry.name.endsWith('.ejs')) out.push(full);
  }
  return out;
}

const SCRIPTS_WITH_ESCAPER = ['status.js', 'intake.js', 'intake-anon.js', 'feature-chat.js', 'revision.js'];

describe('public scripts: escapeHtml is safe in text and in quoted attributes', () => {
  it.each(SCRIPTS_WITH_ESCAPER)('%s escapes & < > " and \'', (file) => {
    const escapeHtml = loadFunction<(value: unknown) => string>(file, 'escapeHtml');
    expect(escapeHtml(`<img src=x onerror="alert(1)"> & 'q'`)).toBe(
      '&lt;img src=x onerror=&quot;alert(1)&quot;&gt; &amp; &#39;q&#39;'
    );
    // The value that breaks out of href="..." when quotes are left alone.
    const breakout = escapeHtml('https://x.example/" onmouseover="alert(1)');
    expect(breakout).not.toContain('"');
  });

  it('no script or inline view script builds its escaper on a text node (which leaves quotes alone)', () => {
    const offenders: string[] = [];
    const sources = [
      ...readdirSync(PUBLIC_DIR).filter((f) => f.endsWith('.js')).map((f) => join(PUBLIC_DIR, f)),
      ...allViewFiles(),
    ];
    for (const file of sources) {
      const source = read(file);
      // function escapeHtml / esc / escapeAttr whose body appends a text node
      // and returns innerHTML.
      const weak = /function\s+esc\w*\s*\([^)]*\)\s*\{[^}]*createTextNode[^}]*\.innerHTML/.test(source);
      if (weak) offenders.push(file.replace(resolve(__dirname, '../..') + '/', ''));
    }
    expect(offenders).toEqual([]);
  });
});

describe('status.js: no URL is written into markup by script', () => {
  const source = read(join(PUBLIC_DIR, 'status.js'));

  it('the banner builders that concatenated preview and live URLs into href are gone', () => {
    // They were never called (a build reaching preview, live or failed makes
    // the page reload and the server renders the result), so the unsafe sink
    // was removed rather than hardened.
    for (const name of ['handlePreview', 'handleLive', 'handleFailed']) {
      expect(source).not.toContain(name);
    }
    expect(source).not.toMatch(/href="'\s*\+/);
    expect(source).not.toMatch(/action="/);
  });

  it('every remaining innerHTML assignment is a constant or goes through escapeHtml', () => {
    const assignments = source.split('\n').filter((line) => /\.innerHTML\s*=/.test(line));
    expect(assignments.length).toBeGreaterThan(0);
    for (const line of assignments) {
      const rhs = line.slice(line.indexOf('=') + 1);
      // Remove string literals and escapeHtml(...) calls; nothing that reads a
      // variable may be left over.
      const residue = rhs
        .replace(/'(?:[^'\\]|\\.)*'/g, '')
        .replace(/escapeHtml\((?:[^()]|\([^()]*\))*\)/g, '')
        .replace(/[\s+;]/g, '');
      expect(residue, line.trim()).toBe('');
    }
  });

  it('the page no longer hands the script values it does not use', () => {
    const view = read(join(VIEWS_DIR, 'status.ejs'));
    for (const unused of ['STATUS_PREVIEW_URL', 'STATUS_ADMIN_EMAIL', 'STATUS_ESTIMATE_MINUTES']) {
      expect(view).not.toContain(unused);
      expect(source).not.toContain(unused);
    }
    // Still needed: the inline feedback form on the status page reads it.
    expect(view).toContain('window.STATUS_CSRF_TOKEN = ');
    expect(view).toContain('var csrf = window.STATUS_CSRF_TOKEN;');
  });
});

describe('intake.js: the logo preview only accepts an image data URL', () => {
  it('guards the assignment to img.src with a data:image check', () => {
    const source = read(join(PUBLIC_DIR, 'intake.js'));
    const assignment = source.indexOf('previewImg.src = data.logo_preview');
    expect(assignment).toBeGreaterThan(-1);
    const guard = source.lastIndexOf('/^data:image\\/(png|jpeg|webp|svg\\+xml);base64,', assignment);
    expect(guard).toBeGreaterThan(-1);
    expect(assignment - guard).toBeLessThan(200);
  });
});

describe('views: database URLs in href go through httpUrl()', () => {
  it('no template writes a live or preview URL straight into an href', () => {
    const offenders: string[] = [];
    let guarded = 0;
    const hrefTag = /href="<%[=-]([^%]*)%>"/g;
    const urlName = /\b(live_url|preview_url|liveUrl|previewUrl|publicUrl|_url)\b/;
    for (const file of allViewFiles()) {
      read(file)
        .split('\n')
        .forEach((line, index) => {
          for (const tag of line.matchAll(hrefTag)) {
            const expression = tag[1].trim();
            if (!urlName.test(expression)) continue;
            if (expression.startsWith('httpUrl(')) guarded += 1;
            else offenders.push(`${file.replace(VIEWS_DIR + '/', '')}:${index + 1}`);
          }
        });
    }
    expect(offenders).toEqual([]);
    // The scan is looking at something: these hrefs exist and are guarded.
    expect(guarded).toBeGreaterThan(15);
  });

  it('no template emits an attribute through an escaping tag (it would render &quot; instead of quotes)', () => {
    // <%= cond ? ' attr="value"' : '' %> HTML-escapes the quotes, so the browser
    // sees attr=&quot;value&quot;: the attribute value then includes literal
    // quote characters (rel="noopener" stops being noopener).
    const offenders: string[] = [];
    const escapedAttribute = /<%=[^%]*['"`]\s*[a-z-]+="[^%]*%>/i;
    for (const file of allViewFiles()) {
      read(file)
        .split('\n')
        .forEach((line, index) => {
          if (escapedAttribute.test(line)) offenders.push(`${file.replace(VIEWS_DIR + '/', '')}:${index + 1}`);
        });
    }
    expect(offenders).toEqual([]);
  });
});
