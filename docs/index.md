# TwinSpark Manager

Run, switch and measure large language models on **two NVIDIA DGX Sparks** — one OpenAI-compatible endpoint
for your apps, with memory planning for unified memory, safe model switching and benchmarks you can compare.

- **Install and connect:** the [README](../README.md) has the 10-minute install; the
  [workstation client](client.md) puts the GUI on your own computer.
- **Pick a model:** the [Cookbook](cookbook.md) has recipes that worked on two Sparks, with modes and a
  catalog that brings new recipes without an update; the [performance lab](perf-lab.md) lists what they
  measured.
- **Use it from your apps:** [Open WebUI, n8n, LiteLLM, Continue, Cherry Studio, Aider, OpenCode](integrations/README.md),
  one key per app ([gateway](gateway.md)).
- **Measure:** [benchmarks](benchmarks.md) per revision, [validation records](validation.md),
  [Prometheus metrics](metrics.md).
- **Operate:** [updating TwinSpark](updating.md), [maintenance](maintenance.md), [remote management](remote-management.md) for headless nodes,
  the [QSFP link](qsfp-link.md).

TwinSpark is MIT-licensed and alpha software; [the security model](security.md) says what it protects and
what it does not.
