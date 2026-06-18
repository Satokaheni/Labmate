import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { z } from 'zod';
import { ReactDoctorRunner } from './runner.js';
import { isAuditError } from './types.js';

const runner = new ReactDoctorRunner();

const server = new McpServer({
  name: 'react-doctor',
  version: '0.1.0',
});

server.registerTool(
  'react_doctor.audit',
  {
    title: 'Audit a React project',
    description:
      'Run react-doctor static analysis on a React project. Returns JSONL of issues, ' +
      'one per line, each with rule_id, category, severity, file, line, column, message.',
    inputSchema: {
      project_path: z.string().describe('Absolute path to the React project root'),
      rules: z
        .array(z.string())
        .optional()
        .describe('Optional list of rule IDs to restrict the audit to'),
      ci_mode: z
        .boolean()
        .optional()
        .default(false)
        .describe('When true, report only newly introduced issues vs the baseline'),
    },
  },
  async ({ project_path, rules, ci_mode }) => {
    const result = await runner.audit(project_path, rules, ci_mode ?? false);

    if (isAuditError(result)) {
      return {
        isError: true,
        content: [{ type: 'text' as const, text: JSON.stringify(result) }],
      };
    }

    // Emit JSONL: one issue per line. Empty result => empty string.
    const jsonl = result.issues.map((i) => JSON.stringify(i)).join('\n');
    const summary = JSON.stringify({
      project_path: result.project_path,
      issue_count: result.issue_count,
      ci_mode: result.ci_mode,
      exit_code: result.exit_code,
    });

    return {
      content: [{ type: 'text' as const, text: `${summary}\n${jsonl}`.trimEnd() }],
    };
  },
);

server.registerTool(
  'react_doctor.list_rules',
  {
    title: 'List react-doctor rules',
    description:
      'List all available react-doctor rule IDs grouped by category ' +
      '(state_effects, performance, architecture, security, accessibility).',
    inputSchema: {},
  },
  async () => {
    const rules = await runner.listRules();
    return { content: [{ type: 'text' as const, text: JSON.stringify(rules, null, 2) }] };
  },
);

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error('[react-doctor] MCP server started on stdio');
}

main().catch((err) => {
  console.error('[react-doctor] fatal:', err);
  process.exit(1);
});
