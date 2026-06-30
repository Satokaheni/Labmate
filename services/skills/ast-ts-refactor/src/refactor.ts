// src/refactor.ts
// NEVER console.log — ALWAYS console.error
import * as path from "node:path";
import { Project, SourceFile } from "ts-morph";
import type { Diff, Reference } from "./types.js";

export class TsRefactor {
  private projects = new Map<string, Project>(); // tsconfig -> Project (cache)

  private getProject(tsconfig: string): Project {
    // tsconfig MUST be absolute path — ts-morph misinterprets relative paths.
    if (!path.isAbsolute(tsconfig)) {
      throw new Error(`tsconfig must be an absolute path, got: ${tsconfig}`);
    }
    let project = this.projects.get(tsconfig);
    if (project === undefined) {
      console.error(`[ts-refactor] loading project from ${tsconfig}`);
      project = new Project({ tsConfigFilePath: tsconfig });
      this.projects.set(tsconfig, project);
    }
    return project;
  }

  /** Capture the current text of every source file as a baseline for diffing. */
  private snapshot(project: Project): Map<string, string> {
    const baseline = new Map<string, string>();
    for (const sf of project.getSourceFiles()) {
      baseline.set(sf.getFilePath(), sf.getFullText());
    }
    return baseline;
  }

  /** Build a Diff of pending in-memory changes vs. the supplied baseline. Does NOT save. */
  private toDiff(project: Project, baseline: Map<string, string>): Diff {
    const filesAffected: string[] = [];
    const diffParts: string[] = [];
    let changes = 0;

    for (const sf of project.getSourceFiles()) {
      const filePath = sf.getFilePath();
      const before = baseline.get(filePath) ?? "";
      const after = sf.getFullText();
      if (before === after) continue;
      filesAffected.push(filePath);
      const fileDiff = this.unifiedDiff(filePath, before, after);
      diffParts.push(fileDiff.text);
      changes += fileDiff.hunks;
    }

    // Files newly created by a move that did not exist in the baseline.
    for (const sf of project.getSourceFiles()) {
      const filePath = sf.getFilePath();
      if (!baseline.has(filePath)) {
        filesAffected.push(filePath);
        const fileDiff = this.unifiedDiff(filePath, "", sf.getFullText());
        diffParts.push(fileDiff.text);
        changes += fileDiff.hunks;
      }
    }

    return {
      unified_diff: diffParts.join("\n"),
      files_affected: [...new Set(filesAffected)],
      changes,
    };
  }

  /** Minimal line-based unified diff for one file. Returns the diff text and a changed-hunk count. */
  private unifiedDiff(filePath: string, before: string, after: string): { text: string; hunks: number } {
    const a = before.split("\n");
    const b = after.split("\n");
    const lines: string[] = [`--- a/${filePath}`, `+++ b/${filePath}`];
    let hunks = 0;
    // Simple LCS-free walk: emit removals for a, additions for b around the first divergence.
    let i = 0;
    let j = 0;
    while (i < a.length || j < b.length) {
      if (i < a.length && j < b.length && a[i] === b[j]) {
        i++;
        j++;
        continue;
      }
      // Find the next matching anchor to bound the changed block.
      const anchorB = j < b.length ? b.indexOf(a[i] ?? " ", j) : -1;
      if (i < a.length && anchorB === -1) {
        lines.push(`-${a[i]}`);
        i++;
        hunks++;
      } else if (j < b.length) {
        lines.push(`+${b[j]}`);
        j++;
        hunks++;
      } else {
        i++;
      }
    }
    return { text: lines.join("\n"), hunks };
  }

  /** Find the SourceFile for `file`, throwing a clear error if absent. */
  private getSourceFile(project: Project, file: string): SourceFile {
    const sf =
      project.getSourceFile(file) ??
      (path.isAbsolute(file) ? project.getSourceFile(path.resolve(file)) : undefined) ??
      project.getSourceFiles().find((s) => s.getFilePath().endsWith(file));
    if (sf === undefined) {
      throw new Error(`file not found in project: ${file}`);
    }
    return sf;
  }

