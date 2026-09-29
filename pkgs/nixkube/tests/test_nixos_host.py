# SPDX-License-Identifier: MIT

import pytest

from src.nixos_host import (
    TOPLEVEL_ANNOTATION,
    HostSwitchError,
    switch_unit,
    wanted_toplevel,
)

CURRENT = "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-nixos-system-cp-26.11"
NEXT = "/nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-nixos-system-cp-26.11"


def test_no_annotation_does_nothing():
    assert wanted_toplevel({}, CURRENT) is None


def test_running_system_does_nothing():
    assert wanted_toplevel({TOPLEVEL_ANNOTATION: CURRENT}, CURRENT) is None


def test_other_system_is_wanted():
    assert wanted_toplevel({TOPLEVEL_ANNOTATION: NEXT}, CURRENT) == NEXT


def test_host_without_current_system_is_switched():
    assert wanted_toplevel({TOPLEVEL_ANNOTATION: NEXT}, None) == NEXT


@pytest.mark.parametrize(
    "value",
    [
        "/tmp/nixos-system",
        f"{NEXT}/bin/switch-to-configuration",
        f"{NEXT}; reboot",
        "nixpkgs#hello",
    ],
)
def test_not_a_store_path_is_refused(value):
    with pytest.raises(HostSwitchError, match="is not a store path"):
        wanted_toplevel({TOPLEVEL_ANNOTATION: value}, CURRENT)


def test_switch_unit_is_stable_and_distinct():
    assert switch_unit(NEXT) == switch_unit(NEXT)
    assert switch_unit(NEXT) != switch_unit(CURRENT)
    assert switch_unit(NEXT).startswith("nixkube-switch-")
