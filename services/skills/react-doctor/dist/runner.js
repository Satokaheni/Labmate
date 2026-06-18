import { spawn } from 'node:child_process';
const VALID_CATEGORIES = [
    'state_effects',
    'performance',
    'architecture',
    'security',
    'accessibility',
];
const VALID_SEVERITIES = ['error', 'warning', 'info'];
export class ReactDoctorRunner {
    cmd;
    constructor(cmd = process.env.REACT_DOCTOR_CMD ?? 'npx react-doctor@latest') {
        this.cmd = cmd;
    }
    async audit(projectPath, rules, ciMode = false) {
        const args = ['--json', '--cwd', projectPath];
        if (ciMode)
            args.push('--ci');
        if (rules && rules.length > 0)
            args.push('--rules', rules.join(','));
        let proc;
        try {
            proc = await this.run(args);
        }
        catch (err) {
            return this.toError(projectPath, err, null);
        }
        if (proc.spawnError) {
            return this.toError(projectPath, proc.spawnError, null);
        }
        // react-doctor exits non-zero when issues are found; that is NOT an error.
        // Only treat unparseable output as an error.
        let issues;
        try {
            issues = this.parseOutput(proc.stdout);
        }
        catch (err) {
            return this.toError(projectPath, err, proc.code);
        }
        return {
            project_path: projectPath,
            issue_count: issues.length,
            issues,
            ci_mode: ciMode,
            exit_code: proc.code ?? 0,
        };
    }
    async listRules() {
        const proc = await this.run(['--list-rules', '--json']);
        if (proc.spawnError) {
            console.error('[react-doctor] list_rules spawn failed:', proc.spawnError.message);
            return {};
        }
        return this.parseRules(proc.stdout);
    }
    parseOutput(stdout) {
        const trimmed = stdout.trim();
        if (!trimmed)
            return [];
        const parsed = JSON.parse(trimmed);
        const rawIssues = Array.isArray(parsed) ? parsed : (parsed.issues ?? []);
        return rawIssues.map((r) => this.normalizeIssue(r));
    }
    normalizeIssue(r) {
        const category = String(r.category ?? '');
        const severity = String(r.severity ?? 'info');
        return {
            rule_id: String(r.rule_id ?? r.ruleId ?? ''),
            category: VALID_CATEGORIES.includes(category) ? category : 'architecture',
            severity: VALID_SEVERITIES.includes(severity) ? severity : 'info',
            file: String(r.file ?? r.filePath ?? ''),
            line: Number(r.line ?? 0),
            column: Number(r.column ?? r.col ?? 0),
            message: String(r.message ?? ''),
        };
    }
    parseRules(stdout) {
        const trimmed = stdout.trim();
        if (!trimmed)
            return {};
        const parsed = JSON.parse(trimmed);
        const out = {};
        const rules = Array.isArray(parsed)
            ? parsed
            : (parsed.rules ?? []);
        for (const rule of rules) {
            const category = String(rule.category ?? 'architecture');
            const id = String(rule.rule_id ?? rule.ruleId ?? rule.id ?? '');
            if (!id)
                continue;
            (out[category] ??= []).push(id);
        }
        return out;
    }
    toError(projectPath, err, code) {
        console.error('[react-doctor] audit failed:', err.message);
        return { error: true, project_path: projectPath, message: err.message, exit_code: code };
    }
    /** Spawn the CLI and collect stdout/stderr. CLI stderr is forwarded to console.error. */
    run(args) {
        const [command, ...baseArgs] = this.cmd.split(' ');
        return new Promise((resolve) => {
            const child = spawn(command, [...baseArgs, ...args], {
                stdio: ['pipe', 'pipe', 'pipe'],
            });
            let stdout = '';
            let stderr = '';
            child.stdout.on('data', (d) => {
                stdout += d.toString();
            });
            child.stderr.on('data', (d) => {
                const text = d.toString();
                stderr += text;
                console.error('[react-doctor:cli]', text.trimEnd());
            });
            child.on('error', (err) => {
                resolve({ stdout, stderr, code: null, spawnError: err });
            });
            child.on('close', (code) => {
                resolve({ stdout, stderr, code });
            });
        });
    }
}
//# sourceMappingURL=runner.js.map