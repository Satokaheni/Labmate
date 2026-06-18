// src/docgen.ts
// NEVER console.log — ALWAYS console.error
import type { PropDef, ComponentDoc } from "./types.js";

export class DocGenerator {
  /** Render a PropDef[] as a GitHub-flavored markdown table. */
  renderPropsTable(props: PropDef[]): string {
    const header =
      "| Prop | Type | Required | Default | Description |\n" +
      "| --- | --- | --- | --- | --- |";
    if (props.length === 0) {
      return `${header}\n| _(none)_ | | | | |`;
    }
    const rows = props.map((p) => {
      const type = p.type.replace(/\|/g, "\\|");
      const required = p.required ? "yes" : "no";
      const def = p.default_value === null ? "" : p.default_value.replace(/\|/g, "\\|");
      const desc = p.description.replace(/\|/g, "\\|").replace(/\n/g, " ");
      return `| \`${p.name}\` | \`${type}\` | ${required} | ${def} | ${desc} |`;
    });
    return [header, ...rows].join("\n");
  }

  /** Best-effort literal sample value for a prop, used in story args. */
  private sampleValue(prop: PropDef): string {
    if (prop.default_value !== null) return prop.default_value;
    const t = prop.type.replace(/\s+/g, "");
    if (/^['"`]/.test(t) || t.includes("|") && /['"]/.test(t)) {
      // Union of string literals — pick the first member.
      const first = t.split("|")[0];
      return first.startsWith("'") || first.startsWith('"') ? first : `'${first}'`;
    }
    if (t === "string") return "'text'";
    if (t === "number") return "0";
    if (t === "boolean") return "false";
    if (/\)=>|=>/.test(t) || t.includes("=>")) return "() => {}";
    if (t.endsWith("[]") || t.startsWith("Array<")) return "[]";
    if (t === "ReactNode" || t === "React.ReactNode") return "'children'";
    return "undefined";
  }

  /** Render a Storybook CSF3 story file for the component. */
  renderStory(componentName: string, props: PropDef[], importPath: string): string {
    const requiredArgs = props
      .filter((p) => p.required)
      .map((p) => `    ${p.name}: ${this.sampleValue(p)},`)
      .join("\n");
    const argsBlock = requiredArgs.length > 0 ? `\n  args: {\n${requiredArgs}\n  },\n` : "\n";

    return [
      `import type { Meta, StoryObj } from '@storybook/react';`,
      `import { ${componentName} } from '${importPath}';`,
      ``,
      `const meta: Meta<typeof ${componentName}> = {`,
      `  title: 'Components/${componentName}',`,
      `  component: ${componentName},`,
      `};`,
      ``,
      `export default meta;`,
      `type Story = StoryObj<typeof ${componentName}>;`,
      ``,
      `export const Default: Story = {${argsBlock}};`,
      ``,
    ].join("\n");
  }

  /** Render the full markdown documentation for the component. */
  renderMarkdownDoc(componentName: string, propsTable: string, description: string): string {
    const intro = description.trim().length > 0 ? `\n${description.trim()}\n` : "";
    return [
      `# ${componentName}`,
      intro,
      `## Props`,
      ``,
      propsTable,
      ``,
    ].join("\n");
  }

  /** Assemble a ComponentDoc from extracted props. */
  generate(
    componentName: string,
    filePath: string,
    props: PropDef[],
    includeStories: boolean,
    description = "",
  ): ComponentDoc {
    const propsTable = this.renderPropsTable(props);
    const importPath = `./${filePath.replace(/^.*\//, "").replace(/\.[jt]sx?$/, "")}`;
    const storyCode = includeStories ? this.renderStory(componentName, props, importPath) : "";
    const markdownDoc = this.renderMarkdownDoc(componentName, propsTable, description);
    return {
      component_name: componentName,
      file_path: filePath,
      props,
      props_table: propsTable,
      story_code: storyCode,
      markdown_doc: markdownDoc,
    };
  }
}
