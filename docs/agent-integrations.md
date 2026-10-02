# Agent clients and evaluation harnesses

TwinSpark can generate connection settings for agent clients and an evaluation
harness. The clients run on your workstation or another host and send inference
requests to TwinSpark's gateway. TwinSpark continues to manage the model recipes,
images, weights and deployment on the Spark nodes.

Use the gateway's stable model aliases, such as `default` and `secondary`, as the
client's model IDs. One distributed model can use one alias across both nodes;
two independent split models can use different aliases at the same gateway URL.
A profile switch changes the model behind an alias without requiring a new
client model ID or gateway URL. Regenerate context-dependent settings if the new
model's limits differ.

Generated settings are connection templates. They do not install clients, start
agents, activate profiles or grant an agent access to TwinSpark's management API.
Keep the inference API key separate from the management API key. Exports refer to
`TWINSPARK_API_KEY` or explain the client's expected environment variable; they
never include either stored key.

Open **Integrations** in the web client, select a model connection and client,
then choose **Create connection files**. Copy or download the generated files
on the machine where the client runs. Select a second alias for clients with
separate planning, coding or helper models. **Run compatibility check** creates
a job for the selected primary alias; check the other alias separately when needed.

## Available exports

| Client | Connection format | Model requirements |
| --- | --- | --- |
| OpenCode stable | Custom Chat Completions provider in `opencode.json` | Streaming and reliable tool calling for agent use; known context limit |
| Aider | OpenAI-compatible model in `.aider.conf.yml` | Chat generation; native tool calling is not required for its textual edit workflow |
| LangGraph / LangChain | Python example using `ChatOpenAI` | Chat generation; tool calling when tools are attached |
| OpenClaw | Additive custom-provider JSON fragment | Streaming and reliable tool calling for agent use; known context limit |
| Hermes | Custom endpoint in a YAML fragment | Tool calling and at least 64,000 tokens of actual context |
| lm-evaluation-harness | YAML configuration using `local-chat-completions` | Chat generation for generation-based evaluation tasks |

Exports can be prepared for planned aliases before a profile is activated. That
does not make an alias available for inference: compatibility checks require a
serving route. Context-dependent exports use the confirmed running limit or an
explicit recipe limit. An unresolved `auto` context cannot safely supply client
limits and must be resolved before those exports can be generated. Increasing a
number in a client configuration does not increase the model's deployed context.

The gateway forwards supported inference requests to the selected runtime. The
pinned vLLM image, model, parser and chat template determine whether tools,
structured output or Responses are usable. A configured capability and a passed
runtime check are different evidence.

### OpenCode stable

