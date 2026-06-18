// tests/parser.test.ts
import { describe, it, expect, beforeEach, afterEach } from "vitest";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { ComponentParser } from "../services/skills/component-doc-gen/src/parser.js";

let tmp: string;

function write(rel: string, content: string): string {
  const full = path.join(tmp, rel);
  fs.mkdirSync(path.dirname(full), { recursive: true });
  fs.writeFileSync(full, content, "utf-8");
  return full;
}

beforeEach(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), "component-doc-"));
});

afterEach(() => {
  fs.rmSync(tmp, { recursive: true, force: true });
});

const BUTTON = `
import React from 'react';

export interface ButtonProps {
  /** The visible button label. */
  label: string;
  /** Visual size of the button.
   * @default 'md'
   */
  size?: 'sm' | 'md' | 'lg';
  /** Click handler. */
  onClick?: () => void;
  disabled?: boolean;
}

export function Button(props: ButtonProps) {
  return <button>{props.label}</button>;
}

export default Button;
`;

describe("ComponentParser.extractProps", () => {
  it("extracts prop names, types, and required status", () => {
    const file = write("Button.tsx", BUTTON);
    const parser = new ComponentParser();
    const { componentName, props } = parser.extractProps(file);

    expect(componentName).toBe("Button");
    const byName = Object.fromEntries(props.map((p) => [p.name, p]));

    expect(byName.label.required).toBe(true);
    expect(byName.label.type).toBe("string");

    expect(byName.size.required).toBe(false);
    expect(byName.size.type).toContain("'sm'");
    expect(byName.size.type).toContain("'lg'");

    expect(byName.onClick.required).toBe(false);
    expect(byName.disabled.required).toBe(false);
  });
});

describe("ComponentParser JSDoc", () => {
  it("captures JSDoc descriptions and @default values", () => {
    const file = write("Button.tsx", BUTTON);
    const parser = new ComponentParser();
    const { props } = parser.extractProps(file);
    const byName = Object.fromEntries(props.map((p) => [p.name, p]));

    expect(byName.label.description).toBe("The visible button label.");
    expect(byName.size.description).toContain("Visual size");
    expect(byName.size.default_value).toBe("'md'");
    expect(byName.disabled.description).toBe(""); // no JSDoc
  });

  it("supports a type-literal alias and a component with no props", () => {
    const parser = new ComponentParser();

    const aliasFile = write(
      "Card.tsx",
      `type CardProps = { title: string };\nexport function Card(p: CardProps) { return null; }\nexport default Card;\n`,
    );
    const alias = parser.extractProps(aliasFile);
    expect(alias.props.map((p) => p.name)).toEqual(["title"]);

    const noPropsFile = write(
      "Spacer.tsx",
      `export function Spacer() { return null; }\nexport default Spacer;\n`,
    );
    const none = parser.extractProps(noPropsFile);
    expect(none.props).toEqual([]);
  });

  it("throws on a relative component_path", () => {
    const parser = new ComponentParser();
    expect(() => parser.extractProps("Button.tsx")).toThrow(/absolute path/i);
  });
});
