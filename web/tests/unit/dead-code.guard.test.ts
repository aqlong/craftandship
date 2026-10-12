import { describe, it, expect } from 'vitest';
import fs from 'fs';
import path from 'path';

const srcRoot = path.join(__dirname, '../../src');

function listTs(dir: string): string[] {
  const out: string[] = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...listTs(full));
    else if (entry.name.endsWith('.ts')) out.push(full);
  }
  return out;
}

const sourceFiles = listTs(srcRoot);
const read = (file: string): string => fs.readFileSync(file, 'utf-8');

describe('background jobs that look alive are registered', () => {
  // A module under src/cron/ is a job. One that nothing schedules is a feature
  // that appears to exist and sends nothing (the unscheduled day-7 letter was
  // exactly that). Every job must be imported by a module outside src/cron/.
  const jobs = fs.readdirSync(path.join(srcRoot, 'cron')).filter((name) => name.endsWith('.ts'));

  it('finds the jobs', () => {
    expect(jobs.length).toBeGreaterThanOrEqual(4);
  });

  for (const job of jobs) {
    it(`${job} is imported by something that runs it`, () => {
      const base = job.replace(/\.ts$/, '');
      const importers = sourceFiles.filter((file) => {
        if (file.includes(`${path.sep}cron${path.sep}`)) return false;
        return new RegExp(`from ['"][./]*(?:cron/)?${base}['"]`).test(read(file));
      });
      expect(importers, `${job} has no importer outside src/cron/`).not.toEqual([]);
    });
  }
});

describe('every email sender has a caller', () => {
  // Senders in services/email.ts are called from routes, jobs or the scheduler's
  // template map. A sender nobody calls is a message the product appears to send
  // and does not. The one exception is documented where it is defined.
  const NO_CALLER_BY_DESIGN = new Map([
    ['sendHarborLiveEmail', 'its caller was an automated review loop that ran outside this repository; kept as the record of what that path sent'],
  ]);
  const emailSource = read(path.join(srcRoot, 'services', 'email.ts'));
  const senders = [...emailSource.matchAll(/^export async function (send[A-Za-z]+)\(/gm)].map((m) => m[1]);
  const others = sourceFiles.filter((file) => !file.endsWith(path.join('services', 'email.ts')));

  it('finds the senders', () => {
    expect(senders.length).toBeGreaterThan(15);
    expect(senders).toContain('sendPasswordResetEmail');
  });

  for (const sender of senders) {
    if (NO_CALLER_BY_DESIGN.has(sender)) {
      it(`${sender} documents why nothing calls it`, () => {
        const callers = others.filter((file) => new RegExp(`\\b${sender}\\b`).test(read(file)));
        expect(callers).toEqual([]);
        const block = emailSource.slice(0, emailSource.indexOf(`export async function ${sender}(`));
        expect(block.slice(-900)).toMatch(/Nothing in this repository calls it/);
      });
      continue;
    }
    it(`${sender} is called from outside email.ts`, () => {
      const callers = others.filter((file) => new RegExp(`\\b${sender}\\b`).test(read(file)));
      expect(callers, `${sender} has no caller in src/`).not.toEqual([]);
    });
  }
});
