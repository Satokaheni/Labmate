// tests/refactor.test.ts
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { TsRefactor } from "../services/skills/ast-ts-refactor/src/refactor.js";

let tmp: string;

function write(rel: string, content: string): void {
  const full = path.join(tmp, rel);
  fs.mkdirSync(path.dirname(full), { recursive: true });
  fs.writeFileSync(full, content, "utf-8");
}

beforeEach(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), "ts-refactor-"));
  write(
    "tsconfig.json",
    JSON.stringify({ compilerOptions: { target: "ES2022", module: "NodeNext", strict: true }, include: ["src"] }),
  );
  // Declaration
  write("src/order.ts", `export function computeTotal(n: number): number {\n  return n * 2;\n}\n`);
  // Direct consumer
  write(
    "src/checkout.ts",
    `import { computeTotal } from "./order.js";\nexport const price = computeTotal(10);\n`,
  );
  // Barrel re-export
  write("src/index.ts", `export { computeTotal } from "./order.js";\n`);
  // Consumer via barrel
  write(
    "src/report.ts",
    `import { computeTotal } from "./index.js";\nexport const r = computeTotal(5);\n`,
  );
});

afterEach(() => {
  fs.rmSync(tmp, { recursive: true, force: true });
  vi.restoreAllMocks();
});

describe("renameSymbol", () => {
  it("renames across files including barrel re-export and returns a diff without saving", () => {
    const r = new TsRefactor();
    const tsconfig = path.join(tmp, "tsconfig.json");
    const diff = r.renameSymbol(tsconfig, path.join(tmp, "src/order.ts"), "computeTotal", "computeOrderTotal");

    expect(diff.changes).toBeGreaterThan(0);
    // All four files reference or declare the symbol.
    const affected = diff.files_affected.map((f) => path.basename(f)).sort();
    expect(affected).toContain("order.ts");
    expect(affected).toContain("checkout.ts");
    expect(affected).toContain("index.ts");
    expect(affected).toContain("report.ts");
    expect(diff.unified_diff).toContain("computeOrderTotal");

    // NOT auto-saved: on-disk files still contain the old name.
    const onDisk = fs.readFileSync(path.join(tmp, "src/order.ts"), "utf-8");
    expect(onDisk).toContain("computeTotal");
    expect(onDisk).not.toContain("computeOrderTotal");
  });
});

describe("findReferences", () => {
  it("returns all usage sites including the barrel re-export", () => {
    const r = new TsRefactor();
    const tsconfig = path.join(tmp, "tsconfig.json");
    const refs = r.findReferences(tsconfig, path.join(tmp, "src/order.ts"), "computeTotal");

    const files = new Set(refs.map((ref) => path.basename(ref.file)));
    expect(files.has("order.ts")).toBe(true);     // definition
    expect(files.has("checkout.ts")).toBe(true);  // direct import
    expect(files.has("index.ts")).toBe(true);     // barrel re-export
    expect(files.has("report.ts")).toBe(true);    // import via barrel

    expect(refs.some((ref) => ref.is_definition)).toBe(true);
    for (const ref of refs) {
      expect(ref.line).toBeGreaterThan(0);
      expect(ref.column).toBeGreaterThan(0);
      expect(typeof ref.text).toBe("string");
    }
  });
});

describe("moveSymbol", () => {
  it("moves the symbol to a new file and rewrites imports", () => {
    const r = new TsRefactor();
    const tsconfig = path.join(tmp, "tsconfig.json");
    const diff = r.moveSymbol(
      tsconfig,
      path.join(tmp, "src/order.ts"),
      "computeTotal",
      path.join(tmp, "src/totals.ts"),
    );

    expect(diff.changes).toBeGreaterThan(0);
    const affected = diff.files_affected.map((f) => path.basename(f));
    expect(affected).toContain("totals.ts"); // destination created
    expect(affected).toContain("order.ts");  // source lost the declaration
    // The destination diff contains the moved declaration.
    expect(diff.unified_diff).toContain("computeTotal");

    // NOT auto-saved.
    expect(fs.existsSync(path.join(tmp, "src/totals.ts"))).toBe(false);
  });
});

describe("absolute path enforcement", () => {
  it("throws when tsconfig is a relative path", () => {
    const r = new TsRefactor();
    expect(() => r.findReferences("tsconfig.json", "src/order.ts", "computeTotal")).toThrow(
      /absolute path/i,
    );
  });
});

describe("stdout hygiene", () => {
  it("never calls console.log; uses console.error for logging", () => {
    const logSpy = vi.spyOn(console, "log").mockImplementation(() => {});
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});

    const r = new TsRefactor();
    const tsconfig = path.join(tmp, "tsconfig.json");
    r.findReferences(tsconfig, path.join(tmp, "src/order.ts"), "computeTotal");
    r.renameSymbol(tsconfig, path.join(tmp, "src/order.ts"), "computeTotal", "computeOrderTotal");

    expect(logSpy).toHaveBeenCalledTimes(0);
    expect(errSpy.mock.calls.length).toBeGreaterThan(0);
  });
});
