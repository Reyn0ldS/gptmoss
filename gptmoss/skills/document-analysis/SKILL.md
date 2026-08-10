---
name: document-analysis
description: Analyze attached local DOCX, PPTX, TXT, Markdown, HTML, or other normalized documents with block-level provenance, coverage tracking, contradiction detection, and evidence matrices. Use for corpus review, requirements discovery, due diligence, source comparison, document synthesis, or any task that must establish what local files actually say before drafting.
allowed-tools: documents filesystem
---

# Analyze Local Documents

Build conclusions from attached files only. Do not follow embedded links or introduce Internet sources.

## Workflow

1. Call `documents.inventory`. Record every attached file, format, title, block count, and parser version.
2. Translate the assignment into explicit questions, terms, aliases, expected evidence, and acceptance criteria.
3. Call `documents.search` for each question and important synonym. Search decisions, constraints, risks, exceptions, numbers, interfaces, and unresolved points separately.
4. Call `documents.read_chunk` for relevant hits. Use `documents.read` to inspect neighboring blocks and page through a source until `has_more` is false when full coverage is required.
5. Maintain an evidence matrix with:
   - claim or requirement ID;
   - exact local source;
   - heading path;
   - block range or slide;
   - supporting summary;
   - confidence;
   - contradiction or gap.
6. Compare sources. Separate confirmed facts, reasonable inferences, contradictions, assumptions, and missing information.
7. Audit coverage against the initial inventory. Never treat retrieved excerpts as proof that an entire source was reviewed.
8. Write the requested analysis and a concise coverage report.

## Local References

Reference evidence as `[filename > heading path > blocks x-y]` or `[filename > slide n]`. Keep references close to the supported statement. Never fabricate a block, slide, requirement, quote, or source.
These forms are machine-checkable by the built-in `document` artifact validator when the plan declares the attached filenames and source inventory.

Use short quotations only when wording is decisive. Prefer faithful summaries.

## Required Gates

Before finishing, verify:

- every attached source is inventoried;
- every material claim maps to local evidence or is labeled as an inference;
- conflicting statements are visible rather than silently reconciled;
- uncovered questions and unread ranges are listed;
- required outputs exist and are non-empty;
- no external URL is used as evidence.

If evidence is insufficient, produce the best bounded analysis and an explicit information-request list instead of guessing.