  /** Find the single source file that DECLARES `symbol`. Searches all project source
   * files for a top-level declaration (function/class/variable/interface/type alias/enum)
   * named `symbol`. Returns its absolute file path.
   * Throws a clear error on 0 matches ("no declaration found for symbol '<s>'") or
   * >1 matches ("symbol '<s>' is declared in multiple files: <list> — pass `file` to disambiguate").
   */
  private resolveDeclaringFile(project: Project, symbol: string): string {
    const matches: string[] = [];
    for (const sf of project.getSourceFiles()) {
      // Ignore node_modules and declaration files
      if (sf.isInNodeModules() || sf.isDeclarationFile()) continue;

      // Check for top-level declaration matching the symbol
      const found =
        sf.getFunction(symbol) ??
        sf.getClass(symbol) ??
        sf.getVariableDeclaration(symbol) ??
        sf.getInterface(symbol) ??
        sf.getTypeAlias(symbol) ??
        sf.getEnum(symbol);

      if (found !== undefined) {
        matches.push(sf.getFilePath());
      }
    }

    if (matches.length === 0) {
      throw new Error(`no declaration found for symbol '${symbol}'`);
    }
    if (matches.length > 1) {
      const list = matches.map((f) => path.basename(f)).join(", ");
      throw new Error(
        `symbol '${symbol}' is declared in multiple files: ${list} — pass \`file\` to disambiguate`,
      );
    }
    return matches[0];
  }

  /** Locate a renameable/named declaration node for `symbol` in `file`. */
  private locateDeclaration(project: Project, file: string, symbol: string) {
    const sf = this.getSourceFile(project, file);
    // getExportedDeclarations covers exported symbols (the common refactor target).
    const exported = sf.getExportedDeclarations().get(symbol);
    if (exported && exported.length > 0) {
      return exported[0];
    }
    // Fall back to any locally declared identifier matching the name.
    const local =
      sf.getFunction(symbol) ??
      sf.getClass(symbol) ??
      sf.getInterface(symbol) ??
      sf.getTypeAlias(symbol) ??
      sf.getEnum(symbol) ??
      sf.getVariableDeclaration(symbol);
    if (local === undefined) {
      throw new Error(`symbol '${symbol}' not found in ${file}`);
    }
    return local;
  }

  findReferences(tsconfig: string, file: string | undefined, symbol: string): Reference[] {
    const project = this.getProject(tsconfig);
    const resolvedFile = file || this.resolveDeclaringFile(project, symbol);
    const decl = this.locateDeclaration(project, resolvedFile, symbol);

    // The declaration node may be a NameableNode; get the identifier to search from.
    const nameNode =
      "getNameNode" in decl && typeof (decl as any).getNameNode === "function"
        ? (decl as any).getNameNode()
        : decl;

    const results: Reference[] = [];
    const referencedSymbols = nameNode.findReferences();
    for (const referencedSymbol of referencedSymbols) {
      for (const ref of referencedSymbol.getReferences()) {
        const node = ref.getNode();
        const sf = ref.getSourceFile();
        const start = node.getStart();
        const { line, column } = sf.getLineAndColumnAtPos(start);
        results.push({
          file: sf.getFilePath(),
          line,
          column,
          text: node.getText(),
          is_definition: ref.isDefinition(),
        });
      }
    }
    console.error(`[ts-refactor] find_references '${symbol}': ${results.length} sites`);
    return results;
  }

