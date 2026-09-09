# SPDX-License-Identifier: MIT

import asyncio
import contextlib
import time
import uuid
from typing import TYPE_CHECKING, Any, cast

import kr8s.asyncio as k8s
import structlog
from kr8s.asyncio.objects import Job, PodTemplate
from pynixd.config import SSHSubprocessStoreSpec
from pynixd.serde.ids import StoreId
from pynixd.store import SSHSubprocessStore

if TYPE_CHECKING:
    from pynixd.instance import Server

log = structlog.get_logger(__name__)

BUILDER_LABEL = "app.kubernetes.io/component"
BUILDER_LABEL_VALUE = "builder"
BUILDER_PODTEMPLATE_NAME = "nixkube-builder"
SYSTEM_LABEL = "nixkube/system"
PROBED_LABEL = "nixkube/probed"
PROBE_LABEL = "nixkube/probe"
FEATURES_ANNOTATION = "nixkube/features"

KUBE_ARCH_TO_NIX_SYSTEM = {
    "amd64": "x86_64-linux",
    "arm64": "aarch64-linux",
}
NIX_SYSTEM_TO_KUBE_ARCH = {v: k for k, v in KUBE_ARCH_TO_NIX_SYSTEM.items()}

_DEFAULT_FEATURES = {"nixos-test", "big-parallel", "benchmark"}

_COOLDOWN_SECONDS = 60.0
_RECONNECT_DELAY = 10.0


