// tests/docgen.test.ts
import { describe, it, expect, vi } from "vitest";
import { glob } from "glob";
import { DocGenerator } from "../services/skills/component-doc-gen/src/docgen.js";
import type { PropDef } from "../services/skills/component-doc-gen/src/types.js";

const PROPS: PropDef[] = [
  { name: "label", type: "string", required: true, default_value: null, description: "The label." },
  { name: "size", type: "'sm' | 'md' | 'lg'", required: false, default_value: "'md'", description: "Size." },
  { name: "onClick", type: "() => void", required: false, default_value: null, description: "" },
];

describe("DocGenerator.renderPropsTable", () => {
  it("produces a markdown table with the correct columns and escaped unions", () => {
    const table = new DocGenerator().renderPropsTable(PROPS);
    const lines = table.split("\n");
    expect(lines[0]).toBe("| Prop | Type | Required | Default | Description |");
    expect(lines[1]).toBe("| --- | --- | --- | --- | --- |");
    expect(table).toContain("`label`");
    expect(table).toContain("yes");
    expect(table).toContain("no");
    expect(table).toContain("\\|"); // union pipe escaped
    expect(table).toContain("'md'"); // default rendered
  });

  it("handles a component with no props", () => {
    const table = new DocGenerator().renderPropsTable([]);
    expect(table).toContain("_(none)_");
  });
});

describe("DocGenerator.renderStory", () => {
  it("emits a CSF3 story with a default export and a named export", () => {
    const story = new DocGenerator().renderStory("Button", PROPS, "./Button");
    expect(story).toContain("import type { Meta, StoryObj } from '@storybook/react';");
    expect(story).toContain("import { Button } from './Button';");
    expect(story).toContain("export default meta;");          // CSF3 default export
    expect(story).toContain("export const Default: Story =");  // named export
    expect(story).toContain("label: 'text'");                 // required arg filled
    expect(story).not.toContain("onClick:");                  // optional arg omitted
  });
});

describe("DocGenerator.generate", () => {
  it("assembles a ComponentDoc and omits story_code when stories disabled", () => {
    const gen = new DocGenerator();
    const withStory = gen.generate("Button", "/abs/Button.tsx", PROPS, true);
    expect(withStory.story_code.length).toBeGreaterThan(0);
    expect(withStory.markdown_doc).toContain("# Button");
    expect(withStory.props_table).toContain("| Prop |");

    const noStory = gen.generate("Button", "/abs/Button.tsx", PROPS, false);
    expect(noStory.story_code).toBe("");
  });
});

describe("stdout hygiene", () => {
  it("never calls console.log; uses console.error for logging in the parser", async () => {
    const logSpy = vi.spyOn(console, "log").mockImplementation(() => {});
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});

    const fs = await import("node:fs");
    const os = await import("node:os");
    const path = await import("node:path");
    const { ComponentParser } = await import(
      "../services/skills/component-doc-gen/src/parser.js"
    );

    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "component-doc-log-"));
    const file = path.join(tmp, "Button.tsx");
    fs.writeFileSync(
      file,
      `export interface ButtonProps { label: string }\nexport function Button(p: ButtonProps){return null}\nexport default Button;\n`,
      "utf-8",
    );

    new ComponentParser().extractProps(file);

    expect(logSpy).toHaveBeenCalledTimes(0);
    expect(errSpy.mock.calls.length).toBeGreaterThan(0);

    fs.rmSync(tmp, { recursive: true, force: true });
    vi.restoreAllMocks();
  });
});

describe("batch shape", () => {
  it("produces one JSONL line per matching .tsx file", async () => {
    const fs = await import("node:fs");
    const os = await import("node:os");
    const path = await import("node:path");
    const { ComponentParser } = await import(
      "../services/skills/component-doc-gen/src/parser.js"
    );

    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "component-doc-batch-"));
    for (const name of ["Button", "Card"]) {
      fs.writeFileSync(
        path.join(dir, `${name}.tsx`),
        `export interface ${name}Props { id: string }\nexport function ${name}(p: ${name}Props){return null}\nexport default ${name};\n`,
        "utf-8",
      );
    }

    const files = await glob("**/*.tsx", { cwd: dir, absolute: true, nodir: true });
    const parser = new ComponentParser();
    const gen = new DocGenerator();
    const lines = files.map((f) => {
      const { componentName, props, filePath } = parser.extractProps(f);
      return JSON.stringify(gen.generate(componentName, filePath, props, true));
    });

    expect(lines.length).toBe(2);
    for (const line of lines) {
      const doc = JSON.parse(line);
      expect(doc.component_name).toMatch(/Button|Card/);
      expect(Array.isArray(doc.props)).toBe(true);
    }

    fs.rmSync(dir, { recursive: true, force: true });
  });
});

