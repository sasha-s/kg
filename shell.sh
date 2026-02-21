#!/bin/bash
# Auto-detect project name from directory
PROJECT_NAME=$(basename "$(pwd)" | tr '[:upper:]' '[:lower:]' | tr -cd '[:alnum:]-_')

docker compose -p "$PROJECT_NAME" exec dev zsh
