#!/usr/bin/env python3
"""Prepare and exercise a bounded SmolLM deployment through the real manager.

Run from the repository: .venv/bin/python scripts/dev_cluster.py prepare|test|serve
Node B needs this source and the agent dependencies under /tmp/twinspark-manager-dev.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import shlex
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
import yaml

from twinspark.agent.actions import AgentActions
from twinspark.controller.agent_client import AgentClient
from twinspark.controller.controller import Controller
from twinspark.gateway.gateway import Gateway
from twinspark.resolver import resolve_hf_revision, spec_from_resolved
from twinspark.schemas.config import AgentConfig, ControllerConfig, load_config
from twinspark.schemas.profile import ProfileDraft
from twinspark.security import SecretsVault

WORK = Path('/tmp/twinspark-manager-dev')
PEER = 'chopc@192.168.100.2'
MODEL = 'HuggingFaceTB/SmolLM2-135M-Instruct'
REVISION = '12fd25f77366fa6b3b4b768ec3050bf629380bac'
IMAGE_ID = 'sha256:d43f15877df4176dfc70b7ebca336d5de698e1696da02fc3738b0338a65f5db2'


def write_json(path, obj):
    path.write_text(json.dumps(obj, indent=2) + '\n')


def prepare():
    WORK.mkdir(parents=True, exist_ok=True, mode=0o700)
    vault = SecretsVault(WORK / 'secrets')
    for name in ('agent_token', 'backend_api_key', 'management_api_key', 'inference_api_key'):
        if not vault.get(name):
            vault.set(name, secrets.token_urlsafe(32))
    runtime = dict(vllm_port=18100, master_port=29521, health_timeout_s=240,
                   drain_timeout_s=10, hf_cache_dir=str(WORK / 'hf'),
                   compile_cache_dir=str(WORK / 'compile'), max_download_gib=0.35,
                   min_disk_free_gib=5, allow_image_pull=False)
    common = dict(runtime=runtime, secrets_dir=str(WORK / 'secrets'))
    for node, bind in [('A', '127.0.0.1'), ('B', '192.168.100.2')]:
        doc = dict(common, node=dict(node_id=node, role='agent'),
                   listener=dict(bind=bind, port=19443), runtime_mode='docker')
        (WORK / f'agent-{node}.yaml').write_text(yaml.safe_dump(doc))
    cfg = dict(common, node=dict(node_id='A', role='controller'),
               db_path=str(WORK / 'state.db'), autostart=False,
               listener=dict(bind='127.0.0.1', port=18443),
               gateway_listener=dict(bind='127.0.0.1', port=18000),
               nodes={n: dict(agent_url=f'http://{ip}:19443', qsfp_ip=qsfp,
                              qsfp_iface='enp1s0f1np1', ssh_user='chopc')
                      for n, ip, qsfp in [('A', '127.0.0.1', '192.168.100.1'),
                                          ('B', '192.168.100.2', '192.168.100.2')]})
    (WORK / 'controller.yaml').write_text(yaml.safe_dump(cfg))
    resolved = resolve_hf_revision(f'{MODEL}@{REVISION}')
    write_json(WORK / 'model-info.json', resolved)
    for topology in ('single-a', 'single-b', 'pp2'):
        draft = ProfileDraft.model_validate(dict(
            name=f'smollm2-135m-{topology}', description='Small development checkpoint; eager, 1K context.',
            simple=dict(model=MODEL, quantization='bf16', topology=topology,
                        context_length=1024, concurrency=1, api_alias='dev'),
            advanced=dict(dtype='bfloat16', weight_loader='safetensors', eager_mode=True,
                          prefix_cache=False, gpu_memory_utilization=0.025,
                          max_num_batched_tokens=1024,
                          env={'NCCL_IB_HCA': 'rocep1s0f1', 'NCCL_IB_GID_INDEX': '3',
                               'NCCL_IB_DISABLE': '0', 'VLLM_USE_V2_MODEL_RUNNER': '0'},
                          extra_vllm_flags={'kv-cache-memory-bytes': 33554432}),
            identity=dict(model_repo=MODEL, model_revision=REVISION, quantization='bf16',
                          image='vllm-node-b12x', image_digest=IMAGE_ID, image_source='local',
                          vllm_version='0.1.dev19023+g30038602b.d20260805',
                          pytorch_version='2.12.0+cu130', cuda_version='13.0.2')))
        write_json(WORK / f'{draft.name}.json', draft.model_dump(mode='json'))
    print(f'Prepared {WORK}; weights {resolved["weight_bytes"] / 1024**2:.1f} MiB', flush=True)


def service_processes(include_controller=False):
    processes = []
    commands = [
        [sys.executable, '-m', 'twinspark.cli', 'serve', 'agent', '--config', str(WORK / 'agent-A.yaml')],
        ['ssh', '-tt', '-o', 'BatchMode=yes', PEER, 'exec ' + shlex.join([
            'env', f'PYTHONPATH={WORK / "source"}', str(WORK / 'venv/bin/python'),
            '-m', 'twinspark.cli', 'serve', 'agent', '--config', str(WORK / 'agent-B.yaml')])],
    ]
    if include_controller:
        commands.append([sys.executable, '-m', 'twinspark.cli', 'serve', 'controller',
                         '--config', str(WORK / 'controller.yaml')])
    for name, command in zip(('agent-A', 'agent-B', 'controller'), commands):
        with (WORK / f'{name}.log').open('a') as log:
            processes.append(subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=log))
    return processes


def deploy():
    subprocess.run(['rsync', '-az', '--mkpath', '--exclude=__pycache__',
                    str(ROOT / 'twinspark'), f'{PEER}:{WORK}/source/'], check=True)
    subprocess.run(['rsync', '-az', str(WORK / 'agent-B.yaml'), str(WORK / 'secrets'),
                    f'{PEER}:{WORK}/'], check=True)
    print('Updated node B development agent source/configuration', flush=True)


async def stage():
    actions = AgentActions(load_config(WORK / 'agent-A.yaml', AgentConfig))
    for action, params in [('download', {}), ('sync', {'target_host': '192.168.100.2',
                                                       'ssh_user': 'chopc'})]:
        result = await actions.dispatch(action, dict(repo=MODEL, revision=REVISION, **params))
        while True:
            state = actions.task_status(result)
            if state['state'] != 'running':
                print(json.dumps(state), flush=True)
                if state['state'] != 'completed':
                    raise RuntimeError(state['error'])
                break
            await asyncio.sleep(1)


async def wait_agents(agents):
    for _ in range(30):
        try:
            for agent in agents.values():
                await agent.call('hardware_facts')
            return
        except Exception:
            await asyncio.sleep(1)
    raise RuntimeError(f'agents did not start; inspect {WORK}/agent-*.log')


async def test(topologies):
    from twinspark.gateway.app import build_gateway_app
    cfg = load_config(WORK / 'controller.yaml', ControllerConfig)
    vault = SecretsVault(WORK / 'secrets')
    agents = {n: AgentClient(n, ep.agent_url, vault.get('agent_token'))
              for n, ep in cfg.nodes.items()}
    gateway = Gateway(vault.get('inference_api_key'), vault.get('backend_api_key'))
    controller = Controller(cfg, gateway, agents, poll_interval=2)
    processes = service_processes()
    report = dict(started=time.time(), model=MODEL, revision=REVISION, runs=[])
    try:
        await wait_agents(agents)
        facts = await controller.refresh_hardware()
        print(json.dumps(facts, indent=2), flush=True)
        # A parallel development run needs room beyond the model's GPU allocation.
        for node in {n for t in topologies for n in
                     (['A', 'B'] if t == 'pp2' else ['A' if t == 'single-a' else 'B'])}:
            if facts[node]['mem_available_gib'] < 4.5:
                raise RuntimeError(f'node {node}: less than 4.5 GiB available; defer GPU test')
        controller.set_model_spec(MODEL, spec_from_resolved(json.loads((WORK / 'model-info.json').read_text())))
        for topology in topologies:
            path = WORK / f'smollm2-135m-{topology}.json'
            draft = ProfileDraft.model_validate_json(path.read_text())
            if controller.get_profile(draft.name):
                controller.save_revision(draft)
            else:
                controller.create_profile(draft)
            job = await controller.activate(draft.name)
            previous = None
            deadline = time.monotonic() + 420
            while time.monotonic() < deadline:
                job = controller.store.load_job(job.job_id)
                current = [(s.stage, s.message) for s in job.steps]
                if current != previous:
                    print(f'{topology}: {current[-1:]}', flush=True)
                    previous = current
                if job.state.value in ('completed', 'failed') and not controller.busy():
                    break
                await asyncio.sleep(1)
            write_json(WORK / f'{topology}-job.json', job.model_dump(mode='json'))
            if job.state.value != 'completed':
                raise RuntimeError(f'{topology} activation {job.state.value}: {job.error}; '
                                   f'see {topology}-job.json')
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_gateway_app(gateway)),
                                         base_url='http://gateway', timeout=60) as client:
                headers = {'Authorization': f'Bearer {vault.get("inference_api_key")}'}
                response = await client.post('/v1/chat/completions', headers=headers, json={
                    'model': 'dev', 'messages': [{'role': 'user', 'content': 'What is 2 + 2?'}],
                    'temperature': 0, 'max_tokens': 24})
                response.raise_for_status()
                body = response.json()
                assert body['choices'][0]['message']['content']
                stream = await client.post('/v1/chat/completions', headers=headers, json={
                    'model': 'dev', 'messages': [{'role': 'user', 'content': 'Hello!'}],
                    'max_tokens': 8, 'stream': True})
                stream.raise_for_status()
                assert 'data: [DONE]' in stream.text
                unauthorized = await client.get('/v1/models')
                assert unauthorized.status_code == 401
                report['runs'].append(dict(topology=topology, state=job.state.value,
                                           completion=body, streaming=True, authentication=True))
                print(f'{topology}: chat + streaming + auth passed', flush=True)
            # A fresh controller must adopt healthy containers without restarting them.
            assert await controller._adopt_running(controller.active())
            await controller.stop()
        report['passed'] = True
    except Exception as exc:
        report.update(passed=False, error=str(exc))
        raise
    finally:
        if not controller.busy():
            await controller.stop()
        report['finished'] = time.time()
        write_json(WORK / 'test-report.json', report)
        await controller.aclose()
        await gateway._client.aclose()
        for proc in processes:
            proc.terminate()
        for proc in processes:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'deploy', 'stage', 'test', 'serve'])
    parser.add_argument('--topology', action='append', choices=['single-a', 'single-b', 'pp2'])
    args = parser.parse_args()
    if args.command == 'prepare':
        prepare()
    elif args.command == 'deploy':
        deploy()
    elif args.command == 'stage':
        asyncio.run(stage())
    elif args.command == 'test':
        asyncio.run(test(args.topology or ['single-a', 'pp2']))
    else:
        children = service_processes(include_controller=True)
        print('Management: http://127.0.0.1:18443  Gateway: http://127.0.0.1:18000', flush=True)
        try:
            while all(p.poll() is None for p in children):
                time.sleep(1)
        finally:
            for proc in children:
                proc.terminate()


if __name__ == '__main__':
    main()
