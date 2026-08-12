---
name: geometry-3d
description: Implement mesh topology, 3D transforms, coordinate systems, OBJ export, rigging, skinning, collision, and geometry tests.
allowed_capabilities: [filesystem, shell]
auto-select: false
---
Declare units, handedness, axes, origin, topology, UV, normals, and transform conventions. Validate finite vertices, triangle indices, non-degenerate faces, scale, and stable export. Prefer deterministic standard-library geometry when dependencies are constrained. For fitting and rigging, preserve canonical topology and define deformation parameters explicitly. Tests must verify invariants and repeatability, not only object types.
