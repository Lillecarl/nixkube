# SPDX-License-Identifier: MIT

import hashlib
from datetime import datetime, timezone

import structlog
from cachetools import TTLCache
from kr8s import NotFoundError
from kr8s.asyncio.objects import Pod, new_class

from .constants import KUBE_POD_NAME, NAMESPACE

logger = structlog.get_logger("nixkube.events")

# Cached nixkube pod instance for event reporting
_nixkube_pod: Pod | None = None
# Cache failure state for 15s so we don't hammer the API on every event report
_nixkube_pod_fetch_failed = TTLCache[str, bool](maxsize=1, ttl=15)


async def get_nixkube_pod() -> Pod | None:
    """Get cached nixkube pod instance for event reporting.

    On success: caches forever (pod identity is stable for the daemon's lifetime).
    On failure: retries after 15s to recover from transient API unavailability at startup.
    """
    global _nixkube_pod
    if _nixkube_pod is not None:
        return _nixkube_pod

    if "failed" in _nixkube_pod_fetch_failed:
        return None

    try:
        _nixkube_pod = await Pod.get(KUBE_POD_NAME, namespace=NAMESPACE)
        return _nixkube_pod
    except Exception:
        logger.warning("pod_fetch_failed", exc_info=True)
        _nixkube_pod_fetch_failed["failed"] = True
        return None


# Create ModernEvent class for events.k8s.io/v1 API
ModernEvent = new_class(
    kind="Event",
    version="events.k8s.io/v1",
    namespaced=True,
)

# Kubernetes event note field max size is 1kB (1000 bytes per Kubernetes docs)
MAX_EVENT_NOTE_SIZE = 1000


def _format_event_note(message: str, logs: str | None = None) -> str:
    """Format event note with message and logs, truncated to 1000 bytes.

    Preserves the full message and truncates logs if needed.
    Naturally handles UTF-8 multi-byte characters.

    Args:
        message: Human-readable message (must be < 1000 bytes)
        logs: Optional build/subprocess logs to append

    Returns:
        Formatted and truncated note (max 1000 bytes)
    """
    message_bytes = message.encode()

    # Message must fit within limit (defensive assertion)
    assert len(message_bytes) <= MAX_EVENT_NOTE_SIZE, (
        f"Message alone exceeds {MAX_EVENT_NOTE_SIZE} bytes: {len(message_bytes)}"
    )

    if not logs:
        return message

    # Calculate space available for logs (reserve space for newline separator)
    available_for_logs = MAX_EVENT_NOTE_SIZE - len(message_bytes) - 1

    # Take last 1000 characters of logs (most recent/relevant)
    if len(logs) > 1000:
        logs = logs[-1000:]

    # Trim logs from start until they fit in available space
    logs_bytes = logs.encode()
    while len(logs_bytes) > available_for_logs and len(logs) > 0:
        excess_bytes = len(logs_bytes) - available_for_logs
        logs = logs[excess_bytes:]  # Remove first excess_bytes chars
        logs_bytes = logs.encode()

    # Combine and verify final size (defensive assertion)
    result = f"{message}\n{logs}"
    result_bytes = result.encode()

    assert len(result_bytes) <= MAX_EVENT_NOTE_SIZE, (
        f"Failed to truncate event note to {MAX_EVENT_NOTE_SIZE} bytes: {len(result_bytes)}"
    )

    return result


def _extract_build_logs(exception: Exception) -> str:
    """Extract build logs from an exception, preferring stderr/combined output."""
    # GRPCError stores combined output in args[2] (the details)
    if hasattr(exception, "args") and len(exception.args) > 2:
        details = exception.args[2]
        if isinstance(details, str) and details:
            return details
    # Fallback to exception message
    return str(exception)


async def report_event(
    pod: Pod | None,
    reason: str,
    note: str | None = None,
    event_type: str = "Normal",
    action: str = "Nix",
    logs: str | Exception | None = None,
) -> None:
    """
    Report a Kubernetes event for a pod, incrementing count if it already exists.

    Events are best-effort - failures are logged but don't propagate to avoid
    blocking main operations.

    Args:
        pod: Pod object with name, namespace, and uid (if None, uses get_nixkube_pod())
        reason: Short, UpperCamelCase reason code without "Nix" prefix (will be added)
                e.g., "PodStoreBuildFailed" → "NixPodStoreBuildFailed"
        note: Human-readable description (will be combined with logs)
        event_type: "Normal" or "Warning" (default: "Normal")
        action: Machine-readable action taken (default: "Nix")
        logs: Optional build/subprocess logs (string or exception) to append to note (will be truncated)
    """
    # Fetch nixkube pod if not provided
    if pod is None:
        pod = await get_nixkube_pod()
        if pod is None:
            logger.warning("event_skipped_no_pod", reason=reason)
            return

    # Extract logs from exception if needed
    if isinstance(logs, Exception):
        logs = _extract_build_logs(logs)

    message = note or ""

    # Ensure reason is prefixed with "Nix" if not already
    if not reason.startswith("Nix"):
        reason = f"Nix{reason}"

    logger.debug("report_event", reason=reason, pod=pod.metadata.name)
    try:
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        # Format the final note with message and optional logs
        final_note = _format_event_note(message, logs)

        # Generate deterministic event name from pod uid + reason so we can
        # look it up directly without listing
        event_hash = hashlib.md5(f"{pod.metadata.uid}{reason}".encode()).hexdigest()[:8]
        event_name = f"{pod.metadata.name}.{event_hash}"

        try:
            event = await ModernEvent.get(event_name, namespace=pod.metadata.namespace)
            # Event already exists - patch to increment count
            try:
                existing_series = event.raw.get("series")
                if existing_series is None:
                    new_series = {"count": 2, "lastObservedTime": now_iso}
                else:
                    new_series = {
                        "count": existing_series.get("count", 1) + 1,
                        "lastObservedTime": now_iso,
                    }

                await event.patch({"series": new_series})
                logger.debug(
                    "event_incremented",
                    reason=reason,
                    pod=pod.metadata.name,
                    count=new_series["count"],
                )
            except Exception:
                logger.warning("event_patch_failed", exc_info=True)
        except NotFoundError:
            # Event does not exist yet - create it
            try:
                event_spec = {
                    "metadata": {
                        "name": event_name,
                        "namespace": pod.metadata.namespace,
                    },
                    "type": event_type,
                    "reason": reason,
                    "action": action,
                    "regarding": {
                        "apiVersion": "v1",
                        "kind": "Pod",
                        "name": pod.metadata.name,
                        "namespace": pod.metadata.namespace,
                        "uid": pod.metadata.uid,
                    },
                    "reportingController": "nixkube",
                    "reportingInstance": KUBE_POD_NAME,
                    "note": final_note,
                    "eventTime": now_iso,
                }
                event = await ModernEvent(event_spec, namespace=pod.metadata.namespace)
                await event.create()
                logger.debug("event_created", reason=reason, pod=pod.metadata.name)
            except Exception:
                logger.warning("event_create_failed", exc_info=True)

    except Exception:
        # Catch-all to ensure events never block operations
        logger.warning("event_unexpected_error", exc_info=True)
