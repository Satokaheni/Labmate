export type IssueCategory =
  | 'state_effects'
  | 'performance'
  | 'architecture'
  | 'security'
  | 'accessibility';

export type IssueSeverity = 'error' | 'warning' | 'info';

export interface Issue {
  rule_id: string;
  category: IssueCategory;
  severity: IssueSeverity;
  file: string;
  line: number;
  column: number;
  message: string;
}

export interface AuditResult {
  project_path: string;
  issue_count: number;
  issues: Issue[];
  ci_mode: boolean; // true = only new issues vs baseline
  exit_code: number;
}

/** Returned (not thrown) when the CLI is missing or exits unexpectedly. */
export interface AuditError {
  error: true;
  project_path: string;
  message: string;
  exit_code: number | null;
}

export function isAuditError(r: AuditResult | AuditError): r is AuditError {
  return (r as AuditError).error === true;
}
