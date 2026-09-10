# SPDX-License-Identifier: MIT

"""The consecutive-failure backoff and the startup watchdog, without a cluster.

The arithmetic and the state transitions are pure, so they are testable on
their own. Everything that talks to the API server is not tested here.
"""

import time
from datetime import datetime, timezone

import pytest
from pynixd_nixkube.builder_manager import (
    _BACKOFF_BASE_SECONDS,
    BuilderManager,
    _job_age_seconds,
    _pod_is_ready,
)

SYSTEM = "x86_64-linux"


def manager(**kwargs) -> BuilderManager:
    return BuilderManager(server=None, namespace="nixkube", **kwargs)  # type: ignore[arg-type]


def test_no_failures_means_no_delay():
    m = manager()
    assert m._backoff_delay(0) == 0.0
    assert m._backoff_remaining(SYSTEM) == 0.0


def test_delay_doubles_and_stops_at_the_cap():
    m = manager(backoff_cap=600.0)
    assert m._backoff_delay(1) == _BACKOFF_BASE_SECONDS
    assert m._backoff_delay(2) == _BACKOFF_BASE_SECONDS * 2
    assert m._backoff_delay(3) == _BACKOFF_BASE_SECONDS * 4
    assert m._backoff_delay(5) == _BACKOFF_BASE_SECONDS * 16
    assert m._backoff_delay(6) == 600.0
    assert m._backoff_delay(50) == 600.0


def test_each_failure_grows_the_delay():
    m = manager()
    for expected in (1, 2, 3):
        m._record_failure(SYSTEM, f"job-{expected}", reason="JobFailed")
        assert m._failures[SYSTEM] == expected
    # The third failure holds the system back for four base intervals.
    assert m._backoff_remaining(SYSTEM) == pytest.approx(
        _BACKOFF_BASE_SECONDS * 4, abs=1.0
    )


def test_one_job_counts_once():
    """A failed Job is seen on every reconcile until its TTL reaps it."""
    m = manager()
    for _ in range(5):
        m._record_failure(SYSTEM, "job-a", reason="JobFailed")
    assert m._failures[SYSTEM] == 1


def test_a_forgotten_job_may_count_again():
    m = manager()
    m._record_failure(SYSTEM, "job-a", reason="JobFailed")
    m._forget_job("job-a")
    m._record_failure(SYSTEM, "job-a", reason="JobFailed")
    assert m._failures[SYSTEM] == 2


def test_ready_clears_the_backoff():
    m = manager()
    m._record_failure(SYSTEM, "job-a", reason="JobFailed")
    m._record_failure(SYSTEM, "job-b", reason="JobFailed")
    m._record_ready(SYSTEM, "job-c")
    assert SYSTEM not in m._failures
    assert m._backoff_remaining(SYSTEM) == 0.0
    # And the next failure starts again at the first step, not where it left off.
    m._record_failure(SYSTEM, "job-d", reason="JobFailed")
    assert m._failures[SYSTEM] == 1


def test_systems_are_independent():
    m = manager()
    m._record_failure("aarch64-linux", "job-a", reason="JobFailed")
    assert m._backoff_remaining("aarch64-linux") > 0
    assert m._backoff_remaining(SYSTEM) == 0.0


def test_the_hold_expires():
    m = manager()
    m._record_failure(SYSTEM, "job-a", reason="JobFailed")
    m._backoff_until[SYSTEM] = time.monotonic() - 1.0
    assert m._backoff_remaining(SYSTEM) == 0.0


def test_pod_is_ready_reads_the_condition():
    assert _pod_is_ready({"conditions": [{"type": "Ready", "status": "True"}]})
    assert not _pod_is_ready({"conditions": [{"type": "Ready", "status": "False"}]})
    # A Pod with an IP but no conditions yet is not Ready. This is the case
    # that made registration the wrong reset signal.
    assert not _pod_is_ready({"podIP": "10.0.0.1"})
    assert not _pod_is_ready({})
    assert not _pod_is_ready({"conditions": None})
    assert not _pod_is_ready(
        {"conditions": [{"type": "PodScheduled", "status": "True"}]}
    )


def test_job_age_prefers_start_time():
    now = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
    raw = {
        "metadata": {"creationTimestamp": "2026-09-10T11:00:00Z"},
        "status": {"startTime": "2026-09-10T11:59:00Z"},
    }
    assert _job_age_seconds(raw, now) == 60.0


def test_job_age_falls_back_to_creation():
    """A Job has a creationTimestamp before the controller writes a status."""
    now = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
    raw = {"metadata": {"creationTimestamp": "2026-09-10T11:55:00Z"}, "status": {}}
    assert _job_age_seconds(raw, now) == 300.0


def test_job_age_is_none_without_a_timestamp():
    now = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
    assert _job_age_seconds({}, now) is None
    assert _job_age_seconds({"status": {"startTime": "not a date"}}, now) is None


def job_raw(started: str, uid: str = "uid-1") -> dict:
    return {"metadata": {"uid": uid}, "status": {"startTime": started}}


NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)


def test_a_young_builder_is_left_alone():
    m = manager(startup_timeout=600.0)
    assert not m._startup_expired(job_raw("2026-09-10T11:55:00Z"), "job-a", NOW)


def test_a_builder_that_never_answered_expires():
    m = manager(startup_timeout=600.0)
    assert m._startup_expired(job_raw("2026-09-10T11:45:00Z"), "job-a", NOW)


def test_a_builder_that_was_ready_never_expires():
    """The reason this is not activeDeadlineSeconds.

    A healthy builder Job runs for hours. Age alone would end it.
    """
    m = manager(startup_timeout=600.0)
    m._ever_ready.add("job-a")
    assert not m._startup_expired(job_raw("2026-09-10T08:00:00Z"), "job-a", NOW)


def test_an_expired_builder_is_not_acted_on_twice():
    m = manager(startup_timeout=600.0)
    old = job_raw("2026-09-10T11:00:00Z")
    assert m._startup_expired(old, "job-a", NOW)
    m._record_failure(SYSTEM, "job-a", reason="StartupTimeout")
    assert not m._startup_expired(old, "job-a", NOW)


def test_a_builder_with_no_timestamp_is_left_alone():
    m = manager(startup_timeout=600.0)
    assert not m._startup_expired({"metadata": {}}, "job-a", NOW)


def test_the_job_carries_no_deadline():
    """The rendered Job, not the option that feeds it.

    activeDeadlineSeconds would cap a healthy builder's life, so it is absent
    from both the Job spec and the Pod spec. The Nix side asserts the same
    thing about the PodTemplate it renders.
    """
    m = manager()
    job = m._build_job_resource(SYSTEM, {"spec": {"containers": []}})
    assert "activeDeadlineSeconds" not in job["spec"]
    assert "activeDeadlineSeconds" not in job["spec"]["template"]["spec"]
    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["ttlSecondsAfterFinished"] == 300
    assert job["metadata"]["labels"]["nixkube/system"] == SYSTEM
    assert job["metadata"]["name"].startswith("nixkube-builder-")


def test_overrides_still_merge():
    m = manager()
    job = m._build_job_resource(
        SYSTEM,
        {"spec": {}},
        overrides={"spec": {"template": {"spec": {"nodeName": "n1"}}}},
    )
    assert job["spec"]["template"]["spec"]["nodeName"] == "n1"
    assert job["spec"]["backoffLimit"] == 0
