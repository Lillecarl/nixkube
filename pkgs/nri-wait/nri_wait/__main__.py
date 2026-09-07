"""Entry point for nri-wait OCI hook."""

import json
import os
import sys

import zmq

from . import check_build_status, wait_for_completion


def main() -> None:
    """Main entry point - orchestrates query and wait phases."""
    # OCI runtime passes container state as JSON on stdin for createRuntime hooks.
    oci_state: dict = {}
    try:
        oci_state = json.load(sys.stdin)
        print(
            f"[nri-wait] OCI state: id={oci_state.get('id')} pid={oci_state.get('pid')} bundle={oci_state.get('bundle')}",
            file=sys.stderr,
        )
    # Anything at all. A hook that dies here says nothing about why, and the
    # missing 'id' below is the message the operator needs.
    except Exception as e:  # noqa: BLE001
        print(f"[nri-wait] Could not parse OCI state from stdin: {e}", file=sys.stderr)

    container_id = oci_state.get("id")
    if not container_id:
        print("[nri-wait] Error: OCI state missing 'id' field", file=sys.stderr)
        sys.exit(1)

    # Read from environment variables (set by OCI hook)
    try:
        query_socket = os.environ["NRI_QUERY_SOCKET"]
        pub_socket = os.environ["NRI_PUB_SOCKET"]
        timeout = int(os.environ.get("NRI_TIMEOUT", "30"))
    except KeyError as e:
        print(f"Error: Required environment variable not set: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError:
        print("Error: NRI_TIMEOUT must be a valid integer", file=sys.stderr)
        sys.exit(1)

    # Create ZeroMQ context
    context = zmq.Context()

    try:
        # Phase 1: Query if build is already done (also registers the PID/bundle with nix-nri)
        if check_build_status(
            context,
            query_socket,
            oci_state,
            timeout,
        ):
            print(
                f"[nri-wait] Build already completed for {container_id}",
                file=sys.stderr,
            )
            return

        print(
            f"[nri-wait] Build pending for {container_id}, subscribing to updates...",
            file=sys.stderr,
        )

        # Phase 2: Subscribe to PUB socket and wait for completion
        wait_for_completion(context, pub_socket, container_id, timeout)
    finally:
        context.term()


if __name__ == "__main__":
    main()