  renameSymbol(tsconfig: string, file: string | undefined, symbol: string, newName: string): Diff {
    const project = this.getProject(tsconfig);
    const baseline = this.snapshot(project);
    const resolvedFile = file || this.resolveDeclaringFile(project, symbol);
    const decl = this.locateDeclaration(project, resolvedFile, symbol);

    if (!("rename" in decl) || typeof (decl as any).rename !== "function") {
      throw new Error(`symbol '${symbol}' in ${file} is not renameable`);
    }
    (decl as any).rename(newName); // type-checker-driven cross-file rename; in-memory only

    const diff = this.toDiff(project, baseline);
    console.error(
      `[ts-refactor] rename '${symbol}' -> '${newName}': ${diff.changes} changes across ${diff.files_affected.length} files (NOT saved)`,
    );
    return diff;
  }

  moveSymbol(tsconfig: string, sourceFile: string, symbol: string, destFile: string): Diff {
    const project = this.getProject(tsconfig);
    const baseline = this.snapshot(project);

    const src = this.getSourceFile(project, sourceFile);
    const decl = this.locateDeclaration(project, sourceFile, symbol);

    // Ensure destination file exists in the project (created in-memory; saved only on confirm).
    const destPath = path.isAbsolute(destFile) ? destFile : path.resolve(path.dirname(src.getFilePath()), destFile);
    const dest = project.getSourceFile(destPath) ?? project.createSourceFile(destPath, "", { overwrite: false });

    // Capture all reference sites BEFORE mutation so we can repoint imports afterward.
    const refs = this.findReferences(tsconfig, sourceFile, symbol);

    // Move the declaration's statement text into the destination file.
    // decl is already the top-level declaration node (FunctionDeclaration, ClassDeclaration, etc.)
    // For exported declarations from getExportedDeclarations(), getText() includes the full declaration.
    // We need to get the parent statement if decl is e.g. a VariableDeclaration inside a VariableStatement.
    let declText: string;
    const parent = (decl as any).getParent?.();
    if (parent && typeof (parent as any).getText === "function" && parent.getKindName?.() === "VariableStatement") {
      declText = (parent as any).getText();
    } else {
      declText = typeof (decl as any).getText === "function" ? (decl as any).getText() : String(decl);
    }
    dest.addStatements(declText.startsWith("export") ? declText : `export ${declText}`);

    // Remove the original declaration from the source file.
    // For VariableDeclaration nodes, we must remove the parent VariableStatement.
    const nodeToRemove =
      (decl as any).getParent?.()?.getKindName?.() === "VariableStatement"
        ? (decl as any).getParent()
        : decl;
    if (typeof (nodeToRemove as any).remove === "function") {
      (nodeToRemove as any).remove();
    } else {
      throw new Error(`cannot remove declaration '${symbol}' from ${sourceFile}`);
    }

    // Add an import of the moved symbol in every file that referenced it (except the dest).
    const importPathFor = (fromFile: SourceFile): string => {
      let rel = path.relative(path.dirname(fromFile.getFilePath()), destPath).replace(/\.tsx?$/, "");
      if (!rel.startsWith(".")) rel = `./${rel}`;
      return rel;
    };
    const touched = new Set<string>();
    for (const ref of refs) {
      if (ref.is_definition) continue;
      const refSf = project.getSourceFile(ref.file);
      if (refSf === undefined || refSf.getFilePath() === destPath) continue;
      if (touched.has(refSf.getFilePath())) continue;
      touched.add(refSf.getFilePath());
      const existing = refSf
        .getImportDeclarations()
        .find((d) => d.getModuleSpecifierValue() === importPathFor(refSf));
      if (existing) {
        if (!existing.getNamedImports().some((n) => n.getName() === symbol)) {
          existing.addNamedImport(symbol);
        }
      } else {
        refSf.addImportDeclaration({ moduleSpecifier: importPathFor(refSf), namedImports: [symbol] });
      }
    }

    const diff = this.toDiff(project, baseline);
    console.error(
      `[ts-refactor] move '${symbol}' ${sourceFile} -> ${destFile}: ${diff.changes} changes across ${diff.files_affected.length} files (NOT saved)`,
    );
    return diff;
  }
}
