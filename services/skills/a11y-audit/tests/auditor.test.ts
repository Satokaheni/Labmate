// tests/auditor.test.ts
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// vi.hoisted() runs before vi.mock() factories — the returned object is
// accessible inside the factory closures as well as in the test body.
const mocks = vi.hoisted(() => {
  return {
    launchCount: 0,
    gotoFn: vi.fn(),
    pageCloseFn: vi.fn(),
    newPageFn: vi.fn(),
    analyzeFn: vi.fn(),
    withRulesFn: vi.fn(),
    evaluateFn: vi.fn(),
    browserCloseFn: vi.fn(),
  };
});

vi.mock("playwright", () => {
  const page = {
    get goto() { return mocks.gotoFn; },
    get close() { return mocks.pageCloseFn; },
    get evaluate() { return mocks.evaluateFn; },
  };

  mocks.newPageFn.mockResolvedValue(page);

  const browser = {
    isConnected: () => true,
    get newPage() { return mocks.newPageFn; },
    get close() { return mocks.browserCloseFn; },
  };

  const launch = vi.fn(async () => {
    mocks.launchCount += 1;
    return browser;
  });

  return {
    chromium: { launch },
    firefox: { launch },
    webkit: { launch },
  };
});

vi.mock("@axe-core/playwright", () => {
  class AxeBuilder {
    withRules(...args: unknown[]) {
      mocks.withRulesFn(...args);
      return this;
    }
    analyze() {
      return mocks.analyzeFn();
    }
  }
  return { AxeBuilder };
});

import { A11yAuditor } from "../src/auditor.js";

const sampleAxeResult = {
  violations: [
    {
      id: "color-contrast",
      impact: "serious",
      description: "Elements must have sufficient color contrast",
      tags: ["wcag2aa", "wcag143"],
      nodes: [
        {
          html: "<button>Go</button>",
          target: [".btn"],
          failureSummary: "Fix contrast ratio",
        },
      ],
    },
  ],
  passes: [{}, {}],
  incomplete: [{}],
  inapplicable: [{}, {}, {}],
};

beforeEach(() => {
  mocks.launchCount = 0;
  mocks.gotoFn.mockReset().mockResolvedValue(null); // file:// returns null
  mocks.pageCloseFn.mockReset().mockResolvedValue(undefined);
  mocks.analyzeFn.mockReset().mockResolvedValue(sampleAxeResult);
  mocks.withRulesFn.mockReset();
  mocks.evaluateFn.mockReset();
  mocks.browserCloseFn.mockReset().mockResolvedValue(undefined);

  // Re-wire newPageFn each beforeEach so goto/close refs are fresh
  mocks.newPageFn.mockReset().mockResolvedValue({
    goto: mocks.gotoFn,
    close: mocks.pageCloseFn,
    evaluate: mocks.evaluateFn,
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("auditFile", () => {
  it("navigates to a file:// URL", async () => {
    const auditor = new A11yAuditor();
    await auditor.auditFile("/tmp/page.html");
    expect(mocks.gotoFn).toHaveBeenCalledTimes(1);
    const navTarget = mocks.gotoFn.mock.calls[0][0] as string;
    expect(navTarget.startsWith("file://")).toBe(true);
    expect(navTarget.endsWith("/tmp/page.html")).toBe(true);
  });
});

describe("result mapping", () => {
  it("maps axe violations into typed Violation objects", async () => {
    const auditor = new A11yAuditor();
    const result = await auditor.auditFile("/tmp/page.html");

    expect(result.violation_count).toBe(1);
    expect(result.passes).toBe(2);
    expect(result.incomplete).toBe(1);
    expect(result.inapplicable).toBe(3);

    const v = result.violations[0];
    expect(v.id).toBe("color-contrast");
    expect(v.impact).toBe("serious");
    expect(v.wcag_level).toBe("AA"); // derived from wcag2aa tag
    expect(v.nodes[0].target).toEqual([".btn"]);
    expect(v.nodes[0].failure_summary).toBe("Fix contrast ratio");
    expect(v.nodes[0].html).toBe("<button>Go</button>");
  });
});

describe("rules filter", () => {
  it("passes rules to AxeBuilder.withRules", async () => {
    const auditor = new A11yAuditor();
    await auditor.auditFile("/tmp/page.html", ["color-contrast", "image-alt"]);
    expect(mocks.withRulesFn).toHaveBeenCalledWith(["color-contrast", "image-alt"]);
  });

  it("does not call withRules when rules omitted", async () => {
    const auditor = new A11yAuditor();
    await auditor.auditFile("/tmp/page.html");
    expect(mocks.withRulesFn).not.toHaveBeenCalled();
  });
});

describe("browser reuse", () => {
  it("launches the browser once across multiple audits", async () => {
    const auditor = new A11yAuditor();
    await auditor.auditFile("/tmp/a.html");
    await auditor.auditFile("/tmp/b.html");
    expect(mocks.launchCount).toBe(1);
  });
});

describe("load failure", () => {
  it("throws when an http response is not ok", async () => {
    mocks.gotoFn.mockResolvedValue({ ok: () => false, status: () => 404 });
    const auditor = new A11yAuditor();
    await expect(auditor.auditUrl("http://localhost:3000/missing")).rejects.toThrow(/404/);
  });

  it("closes the page even when axe throws", async () => {
    mocks.analyzeFn.mockRejectedValue(new Error("axe boom"));
    const auditor = new A11yAuditor();
    await expect(auditor.auditFile("/tmp/page.html")).rejects.toThrow(/axe boom/);
    expect(mocks.pageCloseFn).toHaveBeenCalledTimes(1);
  });
});

describe("stdout discipline", () => {
  it("never calls console.log", async () => {
    const logSpy = vi.spyOn(console, "log").mockImplementation(() => {});
    const auditor = new A11yAuditor();
    await auditor.auditFile("/tmp/page.html");
    await auditor.close();
    expect(logSpy).not.toHaveBeenCalled();
  });
});
