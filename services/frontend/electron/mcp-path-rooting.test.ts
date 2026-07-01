import { describe, it, expect } from 'vitest';
import { resolveMcpPathArgs, injectCodegraphProjectPath } from './mcp-path-rooting.js';

describe('resolveMcpPathArgs', () => {
  describe('single-value path keys', () => {
    it('resolves a relative path-key to absolute', () => {
      const result = resolveMcpPathArgs(
        { tsconfig: 'services/frontend/tsconfig.json' },
        ['/Users/me/repo'],
      );
      expect(result.tsconfig).toBe('/Users/me/repo/services/frontend/tsconfig.json');
    });

    it('leaves an absolute path unchanged', () => {
      const result = resolveMcpPathArgs(
        { file: '/Users/me/repo/src/a.ts' },
        ['/Users/me/repo'],
      );
      expect(result.file).toBe('/Users/me/repo/src/a.ts');
    });

    it('ignores empty string paths', () => {
      const result = resolveMcpPathArgs({ path: '' }, ['/Users/me/repo']);
      expect(result.path).toBe('');
    });

    it('leaves non-path keys untouched', () => {
      const result = resolveMcpPathArgs(
        { tsconfig: 'src/tsconfig.json', new_name: 'MyComponent' },
        ['/Users/me/repo'],
      );
      expect(result.tsconfig).toBe('/Users/me/repo/src/tsconfig.json');
      expect(result.new_name).toBe('MyComponent');
    });
  });

  describe('array path keys', () => {
    it('resolves each element in a files array', () => {
      const result = resolveMcpPathArgs(
        { files: ['src/a.ts', 'src/b.ts'] },
        ['/Users/me/repo'],
      );
      expect(result.files).toEqual([
        '/Users/me/repo/src/a.ts',
        '/Users/me/repo/src/b.ts',
      ]);
    });

    it('handles mixed absolute and relative paths in an array', () => {
      const result = resolveMcpPathArgs(
        { paths: ['src/a.ts', '/Users/me/repo/src/b.ts'] },
        ['/Users/me/repo'],
      );
      expect(result.paths).toEqual([
        '/Users/me/repo/src/a.ts',
        '/Users/me/repo/src/b.ts',
      ]);
    });

    it('handles empty arrays', () => {
      const result = resolveMcpPathArgs({ files: [] }, ['/Users/me/repo']);
      expect(result.files).toEqual([]);
    });

    it('ignores empty strings in arrays', () => {
      const result = resolveMcpPathArgs(
        { files: ['src/a.ts', '', 'src/b.ts'] },
        ['/Users/me/repo'],
      );
      expect(result.files).toEqual([
        '/Users/me/repo/src/a.ts',
        '',
        '/Users/me/repo/src/b.ts',
      ]);
    });
  });

  describe('multiple roots', () => {
    it('resolves against the primary (first) root only', () => {
      const result = resolveMcpPathArgs(
        { file: 'src/a.ts' },
        ['/primary', '/secondary'],
      );
      expect(result.file).toBe('/primary/src/a.ts');
    });

    it('returns args unchanged when roots is empty', () => {
      const result = resolveMcpPathArgs(
        { file: 'src/a.ts', tsconfig: 'tsconfig.json' },
        [],
      );
      expect(result).toEqual({
        file: 'src/a.ts',
        tsconfig: 'tsconfig.json',
      });
    });
  });

  describe('immutability', () => {
    it('does not mutate the input object', () => {
      const input = { tsconfig: 'src/tsconfig.json' };
      const result = resolveMcpPathArgs(input, ['/Users/me/repo']);
      expect(input.tsconfig).toBe('src/tsconfig.json'); // unchanged
      expect(result.tsconfig).toBe('/Users/me/repo/src/tsconfig.json'); // changed
      expect(result).not.toBe(input); // different objects
    });

    it('does not mutate array values', () => {
      const files = ['src/a.ts', 'src/b.ts'];
      const input = { files };
      const result = resolveMcpPathArgs(input, ['/Users/me/repo']);
      expect(input.files).toBe(files); // same array reference
      expect(result.files).not.toBe(files); // different array
      expect(result.files).toEqual([
        '/Users/me/repo/src/a.ts',
        '/Users/me/repo/src/b.ts',
      ]);
    });
  });

  describe('all path key names', () => {
    it('resolves all registered path key names', () => {
      const result = resolveMcpPathArgs(
        {
          path: 'p1',
          file: 'p2',
          tsconfig: 'p3',
          source_file: 'p4',
          dest_file: 'p5',
          dir: 'p6',
          directory: 'p7',
          filepath: 'p8',
          file_path: 'p9',
          files: ['p10'],
          paths: ['p11'],
          project: 'p12',
          component_path: 'p13',
          dir_path: 'p14',
          html_or_component_path: 'p15',
        },
        ['/root'],
      );
      expect(result).toEqual({
        path: '/root/p1',
        file: '/root/p2',
        tsconfig: '/root/p3',
        source_file: '/root/p4',
        dest_file: '/root/p5',
        dir: '/root/p6',
        directory: '/root/p7',
        filepath: '/root/p8',
        file_path: '/root/p9',
        files: ['/root/p10'],
        paths: ['/root/p11'],
        project: '/root/p12',
        component_path: '/root/p13',
        dir_path: '/root/p14',
        html_or_component_path: '/root/p15',
      });
    });

    it('resolves the React-skill path args (component-doc-gen, a11y-audit)', () => {
      // component-doc-gen enforces absolute component_path/dir_path;
      // a11y-audit enforces absolute html_or_component_path.
      const result = resolveMcpPathArgs(
        {
          component_path: 'src/components/Button.tsx',
          dir_path: 'src/components',
          html_or_component_path: 'src/pages/Home.tsx',
        },
        ['/Users/me/repo'],
      );
      expect(result.component_path).toBe('/Users/me/repo/src/components/Button.tsx');
      expect(result.dir_path).toBe('/Users/me/repo/src/components');
      expect(result.html_or_component_path).toBe('/Users/me/repo/src/pages/Home.tsx');
    });
  });

  describe('edge cases', () => {
    it('handles a single root', () => {
      const result = resolveMcpPathArgs({ file: 'src/a.ts' }, ['/root']);
      expect(result.file).toBe('/root/src/a.ts');
    });

    it('handles args with no path keys', () => {
      const result = resolveMcpPathArgs({ foo: 'bar', baz: 42 }, ['/root']);
      expect(result).toEqual({ foo: 'bar', baz: 42 });
    });

    it('handles args as empty object', () => {
      const result = resolveMcpPathArgs({}, ['/root']);
      expect(result).toEqual({});
    });

    it('preserves non-string, non-array values in path keys', () => {
      const result = resolveMcpPathArgs(
        { file: 123, tsconfig: null, path: undefined },
        ['/root'],
      );
      expect(result).toEqual({
        file: 123,
        tsconfig: null,
        path: undefined,
      });
    });

    it('normalizes path separators on the platform', () => {
      const result = resolveMcpPathArgs({ file: 'src/nested/a.ts' }, ['/root']);
      // path.join normalizes to platform separator
      expect(result.file).toBe('/root/src/nested/a.ts');
    });
  });
});

