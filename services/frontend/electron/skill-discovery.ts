import fs from 'node:fs';
import path from 'node:path';

/**
 * A discovered skill with name, description, and body (post-frontmatter content).
 */
export interface DiscoveredSkill {
  name: string;
  description: string;
  body: string;
}

/**
 * Tool descriptor for a skill (schema-less, containing body for instructions).
 */
export interface SkillToolDescriptor {
  name: string;
  source: 'skill';
  description: string;
  body: string;
}

/**
 * Parse YAML-ish frontmatter from a SKILL.md string.
 * Expects format:
 *   ---
 *   name: skill-name
 *   description: >
 *     multiline description
 *   ...
 *   ---
 *
 * Returns { name, description, body } on success, null if parsing fails or
 * required fields are missing.
 *
 * @param text - The full content of a SKILL.md file
 * @returns Parsed frontmatter + body, or null if invalid
 */
export function parseSkillMd(text: string): DiscoveredSkill | null {
  // Normalize line endings to LF
  text = text.replace(/\r\n/g, '\n');

  // Require leading ---
  if (!text.startsWith('---\n')) {
    return null;
  }

  // Find closing ---
  const bodyStartIndex = text.indexOf('\n---\n', 4); // skip first "---\n"
  if (bodyStartIndex === -1) {
    return null;
  }

  const frontmatterText = text.slice(4, bodyStartIndex); // between the ---s
  const body = text.slice(bodyStartIndex + 5); // after closing "---\n"

  // Parse frontmatter lines: extract name and description
  const lines = frontmatterText.split('\n');
  let name: string | null = null;
  let description: string | null = null;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    // Match "name: value" (trim whitespace, strip quotes)
    const nameMatch = line.match(/^name:\s*(.+?)$/);
    if (nameMatch) {
      name = nameMatch[1].trim().replace(/^["']|["']$/g, '');
    }

    // Match "description: value" (trim whitespace, strip quotes)
    // Handles both single-line and multiline (YAML folded >) descriptions
    const descMatch = line.match(/^description:\s*(.*)$/);
    if (descMatch) {
      let desc = descMatch[1].trim();

      // Handle YAML folded indicator (>)
      if (desc === '>') {
        // Collect continuation lines until next key or empty line
        const descLines: string[] = [];
        for (let j = i + 1; j < lines.length; j++) {
          const nextLine = lines[j];
          // Stop if we hit another key (line with ':' at start of word)
          if (nextLine.match(/^\w+:/)) {
            break;
          }
          // Add non-empty lines
          if (nextLine.trim()) {
            descLines.push(nextLine.trim());
          }
        }
        desc = descLines.join(' ');
      } else {
        // Strip quotes from simple single-line description
        desc = desc.replace(/^["']|["']$/g, '');
      }

      description = desc;
    }
  }

  // Require both name and description
  if (!name || !description) {
    return null;
  }

  return { name, description, body };
}

/**
 * Discover all SKILL.md skills in the given directory.
 * Scans immediate subdirectories; skips dirs without a valid SKILL.md.
 * Errors are logged to stderr but never thrown.
 *
 * @param skillsDir - Directory containing skill subdirectories
 * @returns Array of discovered skills, sorted by name
 */
export function discoverSkills(skillsDir: string): DiscoveredSkill[] {
  const discovered: DiscoveredSkill[] = [];

  try {
    if (!fs.existsSync(skillsDir)) {
      console.error(`Skills directory does not exist: ${skillsDir}`);
      return [];
    }

    const entries = fs.readdirSync(skillsDir, { withFileTypes: true });

    for (const entry of entries) {
      if (!entry.isDirectory()) continue;

      const skillMdPath = path.join(skillsDir, entry.name, 'SKILL.md');

      try {
        if (!fs.existsSync(skillMdPath)) {
          // Silently skip dirs without SKILL.md
          continue;
        }

        const content = fs.readFileSync(skillMdPath, 'utf8');
        const parsed = parseSkillMd(content);

        if (parsed) {
          discovered.push(parsed);
        } else {
          console.error(`Failed to parse SKILL.md in ${entry.name}: missing or invalid name/description`);
        }
      } catch (err) {
        console.error(
          `Error reading SKILL.md in ${entry.name}: ${err instanceof Error ? err.message : String(err)}`
        );
      }
    }
  } catch (err) {
    console.error(`Error scanning skills directory: ${err instanceof Error ? err.message : String(err)}`);
  }

  // Sort by name for determinism
  return discovered.sort((a, b) => a.name.localeCompare(b.name));
}

/**
 * Convert discovered skills to SkillToolDescriptor[] for the capability manifest.
 * Each descriptor has source:'skill', name, description, and body (no schema).
 *
 * @param skillsDir - Directory containing skill subdirectories
 * @returns Array of SkillToolDescriptor objects
 */
export function skillDescriptors(skillsDir: string): SkillToolDescriptor[] {
  const skills = discoverSkills(skillsDir);
  return skills.map((skill) => ({
    name: skill.name,
    source: 'skill' as const,
    description: skill.description,
    body: skill.body,
  }));
}
