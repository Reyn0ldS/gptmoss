# Skills and attachments

GPTMOSS discovers trusted local skills from `gptmoss/skills/**/SKILL.md` and from `<workspace>/skills/**/SKILL.md`.

Each skill uses a small YAML-like frontmatter followed by instructions:

```markdown
---
name: my-skill
description: Concise purpose.
allowed_capabilities: [filesystem]
---
Instructions given to the agent only when this skill is selected.
```

Pass explicit skills with `agent_config.skills` when creating an execution. Otherwise GPTMOSS ranks local skills from the task text. A selected skill limits the exposed capability schemas to its declared capabilities. Skills requiring tools that GPTMOSS does not provide remain instructions only and should be adapted rather than executed blindly.

Bundled general skills cover secure Python, architecture, review, testing, and documentation. Bundled domain skills cover requirements/feasibility, computer vision and ML, 3D geometry, digital garments, backend APIs, 3D frontends, integration/delivery, and biometric privacy. Selection uses the exact specialist assignment and expertise, not only the parent task, so sibling agents can receive different instructions.

## Files and images

Upload text, Markdown, JSON, CSV, PNG, JPEG, or WebP through `POST /artifacts` with `filename`, `content_type`, and base64 `content_base64`. The response contains an artifact id. Pass it in `attachment_ids` to `POST /executions`.

Uploads are limited to 10 MiB, normalised to a safe filename, MIME/signature checked for images, stored under the workspace `uploads/` directory, and traced by SHA-256. Text is added to agent context. Images are passed to models whose name advertises vision support (`vision`, `-vl`, or `omni`); other models receive an explicit attachment notice.

PDF and DOCX extraction deliberately remains an optional extension: add a dedicated parser only after selecting its dependency and security policy.

## Autonomous specialization

GPTMOSS persists planner-invented specialists in `<workspace>/agents/**/AGENT.json`. If registry coverage is below the configured threshold, it can synthesize a procedural skill under `<workspace>/skills/auto-*/`, statically validate it, run a non-executable isolated procedure trial, and hot-load it. Concrete delivery failures can revise generated skills; previous revisions are archived.

Generated skills are untrusted prompt material. Their capability list is intersected with registered kernel capabilities, and delegation capabilities are excluded. A generated skill cannot register executable code, change policy, bypass approvals, access secrets, or grant itself permissions. Built-in and manually managed skills are never automatically rewritten.
