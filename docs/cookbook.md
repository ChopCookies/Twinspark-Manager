# The Cookbook: recipe modes, the recipe catalog, community results, the playground

The Cookbook is where a profile starts: a recipe that worked on two DGX Sparks, imported as a profile you can
change, pin and switch to ([docs/recipe-workflow.md](recipe-workflow.md) covers importing and combining
recipes). This page covers what 0.7 added around it.

## Modes

Some recipes come in more than one variant. The GLM-5.3-Flash recipes, for example, have

- **latency** (the default): the speculative drafter on, a few sequences at a time — the fastest single answer;
- **throughput**: no drafter, more sequences and a larger batch — the most tokens per second across many
  requests at once.

Each mode carries its own measured numbers; a mode nobody has measured yet shows none instead of another
mode's. Importing asks which mode to use (the Cookbook's *Import*, or `--mode`):

```bash
tsm cookbook show glm-5.3-flash-nvfp4-tp2                           # the modes and what each measured
tsm cookbook import glm-5.3-flash-nvfp4-tp2 --mode throughput --as glm-batch
```

Each mode becomes a profile of its own; importing both gives two profiles you can switch between and
benchmark against each other (*Benchmarks → Compare*, [docs/benchmarks.md](benchmarks.md)). The mode a profile came from is kept in it (`source.mode`), so *Recipe
updates* compares it with the same mode of a newer recipe.

**Writing a mode.** A recipe (a TwinSpark JSON recipe, or the `.meta.json` of an eugr YAML recipe) lists its
modes next to the draft:

```json
"default_mode": "latency",
"modes": {
  "latency":    {"title": "Low latency", "description": "…", "patch": {}, "measured": {"single_stream_tok_s": 26.5}},
  "throughput": {"title": "Throughput",  "description": "…",
                 "patch": {"advanced": {"speculative_config": null, "max_num_seqs": 8}}, "measured": {}}
}
```

`patch` is merged into the recipe's settings: a value replaces, an object merges, `null` removes. It may
change `simple`, `behaviour`, `advanced`, `engine`, `image_hint`, `description` and `tags` — never the model's
identity. `scripts/build_catalog.py` imports every mode, so a mode that does not build is refused before it
is published.

## The recipe catalog

New and corrected recipes reach every installation without a TwinSpark update. The recipes in
`twinspark/cookbook/recipes` ship with each release; the same folder on this repository's `main` branch is
published as [a catalog](../catalog/README.md) (`catalog/index.json`: every file with its SHA-256 and size).
Node A and the workstation client read it:

- **When.** Nothing is fetched while nobody looks: opening the Cookbook refreshes a copy older than
  `cookbook.catalog_refresh_hours` (24 h) in the background; *Check for new recipes* or
  `tsm cookbook refresh` fetch it now and say what changed.
- **What is checked.** Only https (redirects only within the same host), at most 1 MiB for the index and
  512 KiB for a recipe; a file is only used when its SHA-256 matches the index — at download, and again
  whenever a cached file changes. A refresh that fails leaves the previous copy in use, and nothing
  half-downloaded is ever read. With `catalog_url` switched off or changed, the old copy is not used.
- **What it changes.** The Cookbook marks recipes *new in catalog* or *updated in catalog*. A recipe that
  needs a newer TwinSpark (`min_twinspark`) is listed with that note and not offered. **Profiles you
  imported are never changed**: a newer version of their recipe shows under *Recipe updates* as an update to
  review, which creates a separate profile (`<name>-update`) and keeps your own changes.
- **What it does not protect against.** The SHA-256 guards against damaged, partial or changed downloads and
  a changed cache; the index itself is trusted as far as https to github.com is. A recipe is data, not code,
  but it names a container image and, possibly, mods — review it like any recipe before you switch to it.

```bash
tsm cookbook refresh          # fetch now: "2 recipes — 1 added: …; 1 updated: …"
tsm cookbook list             # NEW / UPD marks; the catalog's state at the end
```

```yaml
# /etc/twinspark/controller.yaml
cookbook:
  catalog_url: https://raw.githubusercontent.com/ChopCookies/Twinspark-Manager/main/catalog/index.json
  catalog_refresh_hours: 24     # 0: only on `tsm cookbook refresh`
  # catalog_url: ""             # off (air-gapped): recipes come only with TwinSpark updates
```

The workstation client uses node A's settings and keeps its own copy in its folder. An unreleased build that
is newer than the catalog keeps its own recipes where both have one.

## NVIDIA's recipes

