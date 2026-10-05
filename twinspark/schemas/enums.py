"""Core enums and value types shared across the TwinSpark domain."""

from __future__ import annotations

from enum import Enum


class NodeRole(str, Enum):
    """Roles a Spark can take in the cluster."""

    CONTROLLER = "controller"  # Node A: runs controller + gateway + agent
    AGENT = "agent"            # Node B: runs agent only


class Topology(str, Enum):
    SINGLE_A = "single-a"
    SINGLE_B = "single-b"
    TP2 = "tp2"
    PP2 = "pp2"
    TP_EP = "tp-ep"
    REPLICATED = "replicated"
    SPLIT = "split"


class DistributedBackend(str, Enum):
    AUTO = "auto"
    NATIVE = "native"  # vLLM / PyTorch Distributed without Ray
    MP = "mp"          # vLLM's process-group backend (the flag value); == native path
    RAY = "ray"


class Quantization(str, Enum):
    """Weight format of the checkpoint (used for sizing and labelling only).

    ``fp8`` covers the DeepSeek-style mixed FP8-dense / FP4-expert checkpoints
    too; ``mxfp4`` is the OCP microscaling FP4 format (MiMo, gpt-oss).
    """

    BF16 = "bf16"
    FP8 = "fp8"
    NVFP4 = "nvfp4"
    MXFP4 = "mxfp4"
    INT4 = "int4"
    AWQ = "awq"
    GPTQ = "gptq"
    AUTOROUND = "autoround"


class RuntimeAdapter(str, Enum):
    NGC = "ngc"                        # NVIDIA officially validated
    EUGR = "eugr"                      # spark-vllm-docker / eugr community
    HYDRA = "hydra"                    # prebuilt Hydra-compatible images
    TONYD2WILD = "tonyd2wild"          # tonyd2wild dual-Spark recipe family
    ANEMLL = "anemll"                  # Anemll dspark-vllm-gx10 image family
    CUSTOM = "custom"                  # Advanced Mode only, unverified


class MemoryStrategy(str, Enum):
    BALANCED = "balanced"
    MAX_CONTEXT = "max-context"
    MAX_QUALITY = "max-quality"
    MAX_THROUGHPUT = "max-throughput"
    MAX_MODEL_SIZE = "max-model-size"


class HeadlessMode(str, Enum):
    DESKTOP = "desktop"
    HEADLESS_SAFE = "headless-safe"
    HEADLESS_MAX = "headless-max"


class ActivationStage(str, Enum):
    """Durable activation state machine (spec §19)."""

    VALIDATING = "validating"
    RESOLVING = "resolving"
    DOWNLOADING = "downloading"
    SYNCING = "syncing"
    DRAINING = "draining"
    STOPPING = "stopping"
    RECLAIMING = "reclaiming"
    STARTING_CLUSTER = "starting-cluster"
    LOADING = "loading"
    COMPILING = "compiling"
    WARMING = "warming"
    TESTING = "testing"
    ROUTING = "routing"
    HEALTHY = "healthy"
    FAILED = "failed"
    ROLLED_BACK = "rolled-back"


class JobState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class VerificationStatus(str, Enum):
    VERIFIED = "verified"          # ran on the maintainers' two DGX Sparks (see the recipe's source note)
    COMMUNITY = "community"        # published, measured dual-Spark recipe
    EXPERIMENTAL = "experimental"  # derived / untested


class DataClass(str, Enum):
    """Immutable identifier parts (spec §2.3)."""

    REPO = "repo"
    REVISION = "revision"
    IMAGE = "image"
    IMAGE_DIGEST = "image_digest"
    VLLM_VERSION = "vllm_version"
