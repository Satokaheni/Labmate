import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import {
  labmateHome,
  userSkillsDir,
  ensureUserSkillsDir,
  labmateMcpConfigPath,
  readUserMcpServers,
} from './labmate-home.js';

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

  describe('labmateMcpConfigPath', () => {
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

    it('should return path.join(labmateHome(), "mcp.json")', () => {
      const testPath = '/test/labmate';
      process.env.LABMATE_HOME = testPath;
      expect(labmateMcpConfigPath()).toBe(path.join(testPath, 'mcp.json'));
    });

    it('should return ~/.labmate/mcp.json when LABMATE_HOME is not set', () => {
      delete process.env.LABMATE_HOME;
      expect(labmateMcpConfigPath()).toBe(path.join(os.homedir(), '.labmate', 'mcp.json'));
    });
  });

  describe('readUserMcpServers', () => {
    let originalEnv: string | undefined;
    let tmpDir: string;

    beforeEach(() => {
      originalEnv = process.env.LABMATE_HOME;
      tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'labmate-mcp-test-'));
      process.env.LABMATE_HOME = tmpDir;
    });

    afterEach(() => {
      if (originalEnv === undefined) {
        delete process.env.LABMATE_HOME;
      } else {
        process.env.LABMATE_HOME = originalEnv;
      }
      if (fs.existsSync(tmpDir)) {
        fs.rmSync(tmpDir, { recursive: true, force: true });
      }
    });

    it('should return empty array when mcp.json does not exist', () => {
      const result = readUserMcpServers();
      expect(result).toEqual([]);
    });

    it('should parse a valid mcp.json with multiple servers', () => {
      const config = {
        mcpServers: {
          'test-server-1': {
            command: 'node',
            args: ['/path/to/server1.js'],
          },
          'test-server-2': {
            command: 'python',
            args: ['/path/to/server2.py', '--arg'],
            cwd: '/some/cwd',
            env: { MY_VAR: 'value' },
          },
        },
      };

      const configPath = labmateMcpConfigPath();
      fs.writeFileSync(configPath, JSON.stringify(config));

      const result = readUserMcpServers();

      expect(result).toHaveLength(2);
      expect(result[0]).toEqual({
        name: 'test-server-1',
        command: 'node',
        args: ['/path/to/server1.js'],
      });
      expect(result[1]).toEqual({
        name: 'test-server-2',
        command: 'python',
        args: ['/path/to/server2.py', '--arg'],
        cwd: '/some/cwd',
        env: { MY_VAR: 'value' },
      });
    });

    it('should return empty array when JSON is corrupt', () => {
      const configPath = labmateMcpConfigPath();
      fs.writeFileSync(configPath, 'not valid json {');

      const result = readUserMcpServers();
      expect(result).toEqual([]);
    });

    it('should return empty array when mcpServers is missing', () => {
      const config = { other: 'data' };
      const configPath = labmateMcpConfigPath();
      fs.writeFileSync(configPath, JSON.stringify(config));

      const result = readUserMcpServers();
      expect(result).toEqual([]);
    });

    it('should return empty array when mcpServers is not an object', () => {
      const config = { mcpServers: 'not-an-object' };
      const configPath = labmateMcpConfigPath();
      fs.writeFileSync(configPath, JSON.stringify(config));

      const result = readUserMcpServers();
      expect(result).toEqual([]);
    });

    it('should skip entries with missing command', () => {
      const config = {
        mcpServers: {
          'missing-command': {
            args: ['/path/to/server.js'],
          },
          'valid-server': {
            command: 'node',
            args: ['/path/to/server.js'],
          },
        },
      };

      const configPath = labmateMcpConfigPath();
      fs.writeFileSync(configPath, JSON.stringify(config));

      const result = readUserMcpServers();
      expect(result).toHaveLength(1);
      expect(result[0].name).toBe('valid-server');
    });

    it('should skip entries with empty command string', () => {
      const config = {
        mcpServers: {
          'empty-command': {
            command: '   ',
            args: [],
          },
          'valid-server': {
            command: 'node',
            args: [],
          },
        },
      };

      const configPath = labmateMcpConfigPath();
      fs.writeFileSync(configPath, JSON.stringify(config));

      const result = readUserMcpServers();
      expect(result).toHaveLength(1);
      expect(result[0].name).toBe('valid-server');
    });

    it('should skip entries where name contains __', () => {
      const config = {
        mcpServers: {
          'bad__name': {
            command: 'node',
            args: [],
          },
          'good-name': {
            command: 'node',
            args: [],
          },
        },
      };

      const configPath = labmateMcpConfigPath();
      fs.writeFileSync(configPath, JSON.stringify(config));

      const result = readUserMcpServers();
      expect(result).toHaveLength(1);
      expect(result[0].name).toBe('good-name');
    });

    it('should default args to empty array when not provided', () => {
      const config = {
        mcpServers: {
          'no-args': {
            command: 'node',
          },
        },
      };

      const configPath = labmateMcpConfigPath();
      fs.writeFileSync(configPath, JSON.stringify(config));

      const result = readUserMcpServers();
      expect(result).toHaveLength(1);
      expect(result[0].args).toEqual([]);
    });

    it('should handle servers with cwd but no env', () => {
      const config = {
        mcpServers: {
          'with-cwd': {
            command: 'node',
            args: ['/path/to/server.js'],
            cwd: '/home/user/servers',
          },
        },
      };

      const configPath = labmateMcpConfigPath();
      fs.writeFileSync(configPath, JSON.stringify(config));

      const result = readUserMcpServers();
      expect(result).toHaveLength(1);
      expect(result[0]).toEqual({
        name: 'with-cwd',
        command: 'node',
        args: ['/path/to/server.js'],
        cwd: '/home/user/servers',
      });
      expect(result[0].env).toBeUndefined();
    });

    it('should handle servers with env but no cwd', () => {
      const config = {
        mcpServers: {
          'with-env': {
            command: 'node',
            args: ['/path/to/server.js'],
            env: { DEBUG: 'true', API_KEY: 'secret' },
          },
        },
      };

      const configPath = labmateMcpConfigPath();
      fs.writeFileSync(configPath, JSON.stringify(config));

      const result = readUserMcpServers();
      expect(result).toHaveLength(1);
      expect(result[0]).toEqual({
        name: 'with-env',
        command: 'node',
        args: ['/path/to/server.js'],
        env: { DEBUG: 'true', API_KEY: 'secret' },
      });
      expect(result[0].cwd).toBeUndefined();
    });

    it('should skip non-object entries', () => {
      const config = {
        mcpServers: {
          'null-entry': null,
          'string-entry': 'not-an-object',
          'valid-server': {
            command: 'node',
            args: [],
          },
        },
      };

      const configPath = labmateMcpConfigPath();
      fs.writeFileSync(configPath, JSON.stringify(config));

      const result = readUserMcpServers();
      expect(result).toHaveLength(1);
      expect(result[0].name).toBe('valid-server');
    });
  });
});