Since November 2026 NVIDIA publishes its own DGX Spark recipes:
[NVIDIA/local-ai-inference-recipes](https://github.com/NVIDIA/local-ai-inference-recipes), one `recipe.yaml`
per model, machine and engine, each validated and measured on NVIDIA's own Sparks. **Cookbook → Community**
lists the ones for two Sparks out of the box, next to eugr's:

```yaml
# the default; set recipe_sources in /etc/twinspark/controller.yaml only to change it
recipe_sources:
  - eugr/spark-vllm-docker:recipes
  - NVIDIA/local-ai-inference-recipes:models/*/dgx-spark-2gpu/linux/vllm/*recipe.yaml
  # NVIDIA's single-Spark recipes too (they import as single-a profiles):
  # - NVIDIA/local-ai-inference-recipes:models/*/dgx-spark/linux/vllm/*recipe.yaml
```

A source path with `*` is a pattern over the whole repository: TwinSpark reads the repository's file list
with one GitHub request and shows every file that matches (`*` also matches `/`). Add `@main` or a commit to
list a fixed state. If you set `recipe_sources` yourself, the list replaces the default: add NVIDIA's line to
yours to see its recipes.

You can also paste a recipe's GitHub link into **Import recipe** (or `tsm cookbook import-recipe <url>`). An NVIDIA
recipe is a complete launch written for people: the `docker run` for every node and the `vllm serve` of each
rank. TwinSpark starts the containers and wires the nodes itself, so the import takes:

- the image (`runtime.image`; Pin resolves its digest) and the checkpoint;
- the variables the `vllm serve` command exports, and the tuning variables of the `docker run`
  (`NCCL_BUFFSIZE`, `VLLM_ENABLE_ROCE_ALLREDUCE`, …);
- every vLLM flag except the per-node ones (`--nnodes`, `--node-rank`, `--master-addr`, `--headless`, host,
  port), which TwinSpark sets for each rank;
- a drafter named in `--speculative-config` as a second checkpoint: it is staged on both nodes and pinned
  with the model.

The network wiring of the `docker run` (interfaces, RoCE devices, head address) and the Hugging Face token are
left out: they come from `controller.yaml` and the node's vault. The import report lists both. Recipes for four
or eight Sparks, llama.cpp recipes and commands that need a shell (`--flag "$VAR"`) are refused with the reason.

NVIDIA's numbers come along as notes, not as measurements of yours: they were taken with NVIDIA's settings on
NVIDIA's Sparks. Several recipes use eugr's community images (`eugr/spark-vllm-b12x:…`), which NVIDIA marks as
mutable; Pin fixes the digest you test.

The Cookbook has one of them built in, with TwinSpark's notes:
`qwen3.8-27b-nvfp4-dflash2-tp2` (Qwen3.8-27B with the DFlash2 drafter on the stock vLLM 0.31.0 image).
[notes/nvidia-recipes.md](notes/nvidia-recipes.md) compares the others with the built-in recipes.

## Browse TwinSpark Community

To opt in to browsing the public recipe registry, add its HTTPS listing to
`recipe_sources` in `/etc/twinspark/controller.yaml` on Spark A (keep any other sources you use):

```yaml
recipe_sources:
  - eugr/spark-vllm-docker:recipes
  - https://studio-tools.info/api/v1/recipes
```

Restart the controller and reconnect the workstation client to pick up the change. In
**Cookbook → Community**, choose **Preview & import**. TwinSpark reads up to five pages and
checks the numbered recipe download against the registry's SHA-256 before showing its preview.
The import uses that reviewed draft; browsing and preview do not start a model or send reports.
Checksums detect mismatched content, not whether a recipe is safe or tested on your hardware.
Self-hosted registries must serve their numbered downloads from the same HTTPS API base.

Paste a recipe's **latest** URL manually if you want to follow new versions with the recipe-update
workflow. Numbered URLs stay fixed. `cookbook.community_url` below is a separate opt-in reporting
endpoint, not the recipe registry.

## Community results

How often does a recipe work, how fast is it on other people's Sparks, and what did they change? Results
people choose to share answer that in the Cookbook: *12 reports · 83 % work · median 812 tok/s · often
changed: max_num_seqs = 12 (4×)*.

Sharing is **opt-in and previewed**. It is off until `cookbook.community_url` points at a community server
(the PHP endpoint in [`community/strato`](../community/strato/README.md)), and nothing is sent without you
seeing the exact document first:

```bash
tsm cookbook report glm-batch                       # how the last switch went; shows the document, asks
tsm cookbook report glm-batch --outcome works-with-changes --comment "max_num_seqs 12 is the sweet spot"
tsm cookbook report glm-batch --anonymous           # without this installation's random id
tsm cookbook stats                                  # the totals per recipe
```

In the GUI: a profile imported from the Cookbook has *Share result…*; the Cookbook's cards show the totals.

**What a report contains** — built from an allowlist, never by stripping a full record:

| Field | |
|---|---|
| `recipe`, `recipe_sha256`, `mode` | which recipe file, exactly, and which mode |
| `outcome`, `failure_stage` | `works`, `works-with-changes` or `fails`, and where (download, pull, start, health, …) |
| `twinspark`, `engine`, `engine_version`, `topology` | versions and the node arrangement |
| `benchmarks` | the newest valid result of each benchmark preset: tok/s, TTFT, concurrency — numbers only |
| `changes` | settings you changed against the recipe: known tuning fields only (context, batch sizes, memory, parsers, sampling) with numbers, booleans and short plain words; extra vLLM flags with numbers and booleans only |
| `comment` | what you typed (500 characters); refused if it contains an address or a home path |
| `reporter` | a random id made for this installation (next to node A's state database, `community-reporter`) so a later report replaces the earlier one; left out with `--anonymous` |

**What it never contains:** host names, IP addresses, user names, paths, model repositories or names you
chose, aliases, environment variables, raw vLLM arguments, image names, keys or tokens. Both ends check
this: node A refuses to send a document with any other field or value, and the server refuses to store one.
The server keeps only a keyed hash of the reporter id and, for its daily rate limit, of the address
(deleted after two days). A dry run is not a result and cannot be reported.

## The playground

**Playground** in the GUI chats with what is being served — one alias, or two side by side with the same
question — streamed through the gateway as an app would see it, with **time to first token**, **decode
tokens per second** and the token counts under each answer. *Copy as curl* gives the same request for an app,
with `$TWINSPARK_API_KEY` where an app key goes (see [Keys & usage](gateway.md#one-key-per-app)).

It uses the management session you are signed in with, not an app key: requests go to
`/api/v1/playground/v1/chat/completions` on node A (`/v1/completions` and `/v1/models` too, nothing else),
which checks the management key, replaces it with TwinSpark's own internal token and passes the stream
through unchanged. The management key never reaches a backend, and playground requests are not counted in
any app's usage. With `gateway_mode: standalone` the playground is served from the controller's process, so
a switch does not wait for a playground answer.
