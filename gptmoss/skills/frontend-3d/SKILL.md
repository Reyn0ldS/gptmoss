---
name: frontend-3d
description: Build offline user interfaces and 3D viewers for upload, preview, configuration, progress, fitting, and export workflows.
allowed_capabilities: [filesystem, shell]
auto-select: false
---
Map every UI control to an implemented service workflow. Show progress, failures, limitations, and generated artifacts clearly. Avoid CDN dependencies for offline delivery. Keep accessibility and keyboard use in scope. A preview must handle valid exported geometry and reject bad files without claiming a rendering succeeded. Provide a repeatable local smoke path.
