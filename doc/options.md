## nixkube\.enable



Whether to enable nixkube\.



*Type:*
boolean



*Default:*

```nix
false
```



*Example:*

```nix
true
```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.deploySecrets

Deploy SSH keypair Secrets to Kubernetes\. Disable if managing secrets externally (e\.g\., with Vault or Sealed Secrets)\.



*Type:*
boolean



*Default:*

```nix
true
```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.discardStringContext



Strip Nix string context from every resource annotated
` nixkube/discard `, so rendering a manifest does not make the deployer
realise the store paths it names\.

On by default, and it is an optimisation rather than a correctness
choice\. The node and pynixd environments are ` buildEnv ` outputs, one
per enabled system, and ` buildEnv ` sets ` allowSubstitutes = false `\.
Keeping their context therefore makes a deployer *build* each of
them, never fetch them\. Measured on an x86_64 machine with binfmt
off:

error: Cannot build ‘…-nodeEnv\.drv’
Reason: platform mismatch
Required system: ‘aarch64-linux’

Its aarch64 dependencies substituted normally; only the ` buildEnv `
output refused\. So a deployer who has not arranged foreign-platform
builds cannot render a manifest for a cluster with a second
architecture\. That is what this avoids, and it is why the default
holds even though the paths are on cachix: the cache cannot serve a
derivation that declines to be substituted\.

What it costs\. ` ekn.cachePackage ` defaults to the manifest and finds
referenced paths through string context, so with this on it finds
none of these:

store paths named as text in manifest\.json   4
paths in its closure                         1   (itself)

` ekn deploy ` then reports a successful cache push and seeds none of
the paths a node needs to boot\. A cluster that relies on that push
must name the environments itself, through the ` csiPkgs ` module
argument – ` ekn.cachePackage `’s own documentation carries the
fragment\.

Turning this off is reasonable when every enabled system is one the
deployer can realise, or when ` always-allow-substitutes = true ` is
set, which lets the fetch happen despite ` allowSubstitutes = false `
and needs no derivation change\. nixkube’s own CI instances turn it
off for exactly that reason\.

See issue \#29\.



*Type:*
boolean



*Default:*

```nix
true
```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.hostMountPath



Where on the host to put nixkube store, / is untested and not recommended



*Type:*
absolute path



*Default:*

```nix
"/var/lib/nix-csi"
```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.knownHosts



SSH host keys to accept when connecting to cache\.
Keys are written to known_hosts on nodes so they can connect without interactive verification\.



*Type:*
attribute set of (string or absolute path)



*Default:*

```nix
{ }
```



*Example:*

```nix
{
  "nix-cache" = "ssh-ed25519 AAAA...";
}

```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.loggingConfig



Logging configuration for the nixkube service (structlog-based)\.



*Type:*
submodule



*Default:*

```nix
{ }
```



*Example:*

```nix
# JSON renderer (default) — production/Loki
{
  renderer = "json";
  loggers.nixkube.level = "DEBUG";
  root.level = "WARNING";
}

# Logfmt renderer — stern / grep-friendly
{
  renderer = "logfmt";
  loggers.nixkube.level = "INFO";
}

# Console renderer — local development
{
  renderer = "console";
  loggers.nixkube.level = "DEBUG";
  root.level = "DEBUG";
}

```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.loggingConfig\.loggers



Per-logger level overrides\. Keys are Python logger names (dotted hierarchy)\.
All loggers under ` nixkube.* ` inherit from ` nixkube ` unless individually overridden\.



*Type:*
attribute set of (submodule)



*Default:*

```nix
{
  httpx = {
    level = "WARNING";
  };
  nixkube = {
    level = "INFO";
  };
  "nixkube.nix_daemon" = {
    level = "WARNING";
  };
}
```



*Example:*

```nix
{
  "nixkube".level = "DEBUG";
  "nixkube.nri".level = "DEBUG";
  "httpx".level = "ERROR";
}

```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.loggingConfig\.loggers\.\<name>\.level



Log level for this logger\.



