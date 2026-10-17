#!/bin/bash
# Ежедневный бэкап state/ (SQLite) с ротацией. systemd-timer: bilet-backup.timer
set -euo pipefail
state_dir="${BILET_STATE_DIR:-/opt/bilet-bot/state}"
backup_root="${BILET_BACKUP_DIR:-/opt/bilet-bot/backups}"
keep="${BILET_BACKUP_KEEP:-14}"
python3 - "$state_dir" "$backup_root" "$keep" <<'PY'
import sqlite3, sys, time
from pathlib import Path
state, root, keep = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
root.mkdir(parents=True, exist_ok=True, mode=0o700)
stamp = time.strftime("%Y%m%d-%H%M%S")
dest = root / f"state-{stamp}"
dest.mkdir(mode=0o700)
for db in state.glob("*.sqlite3"):
    target = dest / db.name
    with sqlite3.connect(db, timeout=30) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    target.chmod(0o600)
backups = sorted(root.glob("state-*"))
for old in backups[:-keep] if len(backups) > keep else []:
    for f in old.iterdir():
        f.unlink()
    old.rmdir()
print(f"backup ok: {dest} (kept {min(len(backups), keep)})")
PY
