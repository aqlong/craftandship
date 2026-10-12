import { describe, it, expect } from 'vitest';
import { spawnSync } from 'child_process';
import fs from 'fs';
import path from 'path';
import { ON_WORDS, OFF_WORDS, safetySwitch, armingSwitch } from '../../src/utils/env-switch';

// utils/env-switch.ts restates the worker's on/off parser
// (coordinator/envflags.py) so the admin settings page can show each switch
// as the worker will read it. These tests hold the two copies together: the
// word lists are compared as text, and when a python3 is available both
// parsers are run over the same values.

const ENVFLAGS_PY = path.resolve(__dirname, '../../../coordinator/envflags.py');

function pythonWordList(source: string, name: string): string[] {
  const match = new RegExp(`^${name} = frozenset\\(\\{([^}]*)\\}\\)`, 'm').exec(source);
  if (!match) throw new Error(`${name} is not defined as a frozenset literal in coordinator/envflags.py`);
  return [...match[1].matchAll(/"([^"]*)"/g)].map((m) => m[1]).sort();
}

describe('the on and off words are the worker\'s', () => {
  const source = fs.readFileSync(ENVFLAGS_PY, 'utf8');

  it('ON_WORDS equals ON_WORDS in coordinator/envflags.py', () => {
    expect([...ON_WORDS].sort()).toEqual(pythonWordList(source, 'ON_WORDS'));
  });

  it('OFF_WORDS equals OFF_WORDS in coordinator/envflags.py', () => {
    expect([...OFF_WORDS].sort()).toEqual(pythonWordList(source, 'OFF_WORDS'));
  });

  it('no word is on both lists', () => {
    expect(ON_WORDS.filter((w) => OFF_WORDS.includes(w))).toEqual([]);
  });
});

describe('safetySwitch: on is the safe state, only an off word turns it off', () => {
  it.each(['false', '0', 'no', 'off', 'disabled', 'FALSE', ' Off ', 'No\n'])('%j is off', (raw) => {
    expect(safetySwitch(raw)).toBe(false);
    expect(safetySwitch(raw, false)).toBe(false);
  });

  it.each(['true', '1', 'yes', 'on', 'enabled', 'TRUE ', 'garbage', 'ture', 'flase', 'o f f'])('%j is on', (raw) => {
    expect(safetySwitch(raw)).toBe(true);
    expect(safetySwitch(raw, false)).toBe(true);
  });

  it.each([undefined, '', '   ', '\t\n'])('%j is the default', (raw) => {
    expect(safetySwitch(raw)).toBe(true);
    expect(safetySwitch(raw, false)).toBe(false);
  });
});

describe('armingSwitch: on arms something, only an on word turns it on', () => {
  it.each(['true', '1', 'yes', 'on', 'enabled', 'TRUE ', ' On', 'Yes\n'])('%j is on', (raw) => {
    expect(armingSwitch(raw)).toBe(true);
    expect(armingSwitch(raw, true)).toBe(true);
  });

  it.each(['false', '0', 'no', 'off', 'disabled', 'garbage', 'ture', '', '   ', 't r u e'])('%j is off', (raw) => {
    expect(armingSwitch(raw)).toBe(false);
    expect(armingSwitch(raw, true)).toBe(false);
  });

  it('only a variable that is not set at all is the default', () => {
    expect(armingSwitch(undefined)).toBe(false);
    expect(armingSwitch(undefined, true)).toBe(true);
  });
});

// Both parsers over one table. The Python side loads coordinator/envflags.py
// by path (standard library only) and sets each value in its own environment.
const PYTHON_PROBE = `
import importlib.util, json, logging, os, sys
spec = importlib.util.spec_from_file_location("envflags_under_test", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
logging.disable(logging.CRITICAL)
NAME = "CAS_SWITCH_UNDER_TEST"
rows = []
for raw in json.load(sys.stdin):
    os.environ.pop(NAME, None)
    if raw is not None:
        os.environ[NAME] = raw
    rows.append([
        module.safety_switch(NAME, True), module.safety_switch(NAME, False),
        module.arming_switch(NAME, False), module.arming_switch(NAME, True),
    ])
json.dump(rows, sys.stdout)
`;

const u = (code: number): string => String.fromCharCode(code);

// Includes the places where JavaScript's trim() and Python's strip() differ:
// U+FEFF (trim removes it, strip does not) and U+001C to U+001F and U+0085
// (strip removes them, trim does not).
const SHARED_VALUES: Array<string | null> = [
  null, '', ' ', '\t', 'true', 'TRUE ', ' True\n', '1', 'yes', 'on', 'enabled', 'Enabled',
  'false', 'FALSE', '0', 'no', 'off', ' Off', 'disabled', 'garbage', 'ture', 'tru e', '00', '01',
  u(0xfeff) + 'true', u(0xfeff) + 'false', 'false' + u(0x1c), u(0x1f) + 'true', 'true' + u(0x85), 'off' + u(0x85),
  u(0xa0) + 'on' + u(0x3000), u(0x2003) + 'no' + u(0x2028), 'true' + u(0x200b), u(0xff2f) + u(0xff2e), 'ON', 'oN',
];

const python = spawnSync('python3', ['--version'], { encoding: 'utf8' });
const havePython = python.status === 0;

describe('both parsers, run over the same values', () => {
  it.skipIf(!havePython)('utils/env-switch.ts reads every value the way coordinator/envflags.py does', () => {
    const run = spawnSync('python3', ['-I', '-B', '-c', PYTHON_PROBE, ENVFLAGS_PY], {
      input: JSON.stringify(SHARED_VALUES),
      encoding: 'utf8',
    });
    expect(run.status, run.stderr).toBe(0);
    const fromPython = JSON.parse(run.stdout) as boolean[][];
    expect(fromPython).toHaveLength(SHARED_VALUES.length);

    const fromTypeScript = SHARED_VALUES.map((value) => {
      const raw = value === null ? undefined : value;
      return [safetySwitch(raw, true), safetySwitch(raw, false), armingSwitch(raw, false), armingSwitch(raw, true)];
    });
    const disagreements = SHARED_VALUES
      .map((value, i) => ({ value: JSON.stringify(value), python: fromPython[i], typescript: fromTypeScript[i] }))
      .filter((row) => JSON.stringify(row.python) !== JSON.stringify(row.typescript));
    expect(disagreements).toEqual([]);
  });
});
