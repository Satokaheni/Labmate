import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["../../../tests/refactor.test.ts"],
    environment: "node",
  },
});
