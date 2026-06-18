import type { AuditResult, RuleInfo } from "./types.js";
export declare class A11yAuditor {
    private browser;
    /** Launch the browser once; subsequent calls reuse the same instance. */
    private getBrowser;
    /** Close the browser cleanly. Idempotent. */
    close(): Promise<void>;
    private wcagLevelFromTags;
    /**
     * Navigate to a URL (http(s) or file://), run axe-core, return typed result.
     * @param navTarget fully-qualified URL the browser can load
     * @param label human-readable url_or_path echoed back in the result
     * @param rules optional axe rule IDs to restrict the run to
     */
    audit(navTarget: string, label: string, rules?: string[]): Promise<AuditResult>;
    auditFile(htmlOrComponentPath: string, rules?: string[]): Promise<AuditResult>;
    auditUrl(url: string, rules?: string[]): Promise<AuditResult>;
    listRules(): Promise<RuleInfo[]>;
}
