# SPDX-License-Identifier: MIT

{ lib, pkgs }:
let
  inherit (pkgs) writeShellApplication;
in
writeShellApplication {
  name = "ci-debug";
  runtimeInputs = [
    pkgs."claude-code"
    pkgs.kubectl
  ];
  text = # bash
    ''
      set -euo pipefail

      DS_API_KEY="''${DS_API:-''${ANTHROPIC_AUTH_TOKEN:-}}"
      if [ -z "$DS_API_KEY" ]; then
        echo "ci-debug requires DS_API or ANTHROPIC_AUTH_TOKEN to be set" >&2
        exit 1
      fi

      export ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic"
      export ANTHROPIC_AUTH_TOKEN="$DS_API_KEY"
      export ANTHROPIC_MODEL="deepseek-v4-flash"
      export ANTHROPIC_DEFAULT_OPUS_MODEL="deepseek-v4-flash"
      export ANTHROPIC_DEFAULT_SONNET_MODEL="deepseek-v4-flash"
      export ANTHROPIC_DEFAULT_HAIKU_MODEL="deepseek-v4-flash"
      export CLAUDE_CODE_SUBAGENT_MODEL="deepseek-v4-flash"
      export CLAUDE_CODE_EFFORT_LEVEL="max"

      echo "=== ci-debug: Starting claude-code agentic diagnosis ==="

      # Capture the debug output so we can detect if claude fails
      DEBUG_OUTPUT=$(claude \
        --print \
        --dangerously-skip-permissions \
        "You are a Kubernetes SRE diagnosing a CI failure on a GitHub Actions runner.

      A Kind cluster (single-node, ephemeral) is running the nixkube CSI driver and NRI plugin.
      The CI pipeline just deployed nixkube to Kind and ran test workloads.

      CRITICAL RULE: Always verify claims with direct evidence. If an event says
      a resource was 'not found' at some point, check whether it EXISTS RIGHT NOW
      with \`kubectl get\`. Do not assume past events reflect current state.

      Investigation methodology:

      == Phase 1: Pod & Controller Health ==
      1. \`kubectl get pods -A -o wide\` — all pods, their nodes and IPs
      2. \`kubectl describe pod -n nixkube -l app.kubernetes.io/component=node\` — node pod Init Containers (exit codes, restart reasons, crash loop)
      3. \`kubectl logs -n nixkube -l app.kubernetes.io/component=node -c appstarter-init --tail=200 --timestamps --previous\` — init container crash logs
      4. \`kubectl logs -n nixkube -l app.kubernetes.io/component=node --tail=100 --timestamps\` — node main container (nixkube) logs
      5. \`kubectl get daemonset,statefulset,job -n nixkube\` — controller state

      == Phase 2: CSI Driver Registration ==
      6. \`kubectl get csidriver -o wide\` — registered CSI drivers (should include nixkube)
      7. \`kubectl get csinode -o wide\` — per-node CSI driver registration
      8. \`kubectl describe csidriver nixkube\` — CSI driver annotations (podInfoOnMount, attachRequired, modes)

      == Phase 3: Volume & Mount Issues ==
      9. For any pod stuck in ContainerCreating/Pending: \`kubectl describe pod -n nixkube <pod>\` and look for FailedMount events
      10. \`kubectl get events -n nixkube --sort-by='.lastTimestamp' | tail -50\` — recent events, focusing on FailedMount, FailedAttachVolume, and Nix* events
      11. \`kubectl logs -n nixkube -l app.kubernetes.io/component=node -c csi-node-driver-registrar-nixkube --tail=50\` — CSI registrar logs

      == Phase 4: Configuration & Secrets ==
      12. \`kubectl get configmap -n nixkube -o yaml\` — verify nix.conf content (substituters, trusted-public-keys, trusted-users)
      13. \`kubectl get secret,serviceaccount -n nixkube\` — verify expected secrets exist
      14. \`kubectl describe pod -n nixkube -l app.kubernetes.io/component=pynixd\` — cache pod state if the cache variant

      == Phase 5: NRI Plugin (if NRI jobs exist) ==
      15. \`kubectl get nodes -o json | jq '.items[0].status.nodeInfo.containerRuntimeVersion'\` — container runtime
      16. \`ls -la /var/run/nri/\` — NRI socket existence (run inside Kind node with \`docker exec kind-control-plane ls -la /var/run/nri/\`)

      == Phase 6: Nix Store & Build ==
      17. For Any Nix store path errors: check if the store path exists on the host with \`docker exec kind-control-plane ls -la /var/lib/nix-csi/nix/store/\`
      18. \`kubectl get events -n nixkube | grep -E 'Nix|Build|Mount' | tail -30\` — nixkube-specific error events

      Key diagnostic patterns to look for:
      - Init container CrashLoopBackOff with exit code 1 in appstarter-init → nix build failure (check appstarter-init logs for the exact error)
      - Init container CrashLoopBackOff with exit code 143 in main container → SIGTERM from resource limits or liveness probe
      - \"driver name nixkube not found\" in FailedMount events → CSI driver not registered, check csinode
      - \"cannot get /nix/store/...\" in appstarter-init logs → read the \`fetching ... (pynixd: ...)\` line above it. \`unreachable\` means pynixd never answered and served nothing, so the path had to come from a cache and did not; \`answered\` means pynixd is reachable and lacks the path, which is not a connectivity problem; \`disabled\` means a cache is the only source. See issue #27
      - \"seeding /nix/store/... from the image instead\" in appstarter-init logs → the fetch failed and the node started on the image's own copy. It runs, and it is behind. The nix-node logs carry both store paths. See issue #49
      - \"no substituter that can build it\" with no \`cannot get\` line → an older node image, before appstarter said which of those three it was. Same three causes, no way to tell them apart from the log
      - Test jobs in Pending state → CSI volume can't be published, check node events
      - pynixd pod not ready → check init-store CSI volume mount, PVC status
      - \"Operation not permitted\" on SSH → network policy or Cilium blocking, check CiliumNetworkPolicy

      Output your diagnosis in this exact format at the end. Every claim in
      Evidence must cite a specific command output that proves it:

      ## Root Cause
      <one line>

      ## Evidence
      - \`kubectl ...\` — <what it showed and why it matters>

      ## Fix
      <what to change to fix it>

      Do NOT modify cluster resources — this is read-only." 2>&1) || true

      echo "=== ci-debug: claude-code output ==="
      echo "$DEBUG_OUTPUT"

      # Only dump raw state if claude clearly failed (empty or very short output)
      LINE_COUNT=$(echo "$DEBUG_OUTPUT" | wc -l)
      if [ "$LINE_COUNT" -lt 5 ]; then
        echo "=== ci-debug: claude output insufficient, dumping raw state ==="
        kubectl get pods -A -o wide 2>/dev/null || true
        kubectl describe pod -l app.kubernetes.io/component=node -n nixkube 2>/dev/null || true
        kubectl get events -n nixkube --sort-by='.lastTimestamp' 2>/dev/null || true
      fi
    '';
}
