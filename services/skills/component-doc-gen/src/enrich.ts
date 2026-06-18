// src/enrich.ts
// NEVER console.log — ALWAYS console.error
import type { PropDef } from "./types.js";

/**
 * Optionally ask Gemma 4 (OpenAI-compatible endpoint) to write a one-paragraph
 * human-readable description from the prop signatures. Returns "" when GEMMA_BASE
 * is unset or on any error — the AST output is always the source of truth.
 */
export async function enrichDescription(componentName: string, props: PropDef[]): Promise<string> {
  const base = process.env.GEMMA_BASE;
  if (!base) return ""; // disabled by default — deterministic, no network

  const signature = props
    .map((p) => `${p.name}${p.required ? "" : "?"}: ${p.type}`)
    .join("; ");
  const prompt =
    `Write a single concise paragraph describing the React component "${componentName}" ` +
    `based only on these props: ${signature}. Do not invent behavior not implied by the props.`;

  try {
    const res = await fetch(`${base.replace(/\/$/, "")}/v1/chat/completions`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        model: process.env.GEMMA_MODEL ?? "google/gemma-4-31B-it",
        messages: [{ role: "user", content: prompt }],
        max_tokens: 200,
        temperature: 0.2,
      }),
    });
    if (!res.ok) {
      console.error(`[component-doc-gen] enrichment HTTP ${res.status}; falling back to ""`);
      return "";
    }
    const data = (await res.json()) as { choices?: Array<{ message?: { content?: string } }> };
    return (data.choices?.[0]?.message?.content ?? "").trim();
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    console.error(`[component-doc-gen] enrichment failed: ${message}; falling back to ""`);
    return "";
  }
}
