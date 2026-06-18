// auditor.ts — A11yAuditor: launches one headless browser, reuses it across audits
import { chromium, firefox, webkit } from "playwright";
import { AxeBuilder } from "@axe-core/playwright";
import { pathToFileURL } from "node:url";
import { resolve } from "node:path";
const BROWSER_NAME = process.env.PLAYWRIGHT_BROWSER ?? "chromium";
function browserType(name) {
    switch (name) {
        case "firefox":
            return firefox;
        case "webkit":
            return webkit;
        case "chromium":
        default:
            return chromium;
    }
}
export class A11yAuditor {
    browser = null;
    /** Launch the browser once; subsequent calls reuse the same instance. */
    async getBrowser() {
        if (this.browser && this.browser.isConnected()) {
            return this.browser;
        }
        console.error(`[a11y-audit] launching ${BROWSER_NAME} (headless)`);
        this.browser = await browserType(BROWSER_NAME).launch({ headless: true });
        return this.browser;
    }
    /** Close the browser cleanly. Idempotent. */
    async close() {
        if (this.browser) {
            console.error("[a11y-audit] closing browser");
            await this.browser.close();
            this.browser = null;
        }
    }
    wcagLevelFromTags(tags) {
        const has = (frag) => tags.some((t) => t.includes(frag));
        if (has("aaa"))
            return "AAA";
        if (has("aa"))
            return "AA";
        return "A";
    }
    /**
     * Navigate to a URL (http(s) or file://), run axe-core, return typed result.
     * @param navTarget fully-qualified URL the browser can load
     * @param label human-readable url_or_path echoed back in the result
     * @param rules optional axe rule IDs to restrict the run to
     */
    async audit(navTarget, label, rules) {
        const browser = await this.getBrowser();
        const page = await browser.newPage();
        try {
            const response = await page.goto(navTarget, {
                waitUntil: "networkidle",
                timeout: 30_000,
            });
            // file:// returns null response; only treat http(s) non-2xx as failure
            if (response && !response.ok()) {
                throw new Error(`page load failed: HTTP ${response.status()} for ${label}`);
            }
            let builder = new AxeBuilder({ page });
            if (rules && rules.length > 0) {
                builder = builder.withRules(rules);
            }
            const results = await builder.analyze();
            const violations = results.violations.map((v) => ({
                id: v.id,
                impact: (v.impact ?? "minor"),
                description: v.description,
                wcag_level: this.wcagLevelFromTags(v.tags),
                nodes: v.nodes.map((n) => ({
                    html: n.html,
                    target: n.target.map((t) => String(t)),
                    failure_summary: n.failureSummary ?? "",
                })),
            }));
            return {
                url_or_path: label,
                violations,
                passes: results.passes.length,
                incomplete: results.incomplete.length,
                inapplicable: results.inapplicable.length,
                violation_count: violations.length,
            };
        }
        finally {
            await page.close();
        }
    }
    async auditFile(htmlOrComponentPath, rules) {
        const abs = resolve(htmlOrComponentPath);
        const fileUrl = pathToFileURL(abs).href;
        return this.audit(fileUrl, htmlOrComponentPath, rules);
    }
    async auditUrl(url, rules) {
        return this.audit(url, url, rules);
    }
    async listRules() {
        const browser = await this.getBrowser();
        const page = await browser.newPage();
        try {
            await page.goto("about:blank");
            // AxeBuilder injects axe into the page; trigger injection then read getRules()
            await new AxeBuilder({ page }).analyze();
            const rules = await page.evaluate(() => {
                // @ts-expect-error axe is injected into window by AxeBuilder
                return window.axe.getRules().map((r) => ({
                    id: r.ruleId,
                    description: r.description,
                    tags: r.tags,
                }));
            });
            return rules.map((r) => ({
                id: r.id,
                description: r.description,
                wcag_level: this.wcagLevelFromTags(r.tags),
                tags: r.tags,
            }));
        }
        finally {
            await page.close();
        }
    }
}
