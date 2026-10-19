"""``tsm prometheus``: the Prometheus export of node A (docs/metrics.md).

``tsm prometheus key`` prints the read-only scrape key (created on first use, ``--rotate`` replaces it);
it opens nothing but ``GET /metrics``. ``tsm prometheus config`` prints a scrape job for prometheus.yml.
Both run on node A and read its vault (root).
"""

from __future__ import annotations

import sys
import textwrap


def cmd_metrics_key(args, api=None) -> None:
    from .security import SecretsVault
    vault = SecretsVault(args.secrets_dir)
    try:
        if args.rotate:
            value, created = vault.rotate("metrics_api_key"), True
        else:
            value, created = vault.ensure("metrics_api_key")
    except PermissionError:
        sys.exit(f"cannot write {vault.dir}: run this on node A with sudo (sudo tsm prometheus key)")
    if created:
        print("# a new metrics key (read-only: GET /metrics only); the previous one, if any, stops working now"
              if args.rotate else "# created the metrics key (read-only: GET /metrics only)", file=sys.stderr)
    print(value)


def scrape_job(target: str, scheme: str, key_file: str, insecure: bool) -> str:
    tls = "\n    tls_config:\n      insecure_skip_verify: true   # node A's own certificate" if insecure else ""
    return textwrap.dedent(f"""\
        scrape_configs:
          - job_name: twinspark
            scrape_interval: 15s
            scheme: {scheme}
            metrics_path: /metrics
            authorization:
              type: Bearer
              credentials_file: {key_file}   # the output of `sudo tsm prometheus key` on node A{{tls}}
            static_configs:
              - targets: ["{target}"]
        """).replace("{tls}", tls)


def cmd_metrics_prometheus(args, api=None) -> None:
    print(scrape_job(args.target, args.scheme, args.key_file, args.insecure), end="")


cmd_metrics_key.local = True            # type: ignore[attr-defined]
cmd_metrics_prometheus.local = True     # type: ignore[attr-defined]


def add_parsers(sub, cmd) -> None:
    m = sub.add_parser("prometheus", help="Prometheus export of the cluster (/metrics): scrape key and config")
    ms = m.add_subparsers(dest="prometheus_cmd", metavar="<action>", required=True)
    s = ms.add_parser("key", help="print the read-only scrape key (created on first use)")
    s.set_defaults(fn=cmd_metrics_key)
    s.add_argument("--rotate", action="store_true", help="replace it (the old key stops working)")
    s = ms.add_parser("config", help="print a scrape job for prometheus.yml")
    s.set_defaults(fn=cmd_metrics_prometheus)
    s.add_argument("--target", default="127.0.0.1:8443",
                   help="node A's management API as Prometheus reaches it (default: the client's SSH forward)")
    s.add_argument("--scheme", choices=["http", "https"], default="http")
    s.add_argument("--key-file", default="/etc/prometheus/twinspark.key")
    s.add_argument("--insecure", action="store_true", help="https with node A's self-signed certificate")
