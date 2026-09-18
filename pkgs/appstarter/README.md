# appstarter

Puts a usable Nix store on a node, then starts the application in it.

Two modes, and they run in different containers.

`appstarter init` runs in the initContainer, out of the container image. There
`/nix` is the image's own store and the node's store is mounted somewhere else.
It tries to fetch the version the deployment asks for. If that fails, it copies
the version inside the image instead, so the node starts a working `nixkube` or
`pynixd` rather than none.

`appstarter run` runs in the main container, out of the store `init` just
filled. It says which version is running and which one was asked for, then
`exec`s the application.

## Why the image carries a second copy

`ekn.cacheTo` is pynixd. A pynixd that cannot start cannot be given the store
paths that would let it start, and the node behind it waits for a substituter
that is waiting for the node. Issue #27 is that cycle seen from the node, and
#46 is an initContainer sitting in `Init:0/1` behind it.

An image that can always start *something* ends the cycle. Degraded and running
beats correct and absent, for infrastructure other things depend on -- as long
as the skew is reported, which is what `run` is for.

## Why it reports rather than restarts

A newer version arriving is not a reason for the process to exit. Restarts are
the kubelet's business: `CrashLoopBackOff` reads a healthy upgrade as a fault,
a restart of pynixd fails other nodes' in-flight fetches because pynixd *is*
the substituter, and nothing inside the process knows whether a build is
running. So `run` states the skew, and something outside decides when to take
the new version.

## The environment it reads

| variable | mode | meaning |
| --- | --- | --- |
| `APPSTARTER_WANTED` | init | The store path the deployment asks for, or a JSON object keyed by system. |
| `APPSTARTER_FALLBACK` | init | The store path inside the image. Set by the image, not by the pod spec -- see `seed.py`. |
| `APPSTARTER_STORE` | init | Where the node's store is mounted. Default `/nix-volume`. |
| `PYNIXD_ENABLED` | init | Whether to ask pynixd as well as the configured substituters. |
| `APPSTARTER_WANTED_STORE_PATH` | run | Written by `run` for the application it starts. |
| `APPSTARTER_RUNNING_STORE_PATH` | run | Written by `run` for the application it starts. |
