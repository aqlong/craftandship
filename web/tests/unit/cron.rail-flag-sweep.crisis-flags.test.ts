import { describe, it, expect, vi } from 'vitest';
import type { Pool } from 'pg';
import { runRailFlagSweep } from '../../src/cron/rail_flag_sweep';
import { scanCrisis } from '../../src/companion/crisis_detection';
import { crisisReply } from '../../src/companion/runtime';

// The weekly sweep reads companion_turns.rail_flags and gives each flag a
// line of guidance; a flag it has no line for gets a generic "unrecognized"
// one. The crisis handler writes one flag per category the scan fired, so
// the two lists have to stay the same list. This test takes the flags from
// the scanner itself, not from a copy of its category names.

const ONE_MESSAGE_PER_CATEGORY = [
  'I want to kill myself',
  'I have been cutting myself',
  'my husband hits me',
  "I think I'm having a heart attack",
  'I keep having thoughts of hurting my children',
];

function sweepPool(population: Array<{ flag: string; turns_with_flag: string; total_turns: string }>): Pool {
  const query = vi.fn();
  query.mockResolvedValueOnce({ rows: [] });
  query.mockResolvedValueOnce({ rows: population });
  query.mockResolvedValue({ rows: [], rowCount: 1 });
  return { query } as unknown as Pool;
}

describe('rail flag sweep: every flag a crisis turn can carry has its own guidance', () => {
  const flags = new Set<string>();
  for (const message of ONE_MESSAGE_PER_CATEGORY) {
    for (const flag of crisisReply(scanCrisis(message).categories).railFlags) flags.add(flag);
  }

  it('the sample messages cover every category the scanner has', () => {
    expect([...flags].sort()).toEqual(['abuse', 'acute-medical', 'crisis', 'harm-to-others', 'self-harm', 'suicide']);
  });

  it('none of them falls back to the generic "unrecognized flag" text', async () => {
    const result = await runRailFlagSweep({
      pool: sweepPool([...flags].map((flag) => ({ flag, turns_with_flag: '5', total_turns: '10' }))),
    });
    const byFlag = new Map(result.findings.map((finding) => [finding.flag, finding.guardrail]));
    for (const flag of flags) {
      expect(byFlag.get(flag), flag).toBeTruthy();
      expect(byFlag.get(flag), flag).not.toMatch(/Unrecognized rail flag/);
    }
    expect(byFlag.get('harm-to-others')).toMatch(/crisis_detection/);
  });
});
