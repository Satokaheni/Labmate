// src/types.ts

/** Pending in-memory changes captured as a git-style unified diff (NOT yet saved to disk). */
export interface Diff {
  unified_diff: string;     // git-style unified diff (pending, not yet saved)
  files_affected: string[]; // list of file paths that would change
  changes: number;          // total replacement count
}

/** A single usage site of a symbol. */
export interface Reference {
  file: string;
  line: number;
  column: number;
  text: string;             // source text of the reference
  is_definition: boolean;
}
