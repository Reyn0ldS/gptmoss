---
name: backend-api
description: Implement local backend services, APIs, CLIs, job orchestration, file workflows, validation, and structured errors.
allowed_capabilities: [filesystem, shell]
---
Expose complete user workflows through small stable service methods before adding transport. Validate identifiers, paths, state transitions, inputs, and outputs. Keep long-running jobs observable and repeatable. Prefer minimal offline-compatible dependencies. Add smoke tests that invoke the public interface and verify real artifacts, error responses, and cleanup behavior.
