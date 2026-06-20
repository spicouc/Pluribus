#!/bin/bash
# Brain v2 automated backup
# Runs: daily via crontab
# Keeps: last 14 daily backups
# Backups: SQLite DB, TurboVec index

BACKUP_DIR="/opt/brain/backups"
DATA_DIR="/opt/brain/data"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RETENTION_DAYS=14
LOG_FILE="/var/log/brain-backup.log"

mkdir -p "$BACKUP_DIR"

echo "[$(date "+%Y-%m-%d %H:%M:%S")] Starting Brain backup..." >> "$LOG_FILE"

# 1. Backup SQLite DB (vacuum + compress)
if [ -f "$DATA_DIR/brain.db" ]; then
    sqlite3 "$DATA_DIR/brain.db" "VACUUM;"
    cp "$DATA_DIR/brain.db" "$BACKUP_DIR/brain_$TIMESTAMP.db"
    gzip -f "$BACKUP_DIR/brain_$TIMESTAMP.db"
    SIZE=$(du -h "$BACKUP_DIR/brain_$TIMESTAMP.db.gz" | cut -f1)
    echo "  DB backed up: brain_$TIMESTAMP.db.gz ($SIZE)" >> "$LOG_FILE"
else
    echo "  brain.db not found!" >> "$LOG_FILE"
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
find "$BACKUP_DIR" -name "brain_*.db.gz" -mtime +$RETENTION_DAYS -delete 2>/dev/null
find "$BACKUP_DIR" -name "turbovec_*.tvim" -mtime +$RETENTION_DAYS -delete 2>/dev/null
echo "  Cleaned backups older than $RETENTION_DAYS days" >> "$LOG_FILE"

# 4. Summary
echo "[$(date "+%Y-%m-%d %H:%M:%S")] Backup complete." >> "$LOG_FILE"
echo "---" >> "$LOG_FILE"
