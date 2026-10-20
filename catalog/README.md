# The recipe catalog

`index.json` lists every recipe in [`twinspark/cookbook/recipes`](../twinspark/cookbook/recipes) with the
SHA-256 and size of its files. TwinSpark 0.7 and later read it from this repository's `main` branch, so a new
or corrected recipe reaches every installation without a TwinSpark update:

```
https://raw.githubusercontent.com/ChopCookies/Twinspark-Manager/main/catalog/index.json
```

What an installation does with it is described in [docs/cookbook.md](../docs/cookbook.md#the-recipe-catalog).
In short: files are only used when their SHA-256 matches the index, a recipe that needs a newer TwinSpark
(`min_twinspark`) is listed but not offered, and profiles imported from a recipe are never changed by it — a
newer recipe shows up under *Recipe updates* as a separate profile to review.

## Adding or changing a recipe

1. Add or edit the file in `twinspark/cookbook/recipes/` — a TwinSpark JSON draft, or an eugr YAML recipe
   with an optional `<name>.meta.json` (see [docs/recipe-workflow.md](../docs/recipe-workflow.md)). Modes
   (`"modes"`, `"default_mode"`) are described in [docs/cookbook.md](../docs/cookbook.md#modes).
2. Regenerate the index:

   ```bash
   python scripts/build_catalog.py
   ```

   It imports every recipe in each of its modes and refuses one that does not, so a broken recipe is caught
   here and not on someone's node.
3. Commit the recipe and `catalog/index.json` together. CI runs `python scripts/build_catalog.py --check`
   and fails when the index does not match the recipes.

A recipe that uses something only a newer TwinSpark understands says so in its `source`
(`"min_twinspark": "0.8.0"`; for a YAML recipe in its `.meta.json`). Older installations then list it with
"needs TwinSpark 0.8.0" instead of failing on it.

## Format

```json
{
  "format": "twinspark-catalog/1",
  "twinspark_version": "0.7.0",
  "recipes": [
    {
      "name": "glm-5.3-flash-nvfp4-tp2",
      "title": "…", "model": "…", "topology": "tp2", "engine": "vllm", "verification": "verified",
      "modes": ["latency", "throughput"], "default_mode": "latency",
      "min_twinspark": "0.7.0",
      "files": [{"path": "twinspark/cookbook/recipes/glm-5.3-flash-nvfp4-tp2.json", "sha256": "…", "size": 5321}]
    }
  ]
}
```

`paths` are relative to the repository root. `twinspark_version` is the version of the repository the index
was generated in: an installation that is newer than that (an unreleased build) keeps its own recipes where
both have one.
