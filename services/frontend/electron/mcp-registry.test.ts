import { describe, it, expect, beforeAll, afterAll, vi } from 'vitest';
import { McpHostManager, skillsDir, parseNamespacedTool } from './mcp-registry.js';
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

describe('parseNamespacedTool', () => {
  describe('basic parsing', () => {
    it('should parse tool name with underscore correctly', () => {
      const result = parseNamespacedTool('mcp__ast-ts-refactor__find_references', [
        'ast-ts-refactor',
      ]);
      expect(result).toEqual({ server: 'ast-ts-refactor', tool: 'find_references' });
    });

    it('should parse server name with underscore correctly', () => {
      const result = parseNamespacedTool('mcp__my_server__find_references', ['my_server']);
      expect(result).toEqual({ server: 'my_server', tool: 'find_references' });
    });

    it('should handle tool name with double underscore', () => {
      const result = parseNamespacedTool('mcp__srv__a__b', ['srv']);
      expect(result).toEqual({ server: 'srv', tool: 'a__b' });
    });

    it('should use longest-prefix match when multiple servers could match', () => {
      const result = parseNamespacedTool('mcp__ast-ts-refactor__rename', [
        'ast',
        'ast-ts-refactor',
      ]);
      expect(result).toEqual({ server: 'ast-ts-refactor', tool: 'rename' });
    });

    it('should prefer exact name over partial prefix', () => {
      const result = parseNamespacedTool('mcp__ast-ts-refactor__foo', [
        'ast',
        'ast-ts',
        'ast-ts-refactor',
      ]);
      expect(result).toEqual({ server: 'ast-ts-refactor', tool: 'foo' });
    });
  });

  describe('error cases (null returns)', () => {
    it('should return null when mcp__ prefix is missing', () => {
      const result = parseNamespacedTool('no_prefix__tool', ['server']);
      expect(result).toBeNull();
    });

    it('should return null when no registered server matches', () => {
      const result = parseNamespacedTool('mcp__unknown__tool', ['other-server']);
      expect(result).toBeNull();
    });

    it('should return null when tool is empty', () => {
      const result = parseNamespacedTool('mcp__srv__', ['srv']);
      expect(result).toBeNull();
    });

    it('should return null when there is no __ separator after mcp__', () => {
      const result = parseNamespacedTool('mcp__noseparator', ['srv']);
      expect(result).toBeNull();
    });
  });

  describe('edge cases', () => {
    it('should handle single-character tool names', () => {
      const result = parseNamespacedTool('mcp__srv__x', ['srv']);
      expect(result).toEqual({ server: 'srv', tool: 'x' });
    });

    it('should handle complex tool names with mixed separators', () => {
      const result = parseNamespacedTool('mcp__ast-ts-refactor__find_all_refs_in_scope', [
        'ast-ts-refactor',
      ]);
      expect(result).toEqual({
        server: 'ast-ts-refactor',
        tool: 'find_all_refs_in_scope',
      });
    });

    it('should work with empty knownServers array (returns null)', () => {
      const result = parseNamespacedTool('mcp__srv__tool', []);
      expect(result).toBeNull();
    });

    it('should handle numeric characters in names', () => {
      const result = parseNamespacedTool('mcp__v2_api__get_data_123', ['v2_api']);
      expect(result).toEqual({ server: 'v2_api', tool: 'get_data_123' });
    });
  });
});

describe('McpHostManager.callTool integration with parseNamespacedTool', () => {
  describe('with stubbed hosts', () => {
    it('should route to correct host and call tool with bare name', async () => {
      const manager = new McpHostManager();

      // Stub a host
      const fakeHost = {
        callTool: vi.fn(async (toolName: string) => ({ result: 'ok' })),
        listTools: vi.fn(),
        start: vi.fn(),
        stop: vi.fn(),
      };
      (manager as any).hosts.set('ast-ts-refactor', fakeHost);

      const result = await manager.callTool('mcp__ast-ts-refactor__find_references', {
        pos: 42,
      });

      expect(fakeHost.callTool).toHaveBeenCalledWith('find_references', { pos: 42 });
      expect(result).toEqual({ result: 'ok' });
    });

    it('should route to correct host when server name has underscores', async () => {
      const manager = new McpHostManager();

      const fakeHost = {
        callTool: vi.fn(async () => ({ ok: true })),
        listTools: vi.fn(),
        start: vi.fn(),
        stop: vi.fn(),
      };
      (manager as any).hosts.set('my_server', fakeHost);

      await manager.callTool('mcp__my_server__some_tool', {});

      expect(fakeHost.callTool).toHaveBeenCalledWith('some_tool', {});
    });

    it('should throw "Invalid namespaced tool name" when mcp__ prefix is missing', async () => {
      const manager = new McpHostManager();
      await expect(manager.callTool('invalid-format', {})).rejects.toThrow(
        /Invalid namespaced tool name/
      );
    });

    it('should throw "Invalid namespaced tool name" when no __ separator exists', async () => {
      const manager = new McpHostManager();
      await expect(manager.callTool('mcp__noseparator', {})).rejects.toThrow(
        /Invalid namespaced tool name/
      );
    });

    it('should throw "Unknown MCP server" when no registered server matches', async () => {
      const manager = new McpHostManager();
      (manager as any).hosts.set('known', {});

      await expect(manager.callTool('mcp__unknown__tool', {})).rejects.toThrow(
        /Unknown MCP server/
      );
    });

    it('should use longest-prefix match to route to the correct host', async () => {
      const manager = new McpHostManager();

      const shortHost = {
        callTool: vi.fn(),
        listTools: vi.fn(),
        start: vi.fn(),
        stop: vi.fn(),
      };
      const longHost = {
        callTool: vi.fn(async () => ({ matched: 'long' })),
        listTools: vi.fn(),
        start: vi.fn(),
        stop: vi.fn(),
      };

      (manager as any).hosts.set('ast', shortHost);
      (manager as any).hosts.set('ast-ts-refactor', longHost);

      await manager.callTool('mcp__ast-ts-refactor__rename', { name: 'foo' });

      expect(longHost.callTool).toHaveBeenCalledWith('rename', { name: 'foo' });
      expect(shortHost.callTool).not.toHaveBeenCalled();
    });

    it('should wrap host.callTool exceptions in a descriptive error', async () => {
      const manager = new McpHostManager();

      const fakeHost = {
        callTool: vi.fn(async () => {
          throw new Error('Tool execution failed');
        }),
        listTools: vi.fn(),
        start: vi.fn(),
        stop: vi.fn(),
      };
      (manager as any).hosts.set('test-server', fakeHost);

      await expect(manager.callTool('mcp__test-server__mytool', {})).rejects.toThrow(
        /Failed to call tool 'mytool' on server 'test-server'/
      );
    });
  });
});