*Type:*
one of “DEBUG”, “INFO”, “WARNING”, “ERROR”, “CRITICAL”

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.loggingConfig\.renderer



Log output renderer:

 - ` "json" ` (default): Structured JSON, one object per line\. Recommended
   for production and log aggregation (Loki, ELK, Datadog)\. Each
   structured field is a top-level JSON key, enabling rich queries:
   
   ```
   {app="nixkube"} | json | elapsed_time > 10
   {app="nixkube"} | json | returncode != 0
   {app="nixkube"} | json | container_id =~ "abc"
   ```

 - ` "logfmt" `: ` key=value ` pairs on a single line\. Human-readable and
   machine-parseable\. Works well with ` stern `, ` kubectl logs | grep `,
   and log shippers with native logfmt support (Vector, Fluentd)\.
   Example line:
   
   ```
   level=info logger=nixkube.nri event=build_task_completed container_id=abc123
   ```

 - ` "console" `: Coloured, aligned output for local development\.
   Not suitable for log aggregation or machine parsing\.



*Type:*
one of “json”, “logfmt”, “console”



*Default:*

```nix
"json"
```



*Example:*

```nix
"logfmt"
```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.loggingConfig\.root



Root logger configuration (catch-all for third-party libraries)\.



*Type:*
submodule



*Default:*

```nix
{ }
```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.loggingConfig\.root\.level



Root logger level\. All loggers inherit this unless overridden in ` loggers `\.



*Type:*
one of “DEBUG”, “INFO”, “WARNING”, “ERROR”, “CRITICAL”



*Default:*

```nix
"WARNING"
```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.metadata



Metadata (labels, annotations) applied to nixkube resources



*Type:*
JSON value



*Default:*

```nix
{ }
```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.metrics\.enable



Serve Prometheus metrics from each node pod, on ` port `\.

A DaemonSet answers for its own node, so the series are per-node:
the size and free space of that node’s /nix, and what its garbage
collection and its volumes have done\.



*Type:*
boolean



*Default:*

```nix
true
```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.metrics\.annotations



Add ` prometheus.io/* ` annotations to the node pods, which is what a
Prometheus configured for annotation discovery reads\. Turn this off
where a PodMonitor or a ServiceMonitor selects the pods instead, so
that the two do not both scrape\.



*Type:*
boolean



*Default:*

```nix
true
```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.metrics\.port



The port ` /metrics ` answers on\. Arbitrary: nixkube holds no entry
in the Prometheus port registry\.



*Type:*
16 bit unsigned integer; between 0 and 65535 (both inclusive)



*Default:*

```nix
9099
```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.namespace



Which namespace to deploy nixkube to



*Type:*
string



*Default:*

```nix
"nixkube"
```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.nix\.package



Nix package to use for nix\.conf generation and daemon



*Type:*
package



*Default:*

```nix
<derivation nix-2.34.8>
```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.nixConfig



Shared nix\.conf defaults inherited by node, pynixd controller, and builder\.



*Type:*
submodule

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.nixConfig\.extraOptions



Extra lines to add to nix\.conf



*Type:*
strings concatenated with “\\n”



*Default:*

```nix
""
```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.nixConfig\.settings



Settings rendered to nix\.conf



*Type:*
open submodule of attribute set of (Nix config atom (null, bool, int, float, str, path or package) or list of (Nix config atom (null, bool, int, float, str, path or package)))



*Default:*

```nix
{ }
```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.node\.enable



Whether to enable node DaemonSet (CSI driver and NRI plugin)\.



*Type:*
boolean



*Default:*

```nix
true
```



*Example:*

```nix
true
```

