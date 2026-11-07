# Kits: other people's set-ups, run by their own scripts

Some two-Spark set-ups are faster than anything a recipe can describe. They come with their own engine and
launcher, and their authors update them almost every day. [Mia's AI Lab](https://mia-ai.net/models?for=spark%3A2)
runs DeepSeek V4.1 Flash and Qwen3.8 Flash Next on TensorFold, a separate engine written in Zig.

A **kit** runs such a set-up exactly as its author publishes it. TwinSpark steps aside and keeps your
endpoint:

1. `sudo tsm kit start <kit>` stops TwinSpark's model on both Sparks.
2. It runs the kit's own `start.sh` on node A, as you.
3. TwinSpark's gateway forwards your requests to the kit's server.

Your apps keep TwinSpark's address, keys and model names. A switch to a TwinSpark profile stops the kit first.

## The kits

| Kit | Model and engine | One request (source) | Free memory at start | Disk |
|---|---|---|---|---|
| `mia-deepseek-v4.1-flash-1017` | DeepSeek V4.1 Flash 1017, EXL3 2.9 bpw on TensorFold | 80.7 tok/s (code 118) | 112 GiB per Spark | ~530 GB on A, ~310 GB on B |
| `mia-qwen3.8-flash-next-tensorfold` | Qwen3.8 Flash Next NVFP4 on TensorFold, MTP drafts | 67.4 tok/s (390 at 16 requests) | ~102 GiB per Spark (1M window) | ~150 GB each |

`tsm kit` lists them. The numbers are the authors' own measurements. TwinSpark has not measured them.

## Using one

On node A, as your own user:

```bash
tsm kit get mia-qwen3.8-flash-next-tensorfold        # clones the author's repository to ~/twinspark-kits/<kit>
cd ~/twinspark-kits/mia-qwen3.8-flash-next-tensorfold
cp scripts/local.sh.example scripts/local.sh         # set WORKER=<you>@<node B's address>
tsm kit prepare mia-qwen3.8-flash-next-tensorfold    # once: image and checkpoint; TwinSpark's model keeps serving
sudo tsm kit start mia-qwen3.8-flash-next-tensorfold
```

`tsm kit get` lists what the kit needs. The common items are:

- key-based ssh from node A to node B;
- your user in the `docker` group on both;
- free disk;
- for DeepSeek, an NFS export of node A's Hugging Face cache to node B. The steps are in the kit's
  `docs/requirements.md`.

Apps then use the kit through TwinSpark's endpoint. The model name is `default`, or `qwen` / `deepseek`;
`--alias` chooses others. The dashboard shows the kit and its state.

```bash
tsm kit status                                       # which kit serves, on which aliases
tsm kit update mia-qwen3.8-flash-next-tensorfold     # git pull: the author's newest version
sudo tsm kit stop mia-qwen3.8-flash-next-tensorfold  # the kit's stop.sh; nothing serves afterwards
tsm activate <profile>                               # back to a TwinSpark model (stops a running kit first)
```

**Stop** in the GUI, `tsm stop` and a switch to a profile all stop the kit's containers on both Sparks.

## What TwinSpark does, and what it does not

- **The kit's scripts run as you, never as root.** With `sudo tsm kit start`, they run as the user who ran
  `sudo`, with your ssh key and your Docker. The checkout must be yours and not writable by every user.
- **The controller never runs a kit's code.** It stops TwinSpark's own model, routes, watches the kit's
  `/health`, and stops the kit's containers by name.
- **The kit's server listens on `127.0.0.1` only.** `tsm kit start` sets `HOST=127.0.0.1` and `PORT` for it.
  Your apps reach it only through the gateway, with TwinSpark's keys. The gateway never sends TwinSpark's
  backend key to it.
- **The kit checks its own memory.** For example, the DeepSeek kit refuses to start below 112 GiB available on
  either Spark. If it does not start, `tsm kit start` runs the kit's `stop.sh` and releases the Sparks.
- **The watchdog only reports.** While the kit does not answer, its aliases answer 503 and the dashboard says
  why. TwinSpark does not restart it: `sudo tsm kit start <kit>` again, or switch to a profile.
- **What you see.** While a kit serves, TwinSpark's own serving metrics stay empty. The kit's logs are the
  container's: `docker logs -f <container>` on each Spark (`tsm kit` names the container).
- **Model files.** A kit downloads into its own Hugging Face cache (by default `~/.cache/huggingface` of your
  user), not TwinSpark's (`/var/lib/twinspark/hf-cache`). The same checkpoint used by both takes disk twice.
- **The first start is long.** The image, the checkpoint (and for DeepSeek, ~200 GB of tables and the weight
  files) come first. `tsm kit prepare` does that beforehand while TwinSpark's model keeps serving. DeepSeek's
  preparation writes two ~106 GB files and competes for memory with a large loaded model, so stop the model
  for that part.

## Adding a kit

A kit is one JSON file in `twinspark/cookbook/kits/`. It names:

- the GitHub repository;
- the container name, the same on both Sparks;
- the port;
- the aliases;
- the scripts, relative to the checkout;
- what it needs and what its author measured.

Scripts outside the checkout, other hosts than GitHub and container names Docker would not accept are refused
when the catalog loads.
