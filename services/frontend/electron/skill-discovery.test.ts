import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { parseSkillMd, discoverSkills, skillDescriptors } from './skill-discovery.js';

describe('skill-discovery', () => {
  describe('parseSkillMd', () => {
    it('should parse valid SKILL.md frontmatter and body', () => {
      const text = `---
name: test-skill
description: A test skill for discovery
tools:
  - tool1
  - tool2
---

# Test Skill

This is the body of the skill.
It can span multiple lines.

## Tools

- tool1: does something
- tool2: does something else
`;

      const result = parseSkillMd(text);
      expect(result).not.toBeNull();
      expect(result?.name).toBe('test-skill');
      expect(result?.description).toBe('A test skill for discovery');
      expect(result?.body).toContain('# Test Skill');
      expect(result?.body).toContain('This is the body of the skill.');
    });

    it('should handle multiline descriptions with YAML folded indicator (>)', () => {
      const text = `---
name: my-skill
description: >
  This is a multiline description
  that spans several lines
  and should be collapsed into one
other_field: value
---

# Body starts here
`;

      const result = parseSkillMd(text);
      expect(result).not.toBeNull();
      expect(result?.name).toBe('my-skill');
      // Description should be joined with spaces
      expect(result?.description).toContain('multiline description');
      expect(result?.description).toContain('spans several lines');
    });

    it('should strip quotes from name and description', () => {
      const text = `---
name: "quoted-skill"
description: "A skill with quoted description"
---

# Body
`;

      const result = parseSkillMd(text);
      expect(result?.name).toBe('quoted-skill');
      expect(result?.description).toBe('A skill with quoted description');
    });

    it('should return null if no leading frontmatter delimiter', () => {
      const text = `name: test-skill
description: test
---

# Body
`;

      const result = parseSkillMd(text);
      expect(result).toBeNull();
    });

    it('should return null if missing closing frontmatter delimiter', () => {
      const text = `---
name: test-skill
description: test

# Body without closing ---
`;

      const result = parseSkillMd(text);
      expect(result).toBeNull();
    });

    it('should return null if name is missing', () => {
      const text = `---
description: A skill without a name
---

# Body
`;

      const result = parseSkillMd(text);
      expect(result).toBeNull();
    });

    it('should return null if description is missing', () => {
      const text = `---
name: nameless-skill
---

# Body
`;

      const result = parseSkillMd(text);
      expect(result).toBeNull();
    });

    it('should extract body correctly without frontmatter', () => {
      const text = `---
name: skill1
description: desc1
---

# First section

Content here
More content
`;

      const result = parseSkillMd(text);
      expect(result?.body).not.toContain('---');
      expect(result?.body).not.toContain('name:');
      expect(result?.body).toContain('# First section');
      expect(result?.body).toContain('Content here');
    });
  });

  describe('discoverSkills', () => {
    let tmpDir: string;

    beforeEach(() => {
      // Create a temporary directory for test fixtures
      tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'skill-discovery-test-'));
    });

    afterEach(() => {
      // Clean up temporary directory
      if (fs.existsSync(tmpDir)) {
        fs.rmSync(tmpDir, { recursive: true, force: true });
      }
    });

    it('should discover valid skills in subdirectories', () => {
      // Create skill1
      const skill1Dir = path.join(tmpDir, 'skill-one');
      fs.mkdirSync(skill1Dir);
      fs.writeFileSync(
        path.join(skill1Dir, 'SKILL.md'),
        `---
name: skill-one
description: First skill
---

# Body
`
      );

      // Create skill2
      const skill2Dir = path.join(tmpDir, 'skill-two');
      fs.mkdirSync(skill2Dir);
      fs.writeFileSync(
        path.join(skill2Dir, 'SKILL.md'),
        `---
name: skill-two
description: Second skill
---

# Body
`
      );

      const skills = discoverSkills(tmpDir);
      expect(skills).toHaveLength(2);
      expect(skills[0].name).toBe('skill-one');
      expect(skills[1].name).toBe('skill-two');
    });

    it('should skip directories without SKILL.md', () => {
      // Create skill1 with SKILL.md
      const skill1Dir = path.join(tmpDir, 'skill-one');
      fs.mkdirSync(skill1Dir);
      fs.writeFileSync(
        path.join(skill1Dir, 'SKILL.md'),
        `---
name: skill-one
description: First skill
---

# Body
`
      );

      // Create skill2 WITHOUT SKILL.md
      const skill2Dir = path.join(tmpDir, 'skill-two');
      fs.mkdirSync(skill2Dir);
      fs.writeFileSync(path.join(skill2Dir, 'other-file.md'), '# Not a SKILL.md');

      const skills = discoverSkills(tmpDir);
      expect(skills).toHaveLength(1);
      expect(skills[0].name).toBe('skill-one');
    });

    it('should skip SKILL.md files with invalid frontmatter', () => {
      // Valid skill
      const skill1Dir = path.join(tmpDir, 'skill-one');
      fs.mkdirSync(skill1Dir);
      fs.writeFileSync(
        path.join(skill1Dir, 'SKILL.md'),
        `---
name: skill-one
description: Valid skill
---

# Body
`
      );

      // Invalid skill (missing description)
      const skill2Dir = path.join(tmpDir, 'skill-two');
      fs.mkdirSync(skill2Dir);
      fs.writeFileSync(
        path.join(skill2Dir, 'SKILL.md'),
        `---
name: skill-two
---

# Body
`
      );

      const skills = discoverSkills(tmpDir);
      expect(skills).toHaveLength(1);
      expect(skills[0].name).toBe('skill-one');
    });

    it('should return skills sorted by name', () => {
      // Create skills in non-alphabetical order
      for (const name of ['zebra-skill', 'alpha-skill', 'beta-skill']) {
        const dir = path.join(tmpDir, name);
        fs.mkdirSync(dir);
        fs.writeFileSync(
          path.join(dir, 'SKILL.md'),
          `---
name: ${name}
description: ${name} description
---

# Body
`
        );
      }

      const skills = discoverSkills(tmpDir);
      expect(skills).toHaveLength(3);
      expect(skills[0].name).toBe('alpha-skill');
      expect(skills[1].name).toBe('beta-skill');
      expect(skills[2].name).toBe('zebra-skill');
    });

    it('should return empty array if directory does not exist', () => {
      const nonexistent = path.join(tmpDir, 'does-not-exist');
      const skills = discoverSkills(nonexistent);
      expect(skills).toEqual([]);
    });

    it('should handle read errors gracefully', () => {
      const skill1Dir = path.join(tmpDir, 'skill-one');
      fs.mkdirSync(skill1Dir);
      fs.writeFileSync(
        path.join(skill1Dir, 'SKILL.md'),
        `---
name: skill-one
description: First skill
---

# Body
`
      );

      // Create a directory where a SKILL.md is expected (to trigger read error)
      const skill2Dir = path.join(tmpDir, 'skill-two');
      fs.mkdirSync(skill2Dir);
      fs.mkdirSync(path.join(skill2Dir, 'SKILL.md')); // This will cause a read error

      // Should not throw; just skip the invalid skill
      const skills = discoverSkills(tmpDir);
      expect(skills).toHaveLength(1);
      expect(skills[0].name).toBe('skill-one');
    });
  });

  describe('skillDescriptors', () => {
    let tmpDir: string;

    beforeEach(() => {
      tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'skill-descriptors-test-'));
    });

    afterEach(() => {
      if (fs.existsSync(tmpDir)) {
        fs.rmSync(tmpDir, { recursive: true, force: true });
      }
    });

    it('should convert discovered skills to SkillToolDescriptor[] with correct shape', () => {
      const skillDir = path.join(tmpDir, 'test-skill');
      fs.mkdirSync(skillDir);
      fs.writeFileSync(
        path.join(skillDir, 'SKILL.md'),
        `---
name: test-skill
description: A test skill
---

# Skill Body
Content here
`
      );

      const descriptors = skillDescriptors(tmpDir);
      expect(descriptors).toHaveLength(1);

      const descriptor = descriptors[0];
      expect(descriptor.name).toBe('test-skill');
      expect(descriptor.source).toBe('skill');
      expect(descriptor.description).toBe('A test skill');
      expect(descriptor.body).toContain('# Skill Body');
      expect(descriptor.body).toContain('Content here');
      expect(descriptor).not.toHaveProperty('schema');
      expect(descriptor).not.toHaveProperty('namespace');
    });

    it('should return multiple descriptors sorted by name', () => {
      for (const name of ['z-skill', 'a-skill']) {
        const dir = path.join(tmpDir, name);
        fs.mkdirSync(dir);
        fs.writeFileSync(
          path.join(dir, 'SKILL.md'),
          `---
name: ${name}
description: ${name} description
---

# Body
`
        );
      }

      const descriptors = skillDescriptors(tmpDir);
      expect(descriptors).toHaveLength(2);
      expect(descriptors[0].name).toBe('a-skill');
      expect(descriptors[1].name).toBe('z-skill');
    });

    it('should include skill body with instructions', () => {
      const skillDir = path.join(tmpDir, 'instruction-skill');
      fs.mkdirSync(skillDir);

      fs.writeFileSync(
        path.join(skillDir, 'SKILL.md'),
        `---
name: instruction-skill
description: A skill with instructions
---

# Instructions

Use this skill to do something.

## Tools

- tool1
- tool2

## Example

\`\`\`json
{"example": "data"}
\`\`\`
`
      );

      const descriptors = skillDescriptors(tmpDir);
      expect(descriptors[0].body).toContain('# Instructions');
      expect(descriptors[0].body).toContain('Use this skill to do something.');
      expect(descriptors[0].body).toContain('## Tools');
      expect(descriptors[0].body).toContain('## Example');
    });

    it('should return empty array when no valid skills exist', () => {
      const descriptors = skillDescriptors(tmpDir);
      expect(descriptors).toEqual([]);
    });
  });
});
