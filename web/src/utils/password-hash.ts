// The one place a password hash is made.
//
// A login against an address with no account compares the typed password with
// a throwaway hash, so that "no such account" and "wrong password" cost the
// same bcrypt work and cannot be told apart by response time (routes/auth.ts).
// That only holds if every hash in the accounts table has the cost of that
// throwaway one. A caller that picks its own cost ("10" for the placeholder
// password of an account made at checkout) makes the accounts it creates
// answer a wrong password measurably faster than the rest. So no caller picks
// a cost: signup, reset, the placeholder passwords of accounts that are made
// for a payer or a preview, and the throwaway hash all come from here.

import crypto from 'crypto';
import bcrypt from 'bcrypt';

/** The bcrypt cost factor of every password hash this application makes. */
export const BCRYPT_ROUNDS = 12;

/** A bcrypt hash of `plain` at BCRYPT_ROUNDS. */
export function hashPassword(plain: string): Promise<string> {
  return bcrypt.hash(plain, BCRYPT_ROUNDS);
}

/**
 * A password hash for an account nobody can log in to by password: the hash of
 * a random secret that is generated here and never returned. The account is
 * entered through a link sent to its mailbox (a verification or reset link).
 */
export function hashUnusablePassword(): Promise<string> {
  return hashPassword(crypto.randomBytes(64).toString('hex'));
}

// A bcrypt hash of a random value, computed once on first use. Login compares
// against it when the email is unknown (see the header).
let dummyPasswordHash: Promise<string> | null = null;
export function getDummyPasswordHash(): Promise<string> {
  if (!dummyPasswordHash) {
    dummyPasswordHash = hashPassword(crypto.randomBytes(24).toString('hex'));
  }
  return dummyPasswordHash;
}
