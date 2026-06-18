import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["../../../tests/parser.test.ts", "../../../tests/docgen.test.ts"],
    environment: "node",
  },
});
