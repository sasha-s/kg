#!/bin/bash
set -e

# Fix permissions for volume mounts (run as root via sudo)
if [ -d /commandhistory ] && [ "$(stat -c '%U' /commandhistory 2>/dev/null)" != "node" ]; then
    echo "Fixing permissions for /commandhistory..."
    sudo chown -R node:node /commandhistory 2>/dev/null || true
fi

# Execute the main command (CMD from Dockerfile)
exec "$@"
