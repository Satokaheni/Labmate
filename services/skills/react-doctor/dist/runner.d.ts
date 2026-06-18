import type { AuditResult, AuditError } from './types.js';
export declare class ReactDoctorRunner {
    private cmd;
    constructor(cmd?: string);
    audit(projectPath: string, rules?: string[], ciMode?: boolean): Promise<AuditResult | AuditError>;
    listRules(): Promise<Record<string, string[]>>;
    private parseOutput;
    private normalizeIssue;
    private parseRules;
    private toError;
    /** Spawn the CLI and collect stdout/stderr. CLI stderr is forwarded to console.error. */
    private run;
}
