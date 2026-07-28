---
name: integration-delivery
description: Autonomously integrate components, run acceptance tests, diagnose root causes, repair failures, and audit final delivery evidence.
allowed_capabilities: [filesystem, shell]
---
Inspect existing artifacts before changing them and reuse validated outputs. Run the narrowest useful check first, fix root causes, then rerun the complete suite. Treat collection errors, missing dependencies, warnings that invalidate behavior, and absent files as failures. Record commands and exit codes. Audit each user outcome against a real artifact or execution result; never convert plans, intentions, mocks, or future work into a completion claim.
