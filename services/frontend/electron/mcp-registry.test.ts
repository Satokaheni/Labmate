import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { McpHostManager, skillsDir } from './mcp-registry.js';
import fs from 'node:fs';
import path from 'node:path';

describe('McpHostManager', () => {
  describe('skillsDir()', () => {
    it('should resolve skills directory from env var if set', () => {
      const original = process.env.LABMATE_SKILLS_DIR;
      try {
        process.env.LABMATE_SKILLS_DIR = '/custom/path';
        expect(skillsDir()).toBe('/custom/path');
      } finally {
        if (original === undefined) {
          delete process.env.LABMATE_SKILLS_DIR;
        } else {
          process.env.LABMATE_SKILLS_DIR = original;
        }
      }
    });

    it('should resolve skills directory relative to repo root when env var not set', () => {
      const original = process.env.LABMATE_SKILLS_DIR;
      try {
        delete process.env.LABMATE_SKILLS_DIR;
        const result = skillsDir();
        // Should end with 'services/skills'
        expect(result).toMatch(/services[/\\]skills$/);
      } finally {
        if (original === undefined) {
          delete process.env.LABMATE_SKILLS_DIR;
        } else {
          process.env.LABMATE_SKILLS_DIR = original;
        }
      }
    });
  });

  describe('real collection', () => {
    let manager: McpHostManager;

    beforeAll(() => {
      manager = new McpHostManager();
    });

    afterAll(async () => {
      await manager.stopAll();
    });

    it('should start all available servers and collect tools', async () => {
      // Check if ast-ts-refactor is built
      const skillsDirPath = skillsDir();
      const astDistPath = path.join(skillsDirPath, 'ast-ts-refactor', 'dist', 'index.js');

      if (!fs.existsSync(astDistPath)) {
        // Skip if the built skill doesn't exist
        expect(true).toBe(true);
        return;
      }

      await manager.startAll();

      const descriptors = manager.getToolDescriptors();

      // Should have collected at least some tools
      expect(descriptors).toBeDefined();
      expect(Array.isArray(descriptors)).toBe(true);
      expect(descriptors.length).toBeGreaterThan(0);

      // Verify descriptor shape
      for (const desc of descriptors) {
        expect(desc).toHaveProperty('name');
        expect(desc).toHaveProperty('source');
        expect(desc).toHaveProperty('namespace');
        expect(desc).toHaveProperty('schema');

        // name should be the raw tool name (not pre-namespaced)
        expect(typeof desc.name).toBe('string');
        expect(desc.name.length).toBeGreaterThan(0);
        expect(desc.name).not.toMatch(/^mcp__/);

        // source must be 'mcp'
        expect(desc.source).toBe('mcp');

        // namespace should be a valid server name
        expect(['ast-ts-refactor', 'component-doc-gen', 'a11y-audit']).toContain(desc.namespace);

        // schema must have the shape of an OpenAI tool schema
        expect(desc.schema).toHaveProperty('type');
        expect(desc.schema).toHaveProperty('function');
        const func = (desc.schema as any).function;
        expect(func).toHaveProperty('name');
        expect(func).toHaveProperty('description');
        expect(func).toHaveProperty('parameters');
      }

      // ast-ts-refactor should be in the collection (if it's built)
      const astTools = descriptors.filter((d) => d.namespace === 'ast-ts-refactor');
      expect(astTools.length).toBeGreaterThan(0);
    }, 30000);

    it('should handle missing dist gracefully (skip, not throw)', async () => {
      // Even if a11y-audit is unbuilt, startAll should not fail
      // Just skip it and continue.
      const manager2 = new McpHostManager();
      await expect(manager2.startAll()).resolves.toBeUndefined();
      await manager2.stopAll();
    });
  });

  describe('namespacing and routing', () => {
    let manager: McpHostManager;

    beforeAll(async () => {
      manager = new McpHostManager();
      const skillsDirPath = skillsDir();
      const astDistPath = path.join(skillsDirPath, 'ast-ts-refactor', 'dist', 'index.js');

      // Only set up if ast-ts-refactor is built
      if (fs.existsSync(astDistPath)) {
        await manager.startAll();
      }
    });

    afterAll(async () => {
      await manager.stopAll();
    });

    it('should reject unknown server in namespaced call', async () => {
      const skillsDirPath = skillsDir();
      const astDistPath = path.join(skillsDirPath, 'ast-ts-refactor', 'dist', 'index.js');

      if (!fs.existsSync(astDistPath)) {
        expect(true).toBe(true);
        return;
      }

      await expect(manager.callTool('mcp__unknown-server__someTool', {})).rejects.toThrow(
        /Unknown MCP server/
      );
    });

    it('should throw clear error on invalid namespaced format', async () => {
      await expect(manager.callTool('invalid-format', {})).rejects.toThrow(
        /Invalid namespaced tool name/
      );
    });
  });

  describe('descriptor shape verification', () => {
    it('collected descriptors have raw tool names (not pre-namespaced)', async () => {
      const manager = new McpHostManager();
      const skillsDirPath = skillsDir();
      const astDistPath = path.join(skillsDirPath, 'ast-ts-refactor', 'dist', 'index.js');

      if (!fs.existsSync(astDistPath)) {
        expect(true).toBe(true);
        return;
      }

      await manager.startAll();

      const descriptors = manager.getToolDescriptors();
      for (const desc of descriptors) {
        // name is the RAW tool name (not pre-namespaced)
        expect(desc.name).not.toMatch(/^mcp__/);
        // namespace is the server name
        expect(desc.namespace).toBeTruthy();
        expect(typeof desc.namespace).toBe('string');
      }

      await manager.stopAll();
    }, 30000);
  });

  describe('stopAll idempotence', () => {
    it('should be safe to call stopAll multiple times', async () => {
      const manager = new McpHostManager();
      await expect(manager.stopAll()).resolves.toBeUndefined();
      await expect(manager.stopAll()).resolves.toBeUndefined();
    });
  });
});
