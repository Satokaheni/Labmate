import os from 'node:os';
import fs from 'node:fs';
import path from 'node:path';

/** Global Labmate home, like ~/.claude. Override with LABMATE_HOME (tests/dev). */
export function labmateHome(): string {
  return process.env.LABMATE_HOME && process.env.LABMATE_HOME.length > 0
    ? process.env.LABMATE_HOME
    : path.join(os.homedir(), '.labmate');
}

/** Global user skills dir: <labmateHome>/skills. Each skill is a subfolder with a SKILL.md. */
export function userSkillsDir(): string {
  return path.join(labmateHome(), 'skills');
}

/** Best-effort: ensure the user skills dir exists so the user can drop skill folders in.
 *  Never throws (a failure must not break app startup). Returns the path. */
export function ensureUserSkillsDir(): string {
  const dir = userSkillsDir();
  try {
    fs.mkdirSync(dir, { recursive: true });
  } catch (err) {
    console.error('failed to ensure user skills dir:', err);
  }
  return dir;
}
