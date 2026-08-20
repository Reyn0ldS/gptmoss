# Skills and attachments

GPTMOSS discovers trusted local skills from `gptmoss/skills/**/SKILL.md` and from `<workspace>/skills/**/SKILL.md`.

Each skill uses a small YAML-like frontmatter followed by instructions:

```markdown
---
name: my-skill
description: Concise purpose.
allowed-tools: documents filesystem
---
Instructions given to the agent only when this skill is selected.
```

Pass explicit skills with `agent_config.skills` when creating an execution. Otherwise GPTMOSS ranks local skills from the task text. By default a selected skill adds a procedure without removing general capabilities. With `strict_skill_capabilities=true`, declared tools also restrict the exposed schemas. The legacy `allowed_capabilities` field remains supported. Skills requiring tools that GPTMOSS does not provide remain instructions only and should be adapted rather than executed blindly.

Bundled general skills cover secure Python, local document analysis, architecture, review, testing, and professional long-form documentation. Narrow project packs such as computer vision, 3D geometry, digital garments, 3D frontends, and biometric privacy declare `auto-select: false`: they remain available through explicit `agent_config.skills` but never specialize unrelated projects automatically. Selection otherwise uses the exact specialist assignment and expertise, not only the parent task, so sibling agents can receive different instructions.

## Files, documents, and images

Upload TXT, Markdown, JSON, CSV, local HTML, DOCX, PPTX, PDF text, PNG, JPEG, or WebP through `POST /artifacts` with `filename`, `content_type`, and base64 `content_base64`. The response contains an artifact id. Pass it in `attachment_ids` to `POST /executions`.

Upload size is strictly bounded by `max_upload_bytes` (100 MiB by default, integer `>= 1`), and normalized text by `max_attachment_text_chars`. Files are normalized to a safe filename, content/signatures are checked, data is stored under the workspace `uploads/` directory, and every source is traced by SHA-256. Documents are normalized into structured blocks, cached, chunked, and indexed locally. The `documents` capability can access only explicit attachment IDs and exposes `inventory`, `search`, `read`, `read_chunk`, `read_image`, and `read_images`. Images are passed to models whose configured vision mode allows them; other models receive an explicit attachment notice.

HTML parsing never executes scripts or loads linked resources. DOCX and PPTX parsing uses the standard library and rejects unsafe OOXML archives. PDF text is extracted locally with `pypdf`. OCR of scanned pages, legacy `.doc`/`.ppt`, Office rendering, macros, and presenter notes remain outside the current contract. See [docs/local-document-workflow.md](docs/local-document-workflow.md) for MIME types, API examples, provenance syntax, quality policies, portable validation, and troubleshooting.

## Autonomous specialization

GPTMOSS persists planner-invented specialists in `<workspace>/agents/**/AGENT.json`. If registry coverage is below the configured threshold, it can synthesize a procedural skill under `<workspace>/skills/auto-*/`, statically validate it, run a non-executable isolated procedure trial, and hot-load it. Concrete delivery failures can revise generated skills; previous revisions are archived.

Generated skills are untrusted prompt material. Their capability list is intersected with registered kernel capabilities, and delegation capabilities are excluded. A generated skill cannot register executable code, change policy, bypass approvals, access secrets, or grant itself permissions. Built-in and manually managed skills are never automatically rewritten.
