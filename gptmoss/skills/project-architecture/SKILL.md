---
name: project-architecture
description: Design and document complete software or system architecture from requirements and attached local evidence. Use for architecture dossiers, target-state designs, modernization plans, integration landscapes, security and deployment designs, architecture decisions, migration roadmaps, or technical specifications requiring traceability and validation.
allowed-tools: documents filesystem
---

# Design Software and System Architecture

Produce an implementable architecture, not a generic technology catalogue. Keep project-specific engine commands or vendor routines in the deliverable rather than hard-coding them into GPTMOSS.

## Workflow

1. Use `documents.inventory`, `documents.search`, and `documents.read` to establish business drivers, actors, constraints, existing systems, decisions, and risks.
2. Assign stable IDs to functional requirements, quality attributes, constraints, assumptions, and decisions.
3. Define scope and system boundary. Identify external actors, systems, trust zones, data owners, and operational responsibilities.
4. Evaluate alternatives against explicit drivers. Record the chosen option and rejected alternatives as architecture decisions.
5. Develop mutually consistent views:
   - context and capabilities;
   - logical components and responsibilities;
   - information model, ownership, lifecycle, retention, and flows;
   - interfaces, protocols, contracts, errors, and versioning;
   - security, identity, authorization, secrets, audit, and threat mitigations;
   - deployment topology, environments, capacity, resilience, backup, and recovery;
   - observability, support, change, incident, and continuity operations;
   - migration, coexistence, rollback, decommissioning, and roadmap.
6. Trace each important component and decision to requirements and local sources.
7. Define measurable acceptance criteria, verification methods, and residual risks.
8. Review the whole dossier for contradictions between diagrams described in text, interfaces, data, security, deployment, operations, and migration.
9. Apply the built-in `document` artifact validator to the dossier. Require every mandatory architecture section and requirement ID, all traceability rows, every attached source, local reference bounds, minimum content metrics, consistent terminology, and no external links or placeholders.

## Decision Quality

For each major decision, state context, drivers, alternatives, decision, consequences, risks, and validation. Do not select a product merely because it is popular. Mark technology versions and capacities as proposals unless the local sources mandate them.

Keep external tools configurable. Provide commands, configurations, or integration routines as project artifacts when needed; do not make the GPTMOSS core directly control project-specific engines.

## Required Gates

Before finishing, verify:

- scope and boundaries are unambiguous;
- every mandatory requirement maps to at least one architecture element;
- every exposed interface has ownership, data contract, failure behavior, and security controls;
- quality attributes have measurable tactics and tests;
- deployment and operations support the stated availability and recovery goals;
- migration has checkpoints, rollback, and data reconciliation;
- assumptions, open decisions, and residual risks are explicit;
- all source references are local and verifiable;
- the final dossier and traceability matrix are non-empty and internally coherent.