The export uses `provider`, `npm: "@ai-sdk/openai-compatible"` and a `baseURL`
ending in `/v1`. The inference key is referenced as `{env:TWINSPARK_API_KEY}`.
Model IDs in the provider catalog match gateway aliases; OpenCode selects them
as `twinspark/<alias>`. Its context and output limits describe the deployed
model. See the [official custom-provider configuration](https://opencode.ai/docs/providers/#custom-provider).

With two selected aliases, a client can use one model for implementation and
another for planning. Both still use the same gateway. OpenCode documents
per-agent model selection in its [agent configuration](https://opencode.ai/docs/agents/#model).

This export targets the stable documentation format. OpenCode's separate
[v2 provider documentation](https://opencode.ai/v2/docs/providers) uses a different
configuration shape; do not substitute this fragment into a v2 configuration.

### Aider

Aider uses the model ID `openai/<alias>` and an `openai-api-base` ending in `/v1`.
Provide the inference key through `OPENAI_API_KEY` in the client's environment.
The generated YAML does not embed a key or assume that shell variables expand
inside YAML values. A second alias can supply Aider's weak model.

See Aider's [OpenAI-compatible endpoint setup](https://aider.chat/docs/llms/openai-compat.html)
and [YAML configuration reference](https://aider.chat/docs/config/aider_conf.html).

### LangGraph / LangChain

The Python example uses `ChatOpenAI` with the selected alias, gateway base URL
and an environment-supplied inference key. It explicitly selects Chat Completions
with `use_responses_api=False` and disables optional streamed token-usage
requests with `stream_usage=False`. Add tools only after confirming that the
recipe supports the required tool-call workflow.

For a split deployment, different graph nodes can use different aliases: for
example, a planner on A and a coder or reviewer on B. The model route and recipe
stay under TwinSpark's control; the graph and its tools remain in the client.

See the [custom-provider guidance](https://docs.langchain.com/oss/python/langchain/models#base-url-and-proxy-settings),
[ChatOpenAI reference](https://docs.langchain.com/oss/python/integrations/chat/openai)
and [LangGraph quickstart](https://docs.langchain.com/oss/python/langgraph/quickstart).

### OpenClaw

The export adds a `twinspark` entry under `models.providers` and preserves the
existing provider catalog with `models.mode: "merge"`. Its
`api: "openai-completions"` adapter uses the Chat Completions endpoint. The
`baseUrl` ends in `/v1`, and the key uses `${TWINSPARK_API_KEY}`. Each model entry
contains the gateway alias and its actual context/output limits.

Merge the fragment into your existing OpenClaw settings. Do not replace unrelated
agent, channel or tool settings. Vision, reasoning and Responses continuation
should only be declared when the deployed recipe has been verified to support
them. See the [official custom-provider reference](https://docs.openclaw.ai/gateway/config-tools/custom-providers).

### Hermes

The export uses `model.provider: custom`, the alias as `model.default`, an
explicit `model.base_url` and `key_env: TWINSPARK_API_KEY`. Its `context_length`
comes from the actual model limit.

Hermes currently documents a minimum of 64,000 context tokens for agent use with
tools and rejects smaller windows at startup. TwinSpark must therefore flag a
smaller recipe as unsuitable for this export; it must not inflate the context
value to satisfy the client. Hermes documents the requirements and endpoint
setup in its [provider guide](https://hermes-agent.nousresearch.com/docs/integrations/providers).
Its [configuration reference](https://hermes-agent.nousresearch.com/docs/user-guide/configuration)
covers environment references and auxiliary-model routing.

### lm-evaluation-harness

The generated YAML selects `local-chat-completions`. Unlike the other clients,
its `base_url` includes the complete `/v1/chat/completions` path. The alias is the
model ID. Tokenization is left to the server, avoiding a download of a tokenizer
named after an alias such as `default`.

The starter configuration runs `gsm8k` with 20 examples, one concurrent request
and a bounded generation length. Supply the inference key through
`OPENAI_API_KEY`, then run:

```text
lm-eval run --config twinspark-eval.yaml
```

This is a smoke evaluation to check the connection and result workflow, not a
benchmark score suitable for publication. Remove the sample limit for a full
evaluation and record the exact recipe revision, task settings and harness
version. Dataset downloads and evaluation run on the client host.
The exported `twinspark-evaluation.json` manifest preserves the alias, profile,
revision and output directory alongside the harness configuration. Keep it with
your result files, especially if the model behind an alias later changes.

The chat adapter supports generation-based tasks and does not implement
loglikelihood. Tasks such as likelihood-based MMLU or HellaSwag require a
different adapter and verified prompt-logprob support. Do not select them under
the generated chat configuration. See the
[configuration schema](https://github.com/EleutherAI/lm-evaluation-harness/blob/main/docs/config_files.md),
[CLI reference](https://github.com/EleutherAI/lm-evaluation-harness/blob/main/docs/interface.md)
and [chat adapter implementation](https://github.com/EleutherAI/lm-evaluation-harness/blob/main/lm_eval/models/openai_completions.py).

## Compatibility checks

Checks run against an alias that is currently serving. They create a persistent
job so progress and failures remain visible in Jobs. They exercise small,
bounded inference requests:

| Check | What it establishes |
| --- | --- |
| `chat` | A normal Chat Completions request returns a usable response |
| `streaming` | A streamed chat response follows the expected event format |
| `tools` | The model can request a local echo tool and accept its result |
| `structured` | The runtime returns a response matching the requested small schema |
| `responses` | The optional Responses path accepts the tested request |

The echo tool is an internal test operation. Checks never execute arbitrary
model-requested commands, install tools or connect an agent to the management
API. A dry-run deployment records simulated results without making inference
requests. Neither simulated nor real compatibility jobs mark a recipe
known-good; recipe activation and its normal validation remain separate.

Automatic tool calling in vLLM requires the appropriate enable flag, a parser
suited to the selected model and a chat template that understands tool messages.
A successful basic chat request does not establish those requirements. See the
[vLLM tool-calling guide](https://docs.vllm.ai/en/stable/features/tool_calling/).

Responses is excluded from the default check set. A passing optional Responses
request does not establish the full response lifecycle and tool semantics needed
by every agent client. Codex is therefore not offered as a client export in this
release. Current Codex custom providers require the Responses protocol; see the
[official configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).

## Management API

These endpoints require the management API key, using the same authentication
as other `/api/v1` routes. Client inference requests use the inference key instead.

```http
GET /api/v1/integrations
x-api-key: <management key>
```

Generate settings for a selected client and alias with:

```http
GET /api/v1/integrations/export?client=<client>&alias=default&base_url=<encoded-gateway-url>
x-api-key: <management key>
```

The client IDs are `opencode`, `aider`, `langgraph`, `openclaw`, `hermes` and
`lm-eval`. The response contains a `files` list with each file's name, content and
language, together with setup notes and the selected revision ID.

`base_url` describes
the gateway address reachable from the client, for example
`http://spark-a:8000/v1`. URL-encode it when constructing the query manually.
Add `secondary_alias=<alias>` for OpenCode, Aider, LangGraph or OpenClaw to include
a second model. Hermes and LM Evaluation Harness use one selected alias per
export. If several planned profiles share an alias, give them different aliases
or activate the intended profile before exporting.

Start an explicit compatibility check with:

```http
POST /api/v1/integrations/check
Content-Type: application/json
x-api-key: <management key>

{
  "alias": "default",
  "checks": ["chat", "streaming", "tools", "structured"]
}
```

The response identifies the job. Read its progress with
`GET /api/v1/jobs/{job_id}` and cancel it with
`POST /api/v1/jobs/{job_id}/cancel`. The optional `responses` check must be
requested explicitly. Individual results report `pass`, `fail`, `unsupported`
or `simulated`. A completed job means its requested checks finished; inspect
`payload.compatible` and the individual results to determine compatibility.
Simulated jobs leave compatibility unconfirmed. Run checks again after switching
a recipe or changing its runtime, parser or context limit; evidence from an
earlier revision does not verify a new deployment.

## Importing evaluation results

In **Integrations**, choose the model connection, enter the exact recipe revision
in **Record evaluation results**, open the harness results JSON, and choose
**Record results**. The web client removes sample logs and client configuration
before sending the numeric report. Jobs displays the recorded metrics and their
evidence label.

Import the harness's results JSON to attach reported metrics to an exact saved
recipe revision:

```http
POST /api/v1/integrations/evaluations
Content-Type: application/json
x-api-key: <management key>

{
  "alias": "default",
  "revision_id": "<immutable recipe revision ID>",
  "results": {
    "results": {
      "gsm8k": { "exact_match,strict-match": 0.4 }
    },
    "config": {
      "model_args": { "model": "default" },
      "limit": 20
    }
  }
}
```

`results` is the harness's results document, not a sample-log file. The full
request must be no larger than 1 MiB. Select the immutable revision that was used
for the evaluation, including an older saved revision if the alias has since
switched. That revision must own the selected alias. When the document includes
`config.model_args.model`, it must match that alias.

TwinSpark records finite numeric metrics, the reported sample limit and recipe
attribution. It does not retain client configuration, keys, prompts or samples.
The response is HTTP 201 with a completed, persistent `evaluation` job. Its
payload identifies `source: "lm-evaluation-harness"`, `evidence: "reported"` and
`verified: false`, together with the metrics and a note explaining the evidence.

Importing a report does not run the harness, independently verify hardware
execution or certify the recipe as known-good. Recipe attribution is the
operator's report about the selected immutable revision. Treat a 20-sample result
as a smoke evaluation even after importing it.
