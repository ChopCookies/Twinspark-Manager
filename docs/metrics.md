# Metrics: Prometheus, Grafana and the 24-hour history

Node A's controller already scrapes the engine and polls both nodes for the dashboard. It exports the same
data for Prometheus at `GET /metrics` on its management API. Prometheus and Grafana run on your workstation;
nothing extra runs on the Sparks.

## Quick start (workstation)

```bash
sudo tsm prometheus key            # on node A: the read-only scrape key (created on first use)
```

On your computer, with the TwinSpark client connected (it forwards node A's management API to
`127.0.0.1:8443` by default):

```bash
cd deploy/observability
printf '%s' 'PASTE-THE-KEY' > twinspark.key
docker compose up -d               # Prometheus on 127.0.0.1:9090, Grafana on 127.0.0.1:3000 (admin / admin)
```

The *TwinSpark* dashboard is provisioned in Grafana. Prometheus keeps 90 days
(`--storage.tsdb.retention.time` in `docker-compose.yml`).

The compose file uses `network_mode: host` so the containers reach the client's `127.0.0.1` forward
(Linux; Docker Desktop 4.34+ with host networking turned on). Otherwise remove both `network_mode` lines,
publish `9090` and `3000`, and set the target in `prometheus.yml` to `host.docker.internal:8443`. If the
client picked another local port, `tsm client ports` shows it.

An existing Prometheus needs one scrape job:

```bash
tsm prometheus config --target 127.0.0.1:8443 --key-file /etc/prometheus/twinspark.key
```

## Alerts

`deploy/observability/alerts.yml` holds alert rules for these metrics. The compose setup loads them, and
firing alerts show on `http://127.0.0.1:9090/alerts`. For notifications, add an Alertmanager. An existing
Prometheus takes the file with `rule_files:`.

| Alert | When |
|---|---|
| TwinSparkControllerUnreachable | node A's management API cannot be scraped for 2 min |
| TwinSparkNodeDown | a node's agent did not answer the telemetry poll for 2 min |
| TwinSparkRouteDown / TwinSparkHalfDown | an alias (or a split half) does not serve for 5 min, not during a switch or an update |
| TwinSparkGatewayErrors | more than 5 % of an alias's requests fail for 10 min |
| TwinSparkRequestStuck | a request has been in flight for more than 15 min |
| TwinSparkEngineMetricsStale | the engine stopped reporting metrics for 5 min |
| TwinSparkMemoryLow / TwinSparkMemoryPressure / TwinSparkSwapInUse | less than 5 % memory available, PSI full above 10 %, or more than 1 GiB swap |
| TwinSparkGpuHot | GPU above 85 °C for 10 min |
| TwinSparkVersionsDiffer | the nodes run different TwinSpark versions for an hour (`tsm update`) |

The thresholds are starting points; change them to suit your recipes. CI checks the file with `promtool check
rules` and runs the unit tests in `alerts-test.yml` (`promtool test rules`).

## The key

The metrics key opens `GET /metrics` and nothing else; the management key also works but should not be
given to a monitoring system. `sudo tsm prometheus key --rotate` replaces it (the old one stops working at
once). With `management_auth: none` the endpoint, like the rest of the API, only answers loopback names.

## What is exported

| Family | Labels | What |
|---|---|---|
| `twinspark:generation_tokens_total`, `twinspark:prompt_tokens_total` | alias, node, model, revision, engine | tokens, from the engine's counters |
| `twinspark:requests_running`, `twinspark:requests_waiting` | same | the engine's queue |
| `twinspark:kv_cache_usage_ratio` | same | share of the KV cache in use (0-1) |
| `twinspark:prefix_cache_hits_total`, `…_queries_total` | same | prefix cache (hit rate = rate of hits / rate of queries) |
| `twinspark:time_to_first_token_seconds`, `…:inter_token_latency_seconds`, `…:e2e_request_latency_seconds`, `…:request_queue_time_seconds`, `…:request_prefill_time_seconds`, `…:request_decode_time_seconds` | same, plus `le` | the engine's **histograms**, buckets passed through: `histogram_quantile` works |
| `twinspark:requests_finished_total` | same, plus `finished_reason` | finished requests |
| `twinspark:spec_decode_*_total`, `twinspark:preemptions_total` | same | speculative decoding, preemptions |
| `twinspark_node_memory_available_bytes`, `…_total_bytes`, `…_free_bytes`, `twinspark_node_page_cache_bytes`, `twinspark_node_swap_used_bytes` | node | memory (shared CPU/GPU) |
| `twinspark_node_memory_pressure_avg10_percent` | node, kind (some/full) | PSI memory stall |
| `twinspark_node_gpu_power_watts`, `…_gpu_utilization_percent`, `…_gpu_temperature_celsius`, `twinspark_node_cpu_utilization_percent`, `twinspark_node_up` | node | GPU sensor (not the wall plug), CPU, agent reachable |
| `twinspark_gateway_route_up`, `…_inflight_requests`, `…_oldest_inflight_seconds`, `…_requests_total`, `…_errors_total` | alias | the gateway |
| `twinspark_active_info`, `twinspark_switch_in_progress`, `twinspark_half_serving` | profile, revision, node | what is deployed, and whether a switch is running |
| `twinspark_window_decode_tokens_per_second`, `…_prefill_…`, `…_requests_per_second`, `twinspark_window_ttft_seconds`, `twinspark_window_itl_seconds` | scope (deployment / half), node, quantile | the GUI's numbers over the controller's last scrape interval |

`twinspark:*` are the engine's families under TwinSpark names, labelled with the gateway alias, the node
that serves them, the served model name (vLLM's `model_name` becomes `model`) and the revision. In a split
deployment each half has its own alias, node and revision. A kept half carries the revision that started
its container. `twinspark_*` are TwinSpark's own.

**Windowed or since start.** Counters and histograms are cumulative, as the engine reports them; Prometheus
computes rates and percentiles over any window. `twinspark_window_*` are what the GUI shows: rates over the
controller's last interval (10 s by default) and latency percentiles from the histogram buckets that changed
in that interval. The dashboard's *TTFT p95 (last interval)* is that value, not an average since the engine
started.

Example queries:

```promql
sum by (alias) (rate(twinspark:generation_tokens_total[1m]))                                 # decode tok/s
histogram_quantile(0.95, sum by (le, alias) (rate(twinspark:time_to_first_token_seconds_bucket[5m])))
sum by (alias) (rate(twinspark:prefix_cache_hits_total[5m])) / sum by (alias) (rate(twinspark:prefix_cache_queries_total[5m]))
min_over_time(twinspark_node_memory_available_bytes[1h])                                    # lowest MemAvailable
```

The values come from the controller's last scrape (every `metrics_interval_s`); `twinspark_scrape_age_seconds`
says how old they are. During a switch the engine is not scraped, except the half of a split that keeps
serving while the other half switches.

## History without Prometheus

The controller keeps minute averages of the dashboard's numbers and each node's memory, swap, memory pressure
and GPU power for 24 hours, in its database (written every 10 minutes and at shutdown), so the history
survives a restart. The System page shows it; `GET /api/v1/system/metrics/history?hours=24` returns it.