describe('injectCodegraphProjectPath', () => {
  it('defaults projectPath to the primary workspace root for a codegraph tool', () => {
    const out = injectCodegraphProjectPath(
      'mcp__codegraph__codegraph_search',
      { query: 'auth' },
      ['/Users/me/repo', '/other'],
    );
    expect(out).toEqual({ query: 'auth', projectPath: '/Users/me/repo' });
  });

  it('respects an explicit projectPath (model targeting another repo)', () => {
    const out = injectCodegraphProjectPath(
      'mcp__codegraph__codegraph_status',
      { projectPath: '/some/other/repo' },
      ['/Users/me/repo'],
    );
    expect(out.projectPath).toBe('/some/other/repo');
  });

  it('treats an empty-string projectPath as absent and injects', () => {
    const out = injectCodegraphProjectPath(
      'mcp__codegraph__codegraph_search',
      { projectPath: '', query: 'x' },
      ['/Users/me/repo'],
    );
    expect(out.projectPath).toBe('/Users/me/repo');
  });

  it('leaves non-codegraph mcp tools untouched', () => {
    const args = { symbol: 'foo', new_name: 'bar' };
    const out = injectCodegraphProjectPath('mcp__ast-ts-refactor__rename_symbol', args, ['/Users/me/repo']);
    expect(out).toBe(args);
    expect('projectPath' in out).toBe(false);
  });

  it('returns args unchanged when there are no roots', () => {
    const args = { query: 'x' };
    const out = injectCodegraphProjectPath('mcp__codegraph__codegraph_search', args, []);
    expect(out).toBe(args);
  });

  it('does not mutate the input', () => {
    const args = { query: 'x' };
    const out = injectCodegraphProjectPath('mcp__codegraph__codegraph_search', args, ['/r']);
    expect(args).not.toHaveProperty('projectPath');
    expect(out).not.toBe(args);
    expect(out.projectPath).toBe('/r');
  });
});
