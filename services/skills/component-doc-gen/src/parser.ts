// src/parser.ts
// NEVER console.log — ALWAYS console.error
import * as path from "node:path";
import {
  Project,
  SourceFile,
  InterfaceDeclaration,
  TypeAliasDeclaration,
  PropertySignature,
  Node,
} from "ts-morph";
import type { PropDef } from "./types.js";

export class ComponentParser {
  /** Load a single .tsx/.ts file into a throwaway ts-morph project. */
  private loadFile(componentPath: string): SourceFile {
    // ts-morph requires absolute paths; relative paths silently load the wrong file.
    if (!path.isAbsolute(componentPath)) {
      throw new Error(`component_path must be an absolute path, got: ${componentPath}`);
    }
    const project = new Project({
      compilerOptions: { allowJs: true, jsx: 4 /* ts.JsxEmit.ReactJSX */ },
      skipAddingFilesFromTsConfig: true,
    });
    console.error(`[component-doc-gen] loading ${componentPath}`);
    return project.addSourceFileAtPath(componentPath);
  }

  /** Best-effort component name: default export name, else PascalCased file basename. */
  private componentName(sf: SourceFile): string {
    const defaultExport = sf.getDefaultExportSymbol();
    if (defaultExport) {
      const decls = defaultExport.getDeclarations();
      for (const d of decls) {
        const named = (d as unknown as { getName?: () => string }).getName?.();
        if (named && named !== "default") return named;
      }
    }
    // Fall back to the first exported function/variable that looks like a component.
    for (const fn of sf.getFunctions()) {
      const name = fn.getName();
      if (name && /^[A-Z]/.test(name)) return name;
    }
    for (const v of sf.getVariableDeclarations()) {
      const name = v.getName();
      if (/^[A-Z]/.test(name)) return name;
    }
    const base = path.basename(sf.getFilePath()).replace(/\.[jt]sx?$/, "");
    return base.charAt(0).toUpperCase() + base.slice(1);
  }

  /** Find the interface or type alias that declares the component's props. */
  private findPropsContainer(
    sf: SourceFile,
    componentName: string,
  ): InterfaceDeclaration | TypeAliasDeclaration | undefined {
    const candidates = [`${componentName}Props`, "Props"];
    for (const name of candidates) {
      const iface = sf.getInterface(name);
      if (iface) return iface;
      const alias = sf.getTypeAlias(name);
      if (alias) return alias;
    }
    // Fallback: any interface/type alias whose name ends in "Props".
    const iface = sf.getInterfaces().find((i) => i.getName().endsWith("Props"));
    if (iface) return iface;
    const alias = sf.getTypeAliases().find((a) => a.getName().endsWith("Props"));
    return alias;
  }

  /** Convert a list of property signatures into PropDef records. */
  private propsFromSignatures(signatures: PropertySignature[]): PropDef[] {
    const props: PropDef[] = [];
    for (const sig of signatures) {
      const typeNode = sig.getTypeNode();
      const type = typeNode ? typeNode.getText() : sig.getType().getText(sig);
      const required = !sig.hasQuestionToken();

      // JSDoc description: prefer the first JSDoc comment block's description text.
      let description = "";
      let defaultValue: string | null = null;
      const docs = sig.getJsDocs();
      if (docs.length > 0) {
        description = docs[docs.length - 1].getDescription().trim();
        for (const doc of docs) {
          for (const tag of doc.getTags()) {
            if (tag.getTagName() === "default" || tag.getTagName() === "defaultValue") {
              defaultValue = (tag.getCommentText() ?? "").trim() || null;
            }
          }
        }
      }

      props.push({
        name: sig.getName(),
        type: type.replace(/\s+/g, " ").trim(),
        required,
        default_value: defaultValue,
        description,
      });
    }
    return props;
  }

  /** Extract the component name and its props from a single source file. */
  extractProps(componentPath: string): { componentName: string; props: PropDef[]; filePath: string } {
    const sf = this.loadFile(componentPath);
    const componentName = this.componentName(sf);
    const container = this.findPropsContainer(sf, componentName);

    let signatures: PropertySignature[] = [];
    if (container instanceof InterfaceDeclaration) {
      signatures = container.getProperties();
    } else if (container instanceof TypeAliasDeclaration) {
      const typeNode = container.getTypeNode();
      if (typeNode && Node.isTypeLiteral(typeNode)) {
        signatures = typeNode.getProperties();
      }
    }

    const props = this.propsFromSignatures(signatures);
    console.error(`[component-doc-gen] '${componentName}': ${props.length} props`);
    return { componentName, props, filePath: sf.getFilePath() };
  }
}
