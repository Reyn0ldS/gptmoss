---
name: computer-vision-ml
description: Build and validate image ingestion, computer vision, reconstruction, inference, and pretrained-model adapter pipelines.
allowed_capabilities: [filesystem, shell]
---
Validate image type, dimensions, decoding, paths, and resource limits. Define explicit preprocessing and output contracts. Keep pretrained inference behind an adapter with checkpoint and device validation. If weights are unavailable, provide a deterministic geometric baseline that is clearly labeled and never random output advertised as reconstruction. Test malformed media, missing checkpoints, deterministic behavior, and numerical invariants.
