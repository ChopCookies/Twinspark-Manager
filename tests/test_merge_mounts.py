"""Native Windows dry-run plans retain Linux Docker mount safety checks."""

from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from twinspark.controller import launch
from twinspark.controller.app import create_app
from twinspark.controller.launch import LaunchError, LaunchPlanner, Mount
from twinspark.schemas.enums import Topology
from twinspark.schemas.profile import Profile

from .conftest import draft


def platform(monkeypatch, name):
    # Replace only launch's reference; changing os.name globally breaks pathlib.
    monkeypatch.setattr(launch, "os", SimpleNamespace(name=name))


@pytest.mark.parametrize("host", [r"C:\Users\spark\.cache\huggingface", "D:/cache/hf", r"C:\cache/hf+v1@main",
                                 r"C:\Users\SPARKU~1\AppData\Local\Temp\twinspark-demo\node-a\hf"])
def test_native_windows_drive_hosts_are_accepted_only_on_windows(monkeypatch, host):
    platform(monkeypatch, "nt")
    assert Mount(host=host, container="/root/.cache/huggingface").host == host
    platform(monkeypatch, "posix")
    with pytest.raises(ValidationError):
        Mount(host=host, container="/root/.cache/huggingface")


@pytest.mark.parametrize("host", [
    r"C:cache\hf", r"\cache\hf", r"\\server\share\hf", r"\\?\C:\cache\hf", "C:/",
    "C:/cache:/hostroot", "C:/cache:rw", "C:/cache,file", "C:/cache file", "C:/cache\n",
    "C:/cache\x00", "C:/cache/../other", r"C:\cache\..\other", "C:/.", "../cache",
])
def test_windows_host_paths_reject_relative_unc_traversal_and_docker_separators(monkeypatch, host):
    platform(monkeypatch, "nt")
    with pytest.raises(ValidationError):
        Mount(host=host, container="/root/.cache/huggingface")


@pytest.mark.parametrize("name", ["nt", "posix"])
@pytest.mark.parametrize("container", [r"C:\cache\hf", "C:/cache/hf", "/cache:rw", "/cache,other",
                                        "/cache space", "/cache\n", "relative/cache", "/cache~1"])
def test_container_paths_remain_strictly_posix_on_every_platform(monkeypatch, name, container):
    platform(monkeypatch, name)
    with pytest.raises(ValidationError):
        Mount(host="/tmp/cache", container=container)


@pytest.mark.parametrize("name", ["nt", "posix"])
def test_plain_linux_host_paths_remain_accepted_without_mount_option_injection(monkeypatch, name):
    platform(monkeypatch, name)
    assert Mount(host="/home/spark/.cache/hf+v1@main", container="/root/.cache/huggingface")
    with pytest.raises(ValidationError):
        Mount(host="/tmp/cache:/hostroot", container="/root/.cache/huggingface")
    with pytest.raises(ValidationError):
        Mount(host="/tmp/cache~1", container="/root/.cache/huggingface")


def test_local_windows_cache_paths_produce_a_dry_run_launch_plan(monkeypatch, controller_config):
    platform(monkeypatch, "nt")
    controller_config.runtime.hf_cache_dir = r"C:\Users\spark\.cache\hf"
    controller_config.runtime.compile_cache_dir = "C:/cache/compile"
    controller_config.runtime.mods_dir = r"C:\cache\mods"
    recipe = draft("windows-plan", Topology.SINGLE_A)
    recipe.advanced.mods = ["parser-patch"]
    profile = Profile(name=recipe.name)
    revision = profile.add_revision(recipe, recipe.identity)
    plan = LaunchPlanner(controller_config).plan(revision, 0.8)
    hosts = {mount.host for spec in plan.containers for mount in spec.mounts}
    assert controller_config.runtime.hf_cache_dir in hosts and controller_config.runtime.mods_dir in hosts
    assert any(host.startswith("C:/cache/compile/") for host in hosts)
    assert all(mount.container.startswith("/") and ":" not in mount.container
               for spec in plan.containers for mount in spec.mounts)


@pytest.mark.parametrize("field,value", [("hf_cache_dir", "relative/cache"),
                                       ("compile_cache_dir", "/tmp/cache:/hostroot"),
                                       ("mods_dir", "/tmp/mods with spaces")])
def test_invalid_runtime_mount_configuration_is_a_launch_error(controller_config, field, value):
    setattr(controller_config.runtime, field, value)
    recipe = draft("invalid-mounts", Topology.SINGLE_A)
    recipe.advanced.mods = ["parser-patch"]
    profile = Profile(name=recipe.name)
    revision = profile.add_revision(recipe, recipe.identity)
    with pytest.raises(LaunchError, match="invalid runtime mount paths"):
        LaunchPlanner(controller_config).plan(revision, 0.8)


@pytest.mark.asyncio
async def test_invalid_runtime_mount_configuration_returns_422_from_launch_plan_api(cluster):
    c = cluster.controller
    c.create_profile(draft("invalid-mounts", Topology.SINGLE_A))
    c.config.runtime.hf_cache_dir = "relative/cache"
    app = create_app(c, "management-key", run_startup=False, background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://controller") as client:
        reply = await client.get("/api/v1/profiles/invalid-mounts/revisions/latest/launch-plan",
                                 headers={"x-api-key": "management-key"})
    assert reply.status_code == 422 and "invalid runtime mount paths" in reply.json()["detail"]
