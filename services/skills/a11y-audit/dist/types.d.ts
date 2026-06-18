export type Impact = "critical" | "serious" | "moderate" | "minor";
export type WcagLevel = "A" | "AA" | "AAA";
export interface ViolationNode {
    html: string;
    target: string[];
    failure_summary: string;
}
export interface Violation {
    id: string;
    impact: Impact;
    description: string;
    wcag_level: WcagLevel;
    nodes: ViolationNode[];
}
export interface AuditResult {
    url_or_path: string;
    violations: Violation[];
    passes: number;
    incomplete: number;
    inapplicable: number;
    violation_count: number;
}
export interface RuleInfo {
    id: string;
    description: string;
    wcag_level: WcagLevel;
    tags: string[];
}
export interface AuditError {
    error: string;
    url_or_path: string;
}