def deep_merge(base: dict, overrides: dict) -> dict:
    """Deep-merge overrides into base (Nix lib.recursiveUpdate style).

    Non-dict values in overrides replace base values entirely.
    Lists are replaced, not merged.
    """
    result = base.copy()
    for key, value in overrides.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class BuilderManager:
    def __init__(
        self,
        server: "Server",
        namespace: str,
        max_builders: int = 3,
        min_builders: int = 1,
        idle_timeout: int = 300,
        systems: list[str] | None = None,
        cooldown_seconds: float = _COOLDOWN_SECONDS,
    ) -> None:
        self.server = server
        self.namespace = namespace
        self.max_builders = max_builders
        self.min_builders = min_builders
        self.idle_timeout = idle_timeout
        self.systems = systems or ["x86_64-linux"]
        self.cooldown_seconds = cooldown_seconds
        self._last_create_time: dict[str, float] = {}
        self._registered: dict[str, str] = {}
        self._idle_since: dict[str, float] = {}
        self._job_names: dict[str, str] = {}
        self._available_systems: set[str] = set()
        self._pending_probes: set[str] = set()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        await self._reap_orphaned_builder_pods()
        self._available_systems = set(self.systems)
        await self._sync_dynamic_features()
        self._task = asyncio.create_task(self._run())
        log.info(
            "builder_manager_started",
            namespace=self.namespace,
            max_builders=self.max_builders,
            min_builders=self.min_builders,
            systems=list(self._available_systems),
        )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        log.info("builder_manager_stopped")

    async def _run(self) -> None:
        await asyncio.gather(
            self._watch_jobs(),
            self._watch_queue(),
            self._reap_idle(),
            self._watch_nodes(),
        )

    # ---- Node probing ----

    async def _watch_nodes(self) -> None:
        """Watch for unprobed nodes and probe them automatically."""
        while True:
            try:
                api = await k8s.api()
                async for event in api.watch(
                    "nodes",
                    label_selector=f"!{PROBED_LABEL}",
                ):
                    event_type, resource = cast(Any, event)
                    if event_type not in {"ADDED", "MODIFIED"}:
                        continue
                    labels = getattr(resource.metadata, "labels", {}) or {}
                    node_name = getattr(resource.metadata, "name", "")
                    if not node_name or node_name in self._pending_probes:
                        continue
                    kube_arch = labels.get("kubernetes.io/arch", "")
                    nix_system = KUBE_ARCH_TO_NIX_SYSTEM.get(kube_arch)
                    if not nix_system or nix_system not in set(self.systems):
                        log.warning(
                            "probe_node_unsupported", node=node_name, arch=kube_arch
                        )
                        continue
                    self._pending_probes.add(node_name)
                    asyncio.create_task(self._probe_node(node_name, nix_system))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("node_watch_error", exc_info=True)
                await asyncio.sleep(10)

    async def _probe_node(self, node_name: str, system: str) -> None:
        """Probe one node: create builder, wait for probe, label, cleanup."""
        log.info("probe_node_started", node=node_name, system=system)
        job_name = None
        try:
            overrides = {
                "metadata": {
                    "labels": {
                        PROBE_LABEL: "true",
                    },
                },
                "spec": {
                    "template": {
                        "spec": {
                            "nodeName": node_name,
                        },
                    },
                },
            }
            job_name = await self._create_builder_job(
                system=system, overrides=overrides
            )
            if not job_name:
                return

            store_id = f"builder-{job_name}"
            for _ in range(30):
                store = self.server.stores.get(store_id)  # type: ignore[arg-type]
                if store is not None:
                    break
                await asyncio.sleep(1)
            else:
                log.warning("probe_store_not_found", job=job_name, node=node_name)
                return

            await store._probe_event.wait()
            features = store.feature_matrix
            if features:
                await self._label_node(node_name, next(iter(features)), features)
        finally:
            self._pending_probes.discard(node_name)
            if job_name:
                try:
                    job = await Job.get(job_name, namespace=self.namespace)
                    await self._delete_job(job)
                except Exception:  # noqa: BLE001 -- best-effort cleanup of a probe job; reconciliation follows regardless
                    log.warning("probe_job_cleanup_failed", job=job_name)

        await self._reconcile_systems()

    async def _label_node(
        self, node_name: str, system: str, features: dict[str, set[str]]
    ) -> None:
        """Store probe results on the node as labels and a features annotation."""
        all_feats = sorted({f for feats in features.values() for f in feats})
        try:
            api = await k8s.api()
            async for node in api.get("nodes", node_name):
                node = cast(Any, node)
                await node.patch(
                    {
                        "metadata": {
                            "labels": {
                                PROBED_LABEL: "true",
                            },
                            "annotations": {
                                FEATURES_ANNOTATION: ",".join(all_feats),
                            },
                        },
                    },
                    type="merge",
                )
                break
            log.info(
                "node_labeled_with_probe",
                node=node_name,
                system=system,
                features=all_feats,
            )
        except Exception:
            log.exception("node_labeling_failed", node=node_name)

    # ---- Dynamic features ----

    async def _sync_dynamic_features(self) -> None:
        scheduler = self.server.scheduler
        if scheduler is None:
            return
        features = await self._read_node_features(self._available_systems)
        scheduler.add_dynamic_features(features)
        log.info("dynamic_features_synced", systems=sorted(features))

    async def _read_node_features(self, systems: set[str]) -> dict[str, set[str]]:
        """Read feature matrix from nixkube/features annotation."""
        feature_matrix: dict[str, set[str]] = {}
        try:
            api = await k8s.api()
            async for node in api.get("nodes"):
                node = cast(Any, node)
                labels = getattr(node.metadata, "labels", {}) or {}
                if labels.get(PROBED_LABEL) != "true":
                    continue
                kube_arch = labels.get("kubernetes.io/arch", "")
                sys_label = KUBE_ARCH_TO_NIX_SYSTEM.get(kube_arch)
                if sys_label not in systems or sys_label in feature_matrix:
                    continue
                annotations = getattr(node.metadata, "annotations", {}) or {}
                raw = annotations.get(FEATURES_ANNOTATION, "")
                features = set(filter(None, raw.split(","))) if raw else set()
                feature_matrix[sys_label] = features or set(_DEFAULT_FEATURES)
        except Exception:
            log.exception("read_node_features_error")
        for s in systems:
            feature_matrix.setdefault(s, set(_DEFAULT_FEATURES))
        return feature_matrix

    async def _reconcile_systems(self) -> None:
        """Read node kubernetes.io/arch to determine available systems."""
        enabled = set(self.systems)
        any_probed = False
        available: set[str] = set()

        try:
            api = await k8s.api()
            async for node in api.get("nodes"):
                node = cast(Any, node)
                labels = getattr(node.metadata, "labels", {}) or {}
                if labels.get(PROBED_LABEL) == "true":
                    any_probed = True
                    kube_arch = labels.get("kubernetes.io/arch", "")
                    system = KUBE_ARCH_TO_NIX_SYSTEM.get(kube_arch)
                    if system and system in enabled:
                        available.add(system)
        except Exception:
            log.exception("system_reconcile_error")
            return

        if not any_probed:
            return

        old = self._available_systems
        self._available_systems = available
        await self._sync_dynamic_features()

        if available != old:
            log.info(
                "available_systems_changed",
                available=sorted(available),
                removed=sorted(old - available),
                added=sorted(available - old),
            )

    # ---- Job watching and reconciliation ----

    async def _watch_jobs(self) -> None:
        while True:
            try:
                api = await k8s.api()

                seen: set[str] = set()
                async for job in api.get(
                    "jobs",
                    namespace=self.namespace,
                    label_selector={BUILDER_LABEL: BUILDER_LABEL_VALUE},
                ):
                    job = cast(Any, job)
                    seen.add(job.metadata.name)
                    await self._reconcile_job(job)

                for store_id in list(self._registered):
                    store_id_prefix = "builder-"
                    if store_id.startswith(store_id_prefix):
                        job_name = store_id[len(store_id_prefix) :]
                        if job_name not in seen:
                            await self._unregister_builder(store_id)

                await self._ensure_min_builders()

                async for event in api.watch(
                    "jobs",
                    namespace=self.namespace,
                    label_selector={BUILDER_LABEL: BUILDER_LABEL_VALUE},
                ):
                    event_type, job = cast(Any, event)
                    if job is None:
                        continue

                    if event_type == "DELETED":
                        store_id = f"builder-{job.metadata.name}"
                        if store_id in self._registered:
                            await self._unregister_builder(store_id)
                        await self._ensure_min_builders()
                    elif event_type in ("ADDED", "MODIFIED"):
                        await self._reconcile_job(job)
                        await self._ensure_min_builders()
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001 -- watch loop: any failure is retried after _RECONNECT_DELAY
                log.warning("builder_watch_error", error=f"{type(e).__name__}: {e}")

            await asyncio.sleep(_RECONNECT_DELAY)

    async def _reconcile_job(self, job: Any) -> None:
        job_name = job.metadata.name
        store_id = f"builder-{job_name}"

        if await self._is_stale_builder(job):
            log.info("builder_stale", store_id=store_id, job_name=job_name)
            await self._unregister_builder(store_id)
            await self._delete_job(job)
            return

        status = job.raw.get("status", {})
        if status.get("succeeded") or status.get("failed"):
            if store_id in self._registered:
                await self._unregister_builder(store_id)
            return

        labels = getattr(job.metadata, "labels", {}) or {}
        is_probe = labels.get(PROBE_LABEL) == "true"

        if store_id not in self._registered:
            pod_ip = await self._get_job_pod_ip(job)
            if pod_ip:
                await self._register_builder(store_id, pod_ip, job_name, probe=is_probe)
        else:
            self._job_names[store_id] = job_name

    async def _register_builder(
        self, store_id: str, pod_ip: str, job_name: str = "", probe: bool = False
    ) -> None:
        store = SSHSubprocessStore(
            SSHSubprocessStoreSpec(
                store_id=StoreId(store_id),
                host=pod_ip,
                port=22,
                username="nix",
                client_keys=["/etc/ssh-key/id_ed25519"],
                nix_bin="/nix/var/result/bin/nix",
                monitor=False,
                no_schedule=probe,
            )
        )
        try:
            await self.server.add_store(store, dynamic=True)
            self._registered[store_id] = pod_ip
            self._idle_since[store_id] = time.monotonic()
            if job_name:
                self._job_names[store_id] = job_name
            log.info("builder_registered", store_id=store_id, pod_ip=pod_ip)
        except Exception:
            log.exception("builder_register_failed", store_id=store_id, pod_ip=pod_ip)

    async def _unregister_builder(self, store_id: str) -> None:
        try:
            await self.server.remove_store(store_id)  # type: ignore[arg-type]
            self._registered.pop(store_id, None)
            self._idle_since.pop(store_id, None)
            self._job_names.pop(store_id, None)
            log.info("builder_unregistered", store_id=store_id)
        except Exception:
            log.exception("builder_unregister_failed", store_id=store_id)

    async def _is_stale_builder(self, job: Any) -> bool:
        expected = await self._get_expected_store_versions()
        if not expected:
            return False
        job_versions = self._get_job_store_versions(job)
        return not job_versions <= expected

    @staticmethod
    def _get_job_store_versions(job: Any) -> set[str]:
        for vol in (
            getattr(job, "raw", {})
            .get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("volumes", [])
        ):
            csi = vol.get("csi", {})
            if csi.get("driver") == "nixkube" and csi.get("readOnly") is False:
                found = set()
                for val in csi.get("volumeAttributes", {}).values():
                    if isinstance(val, str) and val.startswith("/nix/store/"):
                        found.add(val)
                return found
        return set()

    async def _get_expected_store_versions(self) -> set[str]:
        try:
            template = await PodTemplate.get(
                BUILDER_PODTEMPLATE_NAME, namespace=self.namespace
            )
        except Exception:
            log.exception("builder_podtemplate_get_failed")
            return set()
        pod_spec = template.raw.get("template", {})
        for vol in pod_spec.get("spec", {}).get("volumes", []):
            csi = vol.get("csi", {})
            if csi.get("driver") == "nixkube" and csi.get("readOnly") is False:
                found = set()
                for val in csi.get("volumeAttributes", {}).values():
                    if isinstance(val, str) and val.startswith("/nix/store/"):
                        found.add(val)
                return found
        return set()

    async def _get_job_pod_ip(self, job: Any) -> str | None:
        try:
            api = await k8s.api()
            async for pod in api.get(
                "pods",
                namespace=self.namespace,
                label_selector={"job-name": job.metadata.name},
            ):
                pod = cast(Any, pod)
                pod_ip = pod.raw.get("status", {}).get("podIP")
                if pod_ip:
                    return pod_ip
        except Exception:
            log.exception("job_pod_ip_fetch_failed", job=job.metadata.name)
        return None

    # ---- Builder min/max lifecycle ----

    async def _ensure_min_builders(self) -> None:
        try:
            api = await k8s.api()
            active_by_system: dict[str, int] = {}
            async for job in api.get(
                "jobs",
                namespace=self.namespace,
                label_selector={BUILDER_LABEL: BUILDER_LABEL_VALUE},
            ):
                job = cast(Any, job)
                status = job.raw.get("status", {})
                if not status.get("succeeded") and not status.get("failed"):
                    labels = getattr(job.metadata, "labels", {}) or {}
                    if labels.get(PROBE_LABEL) == "true":
                        continue
                    sys_label = labels.get(SYSTEM_LABEL, "unknown")
                    active_by_system[sys_label] = active_by_system.get(sys_label, 0) + 1
        except Exception:
            log.exception("builder_job_count_error")
            return

        total_active = sum(active_by_system.values())

        for system in sorted(self._available_systems):
            active = active_by_system.get(system, 0)
            if active >= self.min_builders:
                continue
            if total_active >= self.max_builders:
                break

            log.info(
                "ensuring_min_builders",
                system=system,
                active=active,
                min_builders=self.min_builders,
                max_builders=self.max_builders,
            )
            await self._create_builder_job(system=system)
            total_active += 1
            await asyncio.sleep(0.5)

    async def _maybe_create_builder(self, system: str) -> None:
        now = time.monotonic()
        last = self._last_create_time.get(system, 0.0)
        if now - last < self.cooldown_seconds:
            return

        try:
            api = await k8s.api()
            active = 0
            async for job in api.get(
                "jobs",
                namespace=self.namespace,
                label_selector={BUILDER_LABEL: BUILDER_LABEL_VALUE},
            ):
                status = getattr(job, "raw", {}).get("status", {})
                if not status.get("succeeded") and not status.get("failed"):
                    labels = getattr(job, "metadata", {}).get("labels", {}) or {}
                    if labels.get(PROBE_LABEL) == "true":
                        continue
                    active += 1
        except Exception:
            log.exception("builder_job_count_error")
            return

        if active >= self.max_builders:
            log.debug(
                "builder_max_reached",
                active=active,
                max=self.max_builders,
                system=system,
            )
            return

        log.info(
            "creating_builder_job", active=active, max=self.max_builders, system=system
        )
        await self._create_builder_job(system=system)
        self._last_create_time[system] = now

    async def _reap_idle(self) -> None:
        while True:
            try:
                total = len(self._registered)
                if total <= self.min_builders:
                    await asyncio.sleep(60)
                    continue

                now = time.monotonic()
                for store_id, pod_ip in list(self._registered.items()):
                    store = self.server.stores.get(store_id)  # type: ignore[arg-type]
                    if store is None:
                        continue

                    if store.in_flight > 0:
                        self._idle_since[store_id] = now
                        continue

                    idle_seconds = now - self._idle_since.get(store_id, now)
                    if idle_seconds < self.idle_timeout:
                        continue

                    if len(self._registered) <= self.min_builders:
                        break

                    log.info(
                        "builder_idle_timeout",
                        store_id=store_id,
                        pod_ip=pod_ip,
                        idle_seconds=idle_seconds,
                    )
                    await self._delete_builder_job(store_id)
            except Exception:
                log.exception("builder_idle_reap_error")

            await asyncio.sleep(60)

    async def _delete_job(self, job: Any) -> None:
        try:
            await job.delete(propagation_policy="Background")
            log.info("builder_job_deleted", job=job.metadata.name)
        except Exception:
            log.exception("builder_job_delete_failed", job=job.metadata.name)

    async def _delete_builder_job(self, store_id: str) -> None:
        job_name = self._job_names.get(store_id)
        if not job_name:
            return

        try:
            job = await Job.get(job_name, namespace=self.namespace)
            await self._delete_job(job)
        except Exception:
            log.exception("builder_job_delete_idle_failed", job=job_name)

    async def _reap_orphaned_builder_pods(self) -> None:
        try:
            api = await k8s.api()
            async for pod in api.get(
                "pods",
                namespace=self.namespace,
                label_selector={BUILDER_LABEL: BUILDER_LABEL_VALUE},
            ):
                pod = cast(Any, pod)
                refs = getattr(pod.metadata, "ownerReferences", None)
                if not refs:
                    log.info("orphaned_builder_pod_found", pod=pod.metadata.name)
                    await pod.delete(propagation_policy="Background")
        except Exception:
            log.exception("orphaned_builder_pod_cleanup_error")

    # ---- Queue watching for reactive builder creation ----

    async def _watch_queue(self) -> None:
        scheduler = self.server.scheduler
        if scheduler is None:
            return

        while True:
            try:
                needed_systems: set[str] = set()
                for build in scheduler.queue.queue:
                    if build.is_pending and build.platform in self._available_systems:
                        needed_systems.add(build.platform)

                if not needed_systems:
                    await asyncio.sleep(5)
                    continue

                active_per_system: dict[str, int] = {}
                for s in self.server.stores.values():
                    if (
                        s.store_id.startswith("builder-")
                        and s.is_healthy
                        and not s.draining
                        and not s.no_schedule
                    ):
                        fm = s.feature_matrix
                        if fm:
                            for system in fm:
                                active_per_system[system] = (
                                    active_per_system.get(system, 0) + 1
                                )
                        else:
                            for system in self._available_systems:
                                active_per_system[system] = (
                                    active_per_system.get(system, 0) + 1
                                )

                for system in sorted(needed_systems):
                    if active_per_system.get(system, 0) == 0:
                        await self._maybe_create_builder(system)
            except Exception:
                log.exception("builder_queue_watch_error")

            await asyncio.sleep(5)

    # ---- Job creation ----

    async def _create_builder_job(
        self, system: str, overrides: dict | None = None
    ) -> str | None:
        try:
            template = await PodTemplate.get(
                BUILDER_PODTEMPLATE_NAME, namespace=self.namespace
            )
        except Exception:  # noqa: BLE001 -- absent, malformed or unreachable all mean the same thing to the caller
            log.error("builder_podtemplate_not_found", name=BUILDER_PODTEMPLATE_NAME)
            return None

        pod_spec = dict(template.raw.get("template", {}))

        kube_arch = NIX_SYSTEM_TO_KUBE_ARCH.get(system)
        if kube_arch:
            spec = pod_spec.setdefault("spec", {})
            node_selector = spec.setdefault("nodeSelector", {})
            node_selector["kubernetes.io/arch"] = kube_arch

        job_id = str(uuid.uuid4())[:8]

        job_resource = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": f"nixkube-builder-{job_id}",
                "namespace": self.namespace,
                "labels": {
                    BUILDER_LABEL: BUILDER_LABEL_VALUE,
                    SYSTEM_LABEL: system,
                },
            },
            "spec": {
                "ttlSecondsAfterFinished": 300,
                "backoffLimit": 0,
                "template": pod_spec,
            },
        }

        if overrides:
            job_resource = deep_merge(job_resource, overrides)

        job_name = job_resource["metadata"]["name"]

        try:
            job = await Job(job_resource)
            await job.create()
            log.info(
                "builder_job_created",
                job=job_name,
                system=system,
                node_name=job_resource["spec"]["template"]
                .get("spec", {})
                .get("nodeName"),
            )
            return job_name
        except Exception:
            log.exception(
                "builder_job_create_failed",
                job=job_name,
                system=system,
            )
            return None