describe("resolveComponentPath", () => {
  it("existing absolute path is returned unchanged (REGRESSION-CRITICAL)", async () => {
    const fs = await import("node:fs");
    const os = await import("node:os");
    const path = await import("node:path");
    const { resolveComponentPath } = await import(
      "../services/skills/component-doc-gen/src/index.js"
    );

    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "component-abs-"));
    const componentFile = path.join(dir, "Button.tsx");
    fs.writeFileSync(
      componentFile,
      `export interface ButtonProps { label: string }\nexport function Button(p: ButtonProps){return null}\nexport default Button;\n`,
      "utf-8",
    );

    const result = resolveComponentPath(componentFile);
    expect(result).toBe(path.resolve(componentFile));

    fs.rmSync(dir, { recursive: true, force: true });
  });

  it("absolute non-existent path is searched by basename", async () => {
    const fs = await import("node:fs");
    const os = await import("node:os");
    const path = await import("node:path");
    const { resolveComponentPath } = await import(
      "../services/skills/component-doc-gen/src/index.js"
    );

    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "component-search-"));
    const srcDir = path.join(dir, "src");
    fs.mkdirSync(srcDir);
    const componentFile = path.join(srcDir, "Button.tsx");
    fs.writeFileSync(
      componentFile,
      `export function Button(){return null}\n`,
      "utf-8",
    );

    // Dispatch-rooting case: absolute but non-existent path to a dir, search by basename
    const result = resolveComponentPath(path.join(dir, "Button"));
    expect(result).toBe(path.resolve(componentFile));

    fs.rmSync(dir, { recursive: true, force: true });
  });

  it("throws when component name has no matches", async () => {
    const fs = await import("node:fs");
    const os = await import("node:os");
    const path = await import("node:path");
    const { resolveComponentPath } = await import(
      "../services/skills/component-doc-gen/src/index.js"
    );

    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "component-nomatch-"));
    // Create no component files

    expect(() => {
      resolveComponentPath(path.join(dir, "NoSuchComp"));
    }).toThrow(/no component file found/);

    fs.rmSync(dir, { recursive: true, force: true });
  });

  it("throws when multiple files match the basename", async () => {
    const fs = await import("node:fs");
    const os = await import("node:os");
    const path = await import("node:path");
    const { resolveComponentPath } = await import(
      "../services/skills/component-doc-gen/src/index.js"
    );

    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "component-multi-"));
    const subdir1 = path.join(dir, "a");
    const subdir2 = path.join(dir, "b");
    fs.mkdirSync(subdir1);
    fs.mkdirSync(subdir2);

    fs.writeFileSync(
      path.join(subdir1, "Button.tsx"),
      `export function Button(){return null}\n`,
      "utf-8",
    );
    fs.writeFileSync(
      path.join(subdir2, "Button.tsx"),
      `export function Button(){return null}\n`,
      "utf-8",
    );

    expect(() => {
      resolveComponentPath(path.join(dir, "Button"));
    }).toThrow(/multiple files match/);

    fs.rmSync(dir, { recursive: true, force: true });
  });

  it("end-to-end: resolves and generates doc with correct component_name", async () => {
    const fs = await import("node:fs");
    const os = await import("node:os");
    const path = await import("node:path");
    const { resolveComponentPath } = await import(
      "../services/skills/component-doc-gen/src/index.js"
    );
    const { ComponentParser } = await import(
      "../services/skills/component-doc-gen/src/parser.js"
    );
    const { DocGenerator } = await import(
      "../services/skills/component-doc-gen/src/docgen.js"
    );

    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "component-e2e-"));
    const srcDir = path.join(dir, "src");
    fs.mkdirSync(srcDir);
    const componentFile = path.join(srcDir, "Checkbox.tsx");
    fs.writeFileSync(
      componentFile,
      `export interface CheckboxProps { checked: boolean }\nexport function Checkbox(p: CheckboxProps){return null}\nexport default Checkbox;\n`,
      "utf-8",
    );

    // Resolve by name (absolute non-existent path)
    const resolved = resolveComponentPath(path.join(dir, "Checkbox"));
    expect(resolved).toBe(path.resolve(componentFile));

    // Parse and generate doc
    const parser = new ComponentParser();
    const { componentName, props, filePath } = parser.extractProps(resolved);
    expect(componentName).toBe("Checkbox");

    const gen = new DocGenerator();
    const doc = gen.generate(componentName, filePath, props, true);
    expect(doc.component_name).toBe("Checkbox");

    fs.rmSync(dir, { recursive: true, force: true });
  });
});
