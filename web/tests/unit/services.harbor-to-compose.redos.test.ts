import { describe, it, expect } from 'vitest';
import { translateExtractedSpecToV1 } from '../../src/services/harbor_to_compose';

/**
 * Three trailing-slash / query regexes in the translator are quadratic on a
 * long run of slashes that does not end the string: `/\/+$/` (normalizeCopyPath
 * and publicListingPathProblem) and `/^\/+|\/+$/g` (the nav slug and the
 * custom-page slug) rescan the slash run from each of its positions when the
 * anchor at the end fails. The input reaches them from the model's spec output
 * (page slugs) and from an admin's dispatch overrides (copied-page paths),
 * neither bounded in length before this point.
 *
 * Each case fails on the version of `web/src/services/harbor_to_compose.ts` this replaced
 * (proven by running this file against a scratch copy of that version, where a
 * 100k-slash value runs for seconds); on the fixed code the loops are linear
 * and each returns in a few ms.
 *
 * publicListingPathProblem is quadratic in the same way but is reached only
 * after a 120-character length check, so no long value gets to it; it is
 * covered by the existing translator tests, not here.
 */

// A long slash run with a non-slash at each end: the trailing-slash anchor
// then fails from every position inside the run.
function slashBomb(slashes: number): string {
  return 'x' + '/'.repeat(slashes) + 'x';
}

const BOUND_MS = 500;

function baseInput(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    spec_version: '1.0',
    app_name: 'sample-keeper',
    business_type: 'practitioner',
    archetype: 'healer-wellness',
    tier: 'build',
    admin_email: 'sample@example.com',
    pages: [],
    data_models: [],
    copy: {},
    design: {},
    _extraction_warnings: [],
    ...overrides,
  };
}

function timed(work: () => void): number {
  const started = performance.now();
  work();
  return performance.now() - started;
}

describe('the translator is linear on a long run of slashes', () => {
  it('nav-slug derivation (pages[].slug): a 100k-slash slug is handled in well under half a second', () => {
    const elapsed = timed(() => {
      translateExtractedSpecToV1(baseInput({ pages: [{ slug: slashBomb(100_000) }] }));
    });
    expect(elapsed).toBeLessThan(BOUND_MS);
  });

  it('custom-page slug (pages[].slug, non-archetype): a 100k-slash slug is handled quickly', () => {
    // A slug the archetype does not already own, so it reaches the custom-page
    // path that trims slashes.
    const elapsed = timed(() => {
      translateExtractedSpecToV1(baseInput({ pages: [{ slug: 'studio' + slashBomb(100_000) }] }));
    });
    expect(elapsed).toBeLessThan(BOUND_MS);
  });

  it('copied-page path (copy_pages[].path): a 100k-slash path is handled quickly', () => {
    const elapsed = timed(() => {
      translateExtractedSpecToV1(
        baseInput({ copy_pages: [{ path: slashBomb(100_000), markdown_blocks: ['hello'] }] })
      );
    });
    expect(elapsed).toBeLessThan(BOUND_MS);
  });

  it('a query string on a long path is cut without rescanning (copy_pages[].path)', () => {
    const elapsed = timed(() => {
      translateExtractedSpecToV1(
        baseInput({ copy_pages: [{ path: '/about?' + 'q'.repeat(200_000), markdown_blocks: ['hi'] }] })
      );
    });
    expect(elapsed).toBeLessThan(BOUND_MS);
  });
});

describe('the linear trimming gives the same result as before on ordinary input', () => {
  it('trims surrounding slashes on a page slug the way the archetype expects', () => {
    const { spec } = translateExtractedSpecToV1(baseInput({ pages: [{ slug: '//journal//' }] }));
    const navPaths = (spec._harbor_meta as Record<string, unknown>)?.nav_paths as string[] | undefined;
    expect(navPaths).toContain('/journal');
  });

  it('normalizes a copied path (origin, query, trailing slash) the way it did before', () => {
    const { spec } = translateExtractedSpecToV1(
      baseInput({
        copy_pages: [
          { path: 'https://studio.example/services/?ref=nav', markdown_blocks: ['What we do'] },
          { path: '/journal///', markdown_blocks: ['Notes'] },
        ],
      })
    );
    const paths = ((spec._harbor_meta as Record<string, unknown>)?.copy_pages as Array<{ path: string }>).map(
      (p) => p.path
    );
    expect(paths).toEqual(['/services', '/journal']);
  });
});
