# Field notes and research

These pages are background material, not user documentation. They come from the maintainer's own two
DGX Sparks and from desk research while TwinSpark was being built. They are kept because they explain
where the defaults and the built-in recipes come from. Machine-specific values in them (paths, image
IDs, addresses) are examples; the [README](../../README.md) and [docs/](..) describe how to use the
current version.

| Note | What it is |
|---|---|
| [small-model-validation-2026-09-28.md](small-model-validation-2026-09-28.md) | First real-hardware run (SmolLM2-135M, single node and PP2), and how to repeat it with `scripts/dev_cluster.py` |
| [deepseek-v4-flash-two-node-startup.md](deepseek-v4-flash-two-node-startup.md) | A working DeepSeek V4 Flash launch with eugr's launcher, the reference for the built-in recipe |
| [researched-profiles.md](researched-profiles.md) | Desk research behind the built-in profiles: memory budgets, quantisations, topologies |
| [glm-mtp-research.md](glm-mtp-research.md) | GLM models and speculative decoding on two Sparks |
| [review-0.2-alpha.de.md](review-0.2-alpha.de.md) | Review of the 0.2 alpha (German): what was broken and how it was fixed |
| [examples/](examples/) | Profiles exported from the validation run; they pin a local image ID, so pin them again before use |
