"""Esportazione completa del database nella cartella di backup."""
from __future__ import annotations

import gzip
import logging
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


def run_backup(cfg) -> Path:
    cfg.backup_dir.mkdir(parents=True, exist_ok=True)
    target = cfg.backup_dir / f"bnb-{datetime.now():%Y%m%d-%H%M%S}.sql.gz"
    tmp = target.with_suffix(".tmp")
    proc = subprocess.Popen(
        ["pg_dump", "--no-owner", "--no-privileges", "--dbname", cfg.database_url],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    with gzip.open(tmp, "wb") as fh:
        shutil.copyfileobj(proc.stdout, fh)
    _, err = proc.communicate()
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"pg_dump fallito: {err.decode(errors='replace').strip()}")
    tmp.rename(target)
    keep = int(cfg.section("backup").get("keep", 14))
    old = sorted(cfg.backup_dir.glob("bnb-*.sql.gz"))[:-keep] if keep > 0 else []
    for f in old:
        f.unlink()
    log.info("Backup scritto in %s", target)
    return target
