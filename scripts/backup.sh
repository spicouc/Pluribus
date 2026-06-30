#!/bin/bash
# Pluribus automated backup
# Runs: daily via crontab
# Keeps: last 14 daily backups
# Backups: SQLite DB, TurboVec index

BACKUP_DIR="/opt/pluribus/backups"
DATA_DIR="/opt/pluribus/data"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RETENTION_DAYS=14
LOG_FILE="/var/log/pluribus-backup.log"

mkdir -p "$BACKUP_DIR"

echo "[$(date "+%Y-%m-%d %H:%M:%S")] Starting Pluribus backup..." >> "$LOG_FILE"

# 1. Backup SQLite DB (vacuum + compress)
if [ -f "$DATA_DIR/pluribus.db" ]; then
    sqlite3 "$DATA_DIR/pluribus.db" "VACUUM;"
    cp "$DATA_DIR/pluribus.db" "$BACKUP_DIR/pluribus_$TIMESTAMP.db"
    gzip -f "$BACKUP_DIR/pluribus_$TIMESTAMP.db"
    SIZE=$(du -h "$BACKUP_DIR/pluribus_$TIMESTAMP.db.gz" | cut -f1)
    echo "  DB backed up: pluribus_$TIMESTAMP.db.gz ($SIZE)" >> "$LOG_FILE"
else
    echo "  pluribus.db not found!" >> "$LOG_FILE"
fi

# 2. Backup TurboVec index
if [ -f "$DATA_DIR/turbovec_index.tvim" ]; then
    cp "$DATA_DIR/turbovec_index.tvim" "$BACKUP_DIR/turbovec_$TIMESTAMP.tvim"
    SIZE=$(du -h "$BACKUP_DIR/turbovec_$TIMESTAMP.tvim" | cut -f1)
    echo "  TurboVec backed up: turbovec_$TIMESTAMP.tvim ($SIZE)" >> "$LOG_FILE"
else
    echo "  turbovec_index.tvim not found" >> "$LOG_FILE"
fi

# 3. Clean old backups (retention)
find "$BACKUP_DIR" -name "pluribus_*.db.gz" -mtime +$RETENTION_DAYS -delete 2>/dev/null
find "$BACKUP_DIR" -name "turbovec_*.tvim" -mtime +$RETENTION_DAYS -delete 2>/dev/null
echo "  Cleaned backups older than $RETENTION_DAYS days" >> "$LOG_FILE"

# 4. Summary
echo "[$(date "+%Y-%m-%d %H:%M:%S")] Backup complete." >> "$LOG_FILE"
echo "---" >> "$LOG_FILE"
