# SPDX-License-Identifier: Apache-2.0

import os
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

FAKE_NSS = Path(os.environ["FAKE_NSS"])
CA_CERTS = Path(os.environ["CA_CERTS"])


def configure_logging() -> None:
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        structlog.configure(
            processors=[
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.format_exc_info,
                structlog.processors.JSONRenderer(),
            ]
        )


def install_nss() -> None:
    for name in ("passwd", "group", "nsswitch.conf"):
        src = FAKE_NSS / "etc" / name
        dst = Path(f"/etc/{name}")
        if src.exists() and not dst.exists():
            dst.write_text(src.read_text())
            log.info("installed_nss_file", name=name)

    ssl_certs_dst = Path("/etc/ssl/certs")
    if not ssl_certs_dst.exists():
        ssl_certs_src = CA_CERTS / "etc" / "ssl" / "certs"
        ssl_certs_dst.parent.mkdir(parents=True, exist_ok=True)
        ssl_certs_dst.symlink_to(ssl_certs_src)
        log.info("installed_ssl_certs", target=str(ssl_certs_src))

    sh_dst = Path("/bin/sh")
    if not sh_dst.exists():
        sh_dst.parent.mkdir(parents=True, exist_ok=True)
        sh_dst.symlink_to("/nix/var/result/bin/bash")
        log.info("installed_bin_sh", target="/nix/var/result/bin/bash")

    env_dst = Path("/usr/bin/env")
    if not env_dst.exists():
        env_dst.parent.mkdir(parents=True, exist_ok=True)
        env_dst.symlink_to("/nix/var/result/bin/env")
        log.info("installed_usr_bin_env", target="/nix/var/result/bin/env")

    for path, mode in (
        ("/tmp", 0o1777),
        ("/var/tmp", 0o1777),
        ("/var/log", 0o755),
        ("/data/var/nix-csi", 0o755),
    ):
        p = Path(path)
        p.mkdir(parents=True, exist_ok=True)
        p.chmod(mode)
        log.info("ensured_directory", path=path, mode=oct(mode))

    host_key_dir = Path("/data/var/pynixd")
    host_key_dir.mkdir(parents=True, exist_ok=True)
    log.info("ensured_host_key_dir", path=str(host_key_dir))