*Declared by:*
 - [/kubenix/daemonset\.nix](file:///kubenix/daemonset.nix)



## nixkube\.node\.compat



Whether to enable nix\.csi\.store CSI driver (for backwards compatibility)\.



*Type:*
boolean



*Default:*

```nix
true
```



*Example:*

```nix
true
```

*Declared by:*
 - [/kubenix/daemonset\.nix](file:///kubenix/daemonset.nix)



## nixkube\.node\.nixConfig



nix\.conf for CSI/mounter/DaemonSet pods



*Type:*
submodule

*Declared by:*
 - [/kubenix/daemonset\.nix](file:///kubenix/daemonset.nix)



## nixkube\.node\.nixConfig\.extraOptions



Extra lines to add to nix\.conf



*Type:*
strings concatenated with “\\n”



*Default:*

```nix
""
```

*Declared by:*
 - [/kubenix/daemonset\.nix](file:///kubenix/daemonset.nix)



## nixkube\.node\.nixConfig\.settings



Settings rendered to nix\.conf



*Type:*
open submodule of attribute set of (Nix config atom (null, bool, int, float, str, path or package) or list of (Nix config atom (null, bool, int, float, str, path or package)))



*Default:*

```nix
{ }
```

*Declared by:*
 - [/kubenix/daemonset\.nix](file:///kubenix/daemonset.nix)



## nixkube\.node\.tolerations



Taints the node DaemonSet tolerates, as a Kubernetes ` tolerations `
list\. Empty by default, so the DaemonSet lands only where an ordinary
workload would\.

This used to be an unconditional toleration of
` node-role.kubernetes.io/control-plane:NoSchedule `, which is right on
a cluster that runs workloads on its control plane – where nixkube
was developed – and wrong as a default\. Shipped that way it assumes
every cluster does that, and on one that respects the taint it puts a
nix-node pod where an ordinary workload would not go\.

Set it to restore the old behaviour where that is what you want:

```
nixkube.node.tolerations = [
  {
    key = "node-role.kubernetes.io/control-plane";
    operator = "Exists";
    effect = "NoSchedule";
  }
];
```



*Type:*
list of (attribute set)



*Default:*

```nix
[ ]
```



*Example:*

```nix
[
  {
    key = "node-role.kubernetes.io/control-plane";
    operator = "Exists";
    effect = "NoSchedule";
  }
]

```

*Declared by:*
 - [/kubenix/daemonset\.nix](file:///kubenix/daemonset.nix)



## nixkube\.nodeBuildTimeout



Timeout in seconds for Nix build operations on node pods\.
Builds exceeding this timeout will be terminated\.



*Type:*
positive integer, meaning >0



*Default:*

```nix
300
```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.pynixd\.enable



Whether to enable pynixd StatefulSet (shared Nix binary cache and build distributor)\.



*Type:*
boolean



*Default:*

```nix
true
```



*Example:*

```nix
true
```

*Declared by:*
 - [/kubenix/pynixd\.nix](file:///kubenix/pynixd.nix)



## nixkube\.pynixd\.authorizedKeys



SSH public keys that can connect to cache\. Used by nodes to push built store paths to the cache\.



*Type:*
list of (string or absolute path)



*Default:*

```nix
[ ]
```



*Example:*

```nix
[
  "ssh-ed25519 AAAA... user@host"
  ./keys/deploy.pub
]

```

*Declared by:*
 - [/kubenix/pynixd\.nix](file:///kubenix/pynixd.nix)



## nixkube\.pynixd\.builder\.nixConfig



nix\.conf for builder pods



*Type:*
submodule

*Declared by:*
 - [/kubenix/pynixd\.nix](file:///kubenix/pynixd.nix)



## nixkube\.pynixd\.builder\.nixConfig\.extraOptions



Extra lines to add to nix\.conf



*Type:*
strings concatenated with “\\n”



*Default:*

```nix
""
```

*Declared by:*
 - [/kubenix/pynixd\.nix](file:///kubenix/pynixd.nix)



## nixkube\.pynixd\.builder\.nixConfig\.settings



Settings rendered to nix\.conf



*Type:*
open submodule of attribute set of (Nix config atom (null, bool, int, float, str, path or package) or list of (Nix config atom (null, bool, int, float, str, path or package)))



*Default:*

```nix
{ }
```

*Declared by:*
 - [/kubenix/pynixd\.nix](file:///kubenix/pynixd.nix)



## nixkube\.pynixd\.builder\.settings



Pynixd configuration as a JSON object\. Merged into the PYNIXD_CONFIG
config file mounted in the pynixd pod\. Corresponds to the PynixdSettings
pydantic model (see pynixd\.config)\.

Common keys include stores (dict of StoreSpec keyed by store ID),
ranking weights, GC intervals, etc\. When stores include SSH stores,
their client keys are auto-discovered from HOME/\.ssh/ if client_keys
is omitted\.



*Type:*
JSON value



*Default:*

```nix
{ }
```



*Example:*

```nix
{
  stores = {
    builder1 = {
      type = "ssh-subprocess";
      host = "builder.example.com";
      port = 22;
      username = "nix";
      systems = [ "x86_64-linux" ];
    };
  };
}

```

*Declared by:*
 - [/kubenix/pynixd\.nix](file:///kubenix/pynixd.nix)



## nixkube\.pynixd\.controller\.nixConfig



nix\.conf for pynixd pod



*Type:*
submodule

*Declared by:*
 - [/kubenix/pynixd\.nix](file:///kubenix/pynixd.nix)



## nixkube\.pynixd\.controller\.nixConfig\.extraOptions



Extra lines to add to nix\.conf



*Type:*
strings concatenated with “\\n”



*Default:*

```nix
""
```

*Declared by:*
 - [/kubenix/pynixd\.nix](file:///kubenix/pynixd.nix)



## nixkube\.pynixd\.controller\.nixConfig\.settings



Settings rendered to nix\.conf



*Type:*
open submodule of attribute set of (Nix config atom (null, bool, int, float, str, path or package) or list of (Nix config atom (null, bool, int, float, str, path or package)))



*Default:*

```nix
{ }
```

*Declared by:*
 - [/kubenix/pynixd\.nix](file:///kubenix/pynixd.nix)



## nixkube\.pynixd\.controller\.settings



Pynixd configuration as a JSON object\. Merged into the PYNIXD_CONFIG
config file mounted in the pynixd pod\. Corresponds to the PynixdSettings
pydantic model (see pynixd\.config)\.

Common keys include stores (dict of StoreSpec keyed by store ID),
ranking weights, GC intervals, etc\. When stores include SSH stores,
their client keys are auto-discovered from HOME/\.ssh/ if client_keys
is omitted\.



*Type:*
JSON value



*Default:*

```nix
{ }
```



*Example:*

```nix
{
  stores = {
    builder1 = {
      type = "ssh-subprocess";
      host = "builder.example.com";
      port = 22;
      username = "nix";
      systems = [ "x86_64-linux" ];
    };
  };
}

```

*Declared by:*
 - [/kubenix/pynixd\.nix](file:///kubenix/pynixd.nix)



## nixkube\.pynixd\.extraVolumeMounts



Extra volume mounts keyed by name\. Merged into the pynixd
container volumeMounts\. Mount external SSH client keys into
HOME/\.ssh/ for asyncssh auto-discovery\.



*Type:*
attribute set of (JSON value)



*Default:*

```nix
{ }
```



*Example:*

```nix
{
  my-builder-key.mountPath = "/nix/var/nix-csi/root/.ssh/id_ed25519";
}

```

*Declared by:*
 - [/kubenix/pynixd\.nix](file:///kubenix/pynixd.nix)



## nixkube\.pynixd\.extraVolumes



Extra Kubernetes volumes keyed by name\. Merged into the
StatefulSet pod spec volumes\. Useful for mounting Secrets
containing SSH client keys for external stores\.



*Type:*
attribute set of (JSON value)



*Default:*

```nix
{ }
```



*Example:*

```nix
{
  my-builder-key.secret.secretName = "my-builder-key";
}

```

*Declared by:*
 - [/kubenix/pynixd\.nix](file:///kubenix/pynixd.nix)



## nixkube\.pynixd\.loadBalancerPort



External SSH port for the pynixd LoadBalancer Service\.
Set to null to disable the LoadBalancer (cluster-internal access only)\.



*Type:*
null or signed integer



*Default:*

```nix
2222
```

*Declared by:*
 - [/kubenix/pynixd\.nix](file:///kubenix/pynixd.nix)



## nixkube\.pynixd\.probes\.failureThreshold



Failed probes in a row before the kubelet acts\. With the defaults
here that is 60 seconds of no answer, against the 3 seconds the
kubelet’s own defaults give\.



*Type:*
positive integer, meaning >0



*Default:*

```nix
6
```

*Declared by:*
 - [/kubenix/pynixd\.nix](file:///kubenix/pynixd.nix)



## nixkube\.pynixd\.probes\.periodSeconds



How often the kubelet probes\.



*Type:*
positive integer, meaning >0



*Default:*

```nix
10
```

*Declared by:*
 - [/kubenix/pynixd\.nix](file:///kubenix/pynixd.nix)



## nixkube\.pynixd\.probes\.startupFailureThreshold



The same, for the startup probe\. Liveness and readiness do not run
until the startup probe passes, so this is how long a cold pynixd
may take to restore its store before anything kills it\. With the
default period that is ten minutes\.



*Type:*
positive integer, meaning >0



*Default:*

```nix
60
```

*Declared by:*
 - [/kubenix/pynixd\.nix](file:///kubenix/pynixd.nix)



## nixkube\.pynixd\.probes\.timeoutSeconds



How long the kubelet waits for the TCP dial of one probe\.

**The kubelet’s own default is 1 second, and that is not enough\.**
A pynixd busy ingesting a multi-hundred-megabyte store transfer does
not answer a dial inside a second, so the liveness probe fails, the
kubelet kills the container, and the push dies with it\. Measured
twice on one cluster while pushing a 186 MiB path: ` Liveness probe failed: dial tcp ...: i/o timeout `, then ` exitCode: 143 `\.

The push does not report a probe failure\. It reports ` Nix daemon disconnected unexpectedly `, which sends the investigation towards
the network instead\. Issue \#37\.



*Type:*
positive integer, meaning >0



*Default:*

```nix
10
```

*Declared by:*
 - [/kubenix/pynixd\.nix](file:///kubenix/pynixd.nix)



## nixkube\.pynixd\.settings



Pynixd configuration as a JSON object\. Merged into the PYNIXD_CONFIG
config file mounted in the pynixd pod\. Corresponds to the PynixdSettings
pydantic model (see pynixd\.config)\.

Common keys include stores (dict of StoreSpec keyed by store ID),
ranking weights, GC intervals, etc\. When stores include SSH stores,
their client keys are auto-discovered from HOME/\.ssh/ if client_keys
is omitted\.



*Type:*
JSON value



*Default:*

```nix
{ }
```



*Example:*

```nix
{
  stores = {
    builder1 = {
      type = "ssh-subprocess";
      host = "builder.example.com";
      port = 22;
      username = "nix";
      systems = [ "x86_64-linux" ];
    };
  };
}

```

*Declared by:*
 - [/kubenix/pynixd\.nix](file:///kubenix/pynixd.nix)



## nixkube\.pynixd\.storageClassName



StorageClass for the pynixd PVC\. null uses the cluster’s default StorageClass\.



*Type:*
null or string



*Default:*

```nix
null
```



*Example:*

```nix
"fast-ssd"
```

*Declared by:*
 - [/kubenix/pynixd\.nix](file:///kubenix/pynixd.nix)



## nixkube\.systems



Which CPU architectures to build nixkube environments for\.
Disable aarch64-linux to skip cross-compilation if your cluster is x86_64-only\.



*Type:*
attribute set of boolean



*Default:*

```nix
{
  aarch64-linux = true;
  x86_64-linux = true;
}
```



*Example:*

```nix
{
  "x86_64-linux" = true;
  "aarch64-linux" = false;
}

```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.undeploy



When true, removes all nixkube Kubernetes resources on the next apply\.



*Type:*
boolean



*Default:*

```nix
false
```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.verifyStorePaths



Verify Nix store paths after building or fetching, before mounting into pods\.



*Type:*
boolean



*Default:*

```nix
true
```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)


