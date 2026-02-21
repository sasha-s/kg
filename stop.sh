#!/bin/bash
# Auto-detect project name from directory
PROJECT_NAME=$(basename "$(pwd)" | tr '[:upper:]' '[:lower:]' | tr -cd '[:alnum:]-_')

echo "Stopping containers for project: $PROJECT_NAME"
docker compose -p "$PROJECT_NAME" down "$@"
