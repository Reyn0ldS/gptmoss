---
name: documentation
description: Produce long, professional, source-grounded documents such as architecture dossiers, studies, policies, specifications, operating guides, and executive reports. Use when GPTMOSS must plan and draft a substantial deliverable from attached local DOCX, PPTX, TXT, Markdown, or HTML sources while preserving traceability and coherence.
allowed-tools: documents filesystem
---

# Write a Professional Long-Form Document

Prioritize accurate, complete content. Use simple Markdown structure until the user requests a richer final format.

## Workflow

1. Inventory the attached corpus with `documents.inventory`.
2. Define audience, decision purpose, scope, exclusions, mandatory sections, tone, and acceptance criteria.
3. Build a source-backed requirement matrix. Give each material requirement a stable ID.
4. Propose a hierarchical outline. Map every requirement, source, decision, risk, and open question to a target section.
5. Draft one section at a time:
   - state its purpose;
   - retrieve evidence with `documents.search`;
   - inspect decisive chunks with `documents.read_chunk` or `documents.read`;
   - write the section;
   - attach local references;
   - update the coverage matrix.
6. Consolidate terminology, acronyms, actors, component names, dates, numbers, and requirement IDs across all sections.
7. Remove repetition. Replace unsupported certainty with a labeled assumption, inference, recommendation, or information gap.
8. Assemble the Markdown deliverable, traceability matrix, and quality report.
9. Declare a `document` artifact validation policy with required headings, requirement and traceability IDs, allowed local source filenames, inventory bounds, minimum content metrics, and applicable repetition, terminology, placeholder, external-link, and claim-reference gates.
10. Let the execution engine apply the declared artifact validation policy automatically after the structured delivery response. Do not invoke repository-only validator scripts or invent a constraints file from an isolated project workspace. When standalone policy or report files are required, create only their declared owned paths from the actual policy and evidence.
11. Verify the saved files by reading them back, return the structured delivery response to trigger validation, and revise the owned artifact when the engine reports a concrete violation.

## Content Contract

A professional deliverable must distinguish:

- source facts;
- requirements;
- architecture or policy decisions;
- recommendations;
- assumptions;
- risks;
- unresolved questions.

Use tables only when they clarify mappings or comparisons. Keep headings descriptive. Prefer concrete decisions, responsibilities, interfaces, thresholds, and validation criteria over generic prose.

## Required Gates

Do not finish until:

- all mandatory sections are non-empty;
- every high-priority requirement is covered exactly where planned;
- material factual claims have local references;
- terminology is consistent;
- contradictions and assumptions are declared;
- executive summary and detailed body agree;
- the traceability matrix has no unexplained mandatory gap;
- output files are readable and contain no placeholder text.

When a gate fails, revise the affected section and rerun the checks.
