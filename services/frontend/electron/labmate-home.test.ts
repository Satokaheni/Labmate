import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { labmateHome, userSkillsDir, ensureUserSkillsDir } from './labmate-home.js';

describe('labmate-home', () => {
  describe('labmateHome', () => {
    let originalEnv: string | undefined;

    beforeEach(() => {
      originalEnv = process.env.LABMATE_HOME;
    });

    afterEach(() => {
      if (originalEnv === undefined) {
        delete process.env.LABMATE_HOME;
      } else {
        process.env.LABMATE_HOME = originalEnv;
      }
    });

    it('should return LABMATE_HOME when set', () => {
      const testPath = '/test/path';
      process.env.LABMATE_HOME = testPath;
      expect(labmateHome()).toBe(testPath);
    });

    it('should return ~/.labmate when LABMATE_HOME is not set', () => {
      delete process.env.LABMATE_HOME;
      expect(labmateHome()).toBe(path.join(os.homedir(), '.labmate'));
    });

    it('should return ~/.labmate when LABMATE_HOME is empty string', () => {
      process.env.LABMATE_HOME = '';
      expect(labmateHome()).toBe(path.join(os.homedir(), '.labmate'));
    });
  });

  describe('userSkillsDir', () => {
    let originalEnv: string | undefined;

    beforeEach(() => {
      originalEnv = process.env.LABMATE_HOME;
    });

    afterEach(() => {
      if (originalEnv === undefined) {
        delete process.env.LABMATE_HOME;
      } else {
        process.env.LABMATE_HOME = originalEnv;
      }
    });

    it('should return path.join(labmateHome(), "skills")', () => {
      const testPath = '/test/labmate';
      process.env.LABMATE_HOME = testPath;
      expect(userSkillsDir()).toBe(path.join(testPath, 'skills'));
    });

    it('should return ~/.labmate/skills when LABMATE_HOME is not set', () => {
      delete process.env.LABMATE_HOME;
      expect(userSkillsDir()).toBe(path.join(os.homedir(), '.labmate', 'skills'));
    });
  });

  describe('ensureUserSkillsDir', () => {
    let originalEnv: string | undefined;
    let tmpDir: string;

    beforeEach(() => {
      originalEnv = process.env.LABMATE_HOME;
      // Create a fresh temp directory for testing
      tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'labmate-home-test-'));
      process.env.LABMATE_HOME = tmpDir;
    });

    afterEach(() => {
      if (originalEnv === undefined) {
        delete process.env.LABMATE_HOME;
      } else {
        process.env.LABMATE_HOME = originalEnv;
      }
      // Clean up test directory
      if (fs.existsSync(tmpDir)) {
        fs.rmSync(tmpDir, { recursive: true, force: true });
      }
    });

    it('should create the user skills directory if it does not exist', () => {
      const skillsDir = userSkillsDir();
      expect(fs.existsSync(skillsDir)).toBe(false);

      ensureUserSkillsDir();

      expect(fs.existsSync(skillsDir)).toBe(true);
    });

    it('should return the skills directory path', () => {
      const result = ensureUserSkillsDir();
      expect(result).toBe(userSkillsDir());
    });

    it('should be idempotent (calling twice does not error)', () => {
      const skillsDir = userSkillsDir();

      ensureUserSkillsDir();
      expect(fs.existsSync(skillsDir)).toBe(true);

      // Calling again should not throw
      expect(() => {
        ensureUserSkillsDir();
      }).not.toThrow();
      expect(fs.existsSync(skillsDir)).toBe(true);
    });

    it('should not throw on failure (best-effort)', () => {
      // Mock fs.mkdirSync to simulate failure
      const originalMkdir = fs.mkdirSync;
      let callCount = 0;
      fs.mkdirSync = (() => {
        callCount++;
        throw new Error('Simulated mkdir failure');
      }) as any;

      // ensureUserSkillsDir should not throw even if mkdir fails
      expect(() => {
        ensureUserSkillsDir();
      }).not.toThrow();

      expect(callCount).toBe(1);

      // Restore original function
      fs.mkdirSync = originalMkdir;
    });
  });
});
