#!/usr/bin/env python3
"""Capture the existing two-Spark deployment without copying weights or stopping it."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path('/home/chopc/spark-vllm-docker')
PEER = 'chopc@192.168.100.2'


def run(*args: str) -> str:
    return subprocess.check_output(args, text=True, timeout=60)


def redact(value):
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return re.sub(r'(?im)^([^=\n]*(?:TOKEN|PASSWORD|SECRET|API_KEY)[^=\n]*=).*$',
                      r'\1<redacted>', value)
    return value


def main():
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    dest = ROOT / 'backups' / f'deepseek-v4-flash-0731-{stamp}'
    dest.mkdir(parents=True, exist_ok=False, mode=0o700)
    for node, prefix in [('A', []), ('B', ['ssh', '-o', 'BatchMode=yes',
                                         '-o', 'ConnectTimeout=10', PEER])]:
        inspect = json.loads(run(*prefix, 'docker', 'inspect', 'vllm_node'))
        (dest / f'node-{node}-container.json').write_text(json.dumps(redact(inspect), indent=2) + '\n')
        script = run(*prefix, 'docker', 'exec', 'vllm_node', 'cat', '/workspace/exec-script.sh')
        (dest / f'node-{node}-exec-script.sh').write_text(script)
        image_id = inspect[0]['Image']
        image = json.loads(run(*prefix, 'docker', 'image', 'inspect', image_id))
        (dest / f'node-{node}-image.json').write_text(json.dumps(redact(image), indent=2) + '\n')
        processes = run(*prefix, 'docker', 'top', 'vllm_node', '-eo', 'pid,ppid,args')
        (dest / f'node-{node}-processes.txt').write_text(processes)
    (dest / 'api-models.json').write_text(run('curl', '-fsS', '--max-time', '10',
                                             'http://127.0.0.1:8000/v1/models'))
    (dest / 'source-commit.txt').write_text(run('git', '-C', str(SOURCE), 'rev-parse', 'HEAD'))
    (dest / 'source-status.txt').write_text(run('git', '-C', str(SOURCE), 'status', '--short'))
    # Source files and mods are tiny; exclude git history, wheels, and caches.
    with tarfile.open(dest / 'spark-launcher.tar.gz', 'w:gz') as archive:
        for path in sorted(SOURCE.rglob('*')):
            rel = path.relative_to(SOURCE)
            if any(p in {'.git', 'wheels', '__pycache__', '.pytest_cache'} for p in rel.parts):
                continue
            if path.is_file():
                archive.add(path, arcname=str(rel), recursive=False)
    recipe = SOURCE / 'recipes/deepseek-v4-flash-0731.yaml'
    (dest / recipe.name).write_bytes(recipe.read_bytes())
    (dest / 'cluster.env').write_text(redact((SOURCE / '.env').read_text()))
    entries = [f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}'
               for p in sorted(dest.iterdir()) if p.is_file()]
    (dest / 'SHA256SUMS').write_text('\n'.join(entries) + '\n')
    print(dest)
    print(f'Backup size: {sum(p.stat().st_size for p in dest.iterdir()) / 1024:.1f} KiB')


if __name__ == '__main__':
    main()
