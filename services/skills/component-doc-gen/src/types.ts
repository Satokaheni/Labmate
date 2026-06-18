// src/types.ts

/** A single prop of a React component, extracted from its Props interface/type. */
export interface PropDef {
  name: string;
  type: string;                  // TypeScript type string (e.g. "string", "() => void", "'sm' | 'lg'")
  required: boolean;
  default_value: string | null;  // literal default if discoverable, else null
  description: string;           // from JSDoc comment if present, else ""
}

/** Full generated documentation for one React component. */
export interface ComponentDoc {
  component_name: string;
  file_path: string;
  props: PropDef[];
  props_table: string;     // markdown table
  story_code: string;      // Storybook CSF3 story ("" when stories not requested)
  markdown_doc: string;    // full markdown documentation
}
