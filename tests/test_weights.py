"""Storage limits and cache correctness must hold before touching a deployment."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from tests.conftest import draft
from twinspark.agent.actions import ActionError, AgentActions
from twinspark.agent.runtime import DockerRuntime
from twinspark.agent.tasks import TaskRegistry
from twinspark.agent.transfers import run_download
from twinspark.controller.launch import LaunchPlanner, docker_run_argv
from twinspark.resolver import resolve_hf_revision
from twinspark.schemas.config import AgentConfig, NodeIdentity
from twinspark.weights import MANIFEST, inference_file, select_files, snapshot_complete, sync_files


def test_select_only_inference_files():
    files = select_files([SimpleNamespace(rfilename=n, size=s) for n, s in [
        ('config.json', 20), ('model.safetensors', 269000000), ('tokenizer.json', 100),
        ('onnx/model.onnx', 540000000), ('onnx/model.safetensors', 269000000),
        ('training_args.bin', 100), ('optimizer.pt', 1000000000), ('runs/log.json', 100)]])
    assert set(files) == {'config.json', 'model.safetensors', 'tokenizer.json'}
    assert not inference_file('../config.json')
    with pytest.raises(ValueError, match='missing file size'):
        select_files([SimpleNamespace(rfilename='model.safetensors', size=None)])


def test_partial_multishard_cache_is_not_complete(tmp_path):
    (tmp_path / 'config.json').write_text('{}')
    (tmp_path / 'tokenizer.json').write_text('{}')
    (tmp_path / 'model.safetensors.index.json').write_text(json.dumps({
        'weight_map': {'layer0': 'model-00001.safetensors', 'layer1': 'model-00002.safetensors'}}))
    (tmp_path / 'model-00001.safetensors').write_bytes(b'weights')
    assert not snapshot_complete(tmp_path)
    (tmp_path / 'model-00002.safetensors').write_bytes(b'weights')
    assert snapshot_complete(tmp_path)


def test_manifest_catches_truncated_weights_and_sync_scopes_revision(tmp_path, require_symlinks):
    repo = tmp_path / 'models--org--model'
    snap = repo / 'snapshots' / ('a' * 40)
    snap.mkdir(parents=True)
    blob = repo / 'blobs' / 'weight-hash'
    blob.parent.mkdir()
    blob.write_bytes(b'weights')
    (snap / 'model.safetensors').symlink_to('../../blobs/weight-hash')
    (snap / 'config.json').write_text('{}')
    (snap / 'tokenizer.json').write_text('{}')
    (snap / MANIFEST).write_text(json.dumps({'revision': snap.name, 'files': {
        'config.json': 2, 'tokenizer.json': 2, 'model.safetensors': 7}}))
    older = repo / 'snapshots' / ('b' * 40)
    older.mkdir()
    (older / 'model.safetensors').write_bytes(b'older')
    assert snapshot_complete(snap)
    files = sync_files(repo, snap.name)
    assert 'models--org--model/blobs/weight-hash' in files
    assert all(('b' * 40) not in n for n in files)
    blob.write_bytes(b'bad')
    assert not snapshot_complete(snap)


def test_sync_follows_hf2_shared_blob_chain(tmp_path, require_symlinks):
    repo = tmp_path / 'models--org--model'
    snap = repo / 'snapshots' / ('a' * 40)
    snap.mkdir(parents=True)
    (repo / 'blobs').mkdir()
    shared = tmp_path / 'blobs' / 'ab' / 'abcd'
    shared.parent.mkdir(parents=True)
    shared.write_bytes(b'weights')
    (repo / 'blobs' / 'hash').symlink_to('../../blobs/ab/abcd')
    (snap / 'model.safetensors').symlink_to('../../blobs/hash')
    (snap / 'config.json').write_text('{}')
    (snap / 'tokenizer.json').write_text('{}')
    files = sync_files(repo, snap.name)
    assert 'models--org--model/blobs/hash' in files
    assert 'blobs/ab/abcd' in files
    assert len(files) == 5


@pytest.mark.asyncio
async def test_download_limit_precedes_weight_transfer(tmp_path, monkeypatch):
    cfg = AgentConfig(node=NodeIdentity(node_id='A', role='agent'), runtime_mode='docker',
                      secrets_dir=str(tmp_path / 'secrets'),
                      runtime={'hf_cache_dir': str(tmp_path / 'hf'), 'max_download_gib': 0.35})
    actions = AgentActions(cfg)
    info = SimpleNamespace(siblings=[SimpleNamespace(rfilename='config.json', size=20),
                                    SimpleNamespace(rfilename='model.safetensors', size=1024**3)])
    called = []
    monkeypatch.setitem(sys.modules, 'huggingface_hub', SimpleNamespace(
        HfApi=lambda **kw: SimpleNamespace(model_info=lambda *a, **kw: info)))
    reg = TaskRegistry()
    reg.tasks['test'] = {'task_id': 'test'}
    with pytest.raises(RuntimeError, match='download limit'):
        await run_download(reg, 'test', actions.rt, 'org/model', 'a' * 40, [], None, False,
                           downloader=called.append)
    assert called == []
    assert not (tmp_path / 'hf').exists()


@pytest.mark.asyncio
async def test_download_records_selected_files_and_reuses_cache(tmp_path, monkeypatch):
    cfg = AgentConfig(node=NodeIdentity(node_id='A', role='agent'), runtime_mode='docker',
                      secrets_dir=str(tmp_path / 'secrets'),
                      runtime={'hf_cache_dir': str(tmp_path / 'hf'), 'max_download_gib': 0.35,
                               'min_disk_free_gib': 0})
    actions = AgentActions(cfg)
    files = {'config.json': b'{}', 'model.safetensors': b'weights', 'tokenizer.json': b'{}'}
    info = SimpleNamespace(siblings=[SimpleNamespace(rfilename=n, size=len(b)) for n, b in files.items()]
                           + [SimpleNamespace(rfilename='onnx/model.onnx', size=1024**3)])
    calls = []
    def download(**kw):
        calls.append(kw)
        snapshot = Path(kw['cache_dir']) / 'models--org--model' / 'snapshots' / kw['revision']
        snapshot.mkdir(parents=True, exist_ok=True)
        for name in kw['allow_patterns']:
            (snapshot / name).write_bytes(files[name])
    monkeypatch.setitem(sys.modules, 'huggingface_hub', SimpleNamespace(
        HfApi=lambda **kw: SimpleNamespace(model_info=lambda *a, **kw: info)))
    reg = TaskRegistry()
    reg.tasks['test'] = {'task_id': 'test'}
    res = await run_download(reg, 'test', actions.rt, 'org/model', 'a' * 40, [], None, False,
                             downloader=lambda kw: download(**kw))
    assert set(calls[0]['allow_patterns']) == set(files)       # onnx export skipped
    assert res['downloaded_bytes'] == sum(len(b) for b in files.values())
    assert actions.weights_present({'repo': 'org/model', 'revision': 'a' * 40})['present']
    # second run: everything is on disk, nothing is downloaded again
    res2 = await run_download(reg, 'test', actions.rt, 'org/model', 'a' * 40, [], None, False,
                              downloader=lambda kw: download(**kw))
    assert len(calls) == 1 and res2['downloaded_bytes'] == 0


def test_local_image_pin_and_small_memory_fraction(controller_config):
    from twinspark.schemas.profile import Profile
    d = draft(topology='single-a')
    d.identity.image_source = 'local'
    d.advanced.gpu_memory_utilization = 0.025
    profile = Profile(name=d.name)
    rev = profile.add_revision(d, d.identity)
    spec = LaunchPlanner(controller_config).plan(rev, 0.025).containers[0]
    argv = docker_run_argv(spec)
    assert d.identity.image_digest in argv
    assert spec.command[spec.command.index('--gpu-memory-utilization') + 1] == '0.025'


def test_missing_local_image_is_never_pulled(tmp_path):
    cfg = AgentConfig(node=NodeIdentity(node_id='A', role='agent'),
                      secrets_dir=str(tmp_path / 'secrets'))
    class Runtime:
        def image_present(self, ref):
            return False
        def pull(self, ref):
            pytest.fail('must not pull a local image ID')
    with pytest.raises(ActionError, match='pulling is disabled'):
        AgentActions(cfg, runtime=Runtime()).image_ensure({'image_ref': 'sha256:' + 'a' * 64})


def test_docker_state_keeps_labels(monkeypatch):
    runtime = DockerRuntime()
    monkeypatch.setattr(runtime, '_run', lambda *a, **kw: json.dumps({
        'State': {'Status': 'running', 'ExitCode': 0},
        'Config': {'Labels': {'org.twinspark.owned': 'true', 'org.twinspark.revision': 'r1'}}}))
    assert runtime.state('tsm-dev').labels['org.twinspark.revision'] == 'r1'


def test_resolver_uses_real_hf_paths_auth_and_single_file_size():
    sha = 'a' * 40
    def handler(request):
        assert request.headers['Authorization'] == 'Bearer secret'
        if request.url.path == '/api/models/org/model/revision/main':
            return httpx.Response(200, json={'sha': sha, 'siblings': [
                {'rfilename': 'model.safetensors', 'size': 269000000},
                {'rfilename': 'onnx/model.safetensors', 'size': 540000000}]})
        if request.url.path == f'/org/model/resolve/{sha}/config.json':
            return httpx.Response(307, headers={'location': '/cached-config'})
        if request.url.path == '/cached-config':
            return httpx.Response(200, json={'num_hidden_layers': 30})
        return httpx.Response(404)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = resolve_hf_revision('org/model', token='secret', client=client)
    assert result['revision'] == sha and result['weight_bytes'] == 269000000
