#!/bin/bash
# Auto-generate project name from directory name
PROJECT_NAME=$(basename "$(pwd)" | tr '[:upper:]' '[:lower:]' | tr -cd '[:alnum:]-_')

# Export for use in docker-compose.yml if needed
export PROJECT_NAME
export TS_HOSTNAME="${PROJECT_NAME}"

echo "Starting containers for project: $PROJECT_NAME"
echo "Tailscale hostname: $TS_HOSTNAME"
echo ""

docker compose -p "$PROJECT_NAME" up -d --build "$@"

echo ""
echo "Enter with: ./shell.sh"
echo "Or: docker compose -p $PROJECT_NAME exec dev zsh"
