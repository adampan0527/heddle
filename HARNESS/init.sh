#!/bin/bash

# === template-safety: refuse to silently pass when unconfigured ===
if [ -z "${LRH_CONFIGURED:-}" ]; then
  # The framework ships init.sh as a template only.
  # A configured project must set LRH_CONFIGURED=1 in this file or its environment.
  echo "ERROR: init.sh has not been configured for this project." >&2
  echo "       Open init.sh, define the real server-start / smoke-test commands," >&2
  echo "       then re-run or export LRH_CONFIGURED=1 after you have validated it." >&2
  exit 1
fi

# init.sh - Development server startup and testing script
# This script should be customized based on your specific project

set -e  # Exit on error

echo "=========================================="
echo "Starting Development Environment"
echo "=========================================="

# Navigate to the script's directory (in case it's called from elsewhere)
cd "$(dirname "$0")"

# INSTALL DEPENDENCIES (if needed)
echo "Checking dependencies..."
# Uncomment and adjust for your project:
# npm install
# pip install -r requirements.txt
# cargo build
echo "Dependencies are ready."

# START DEVELOPMENT SERVER
echo "Starting development server..."

# For Node.js projects:
# npm run dev &
# SERVER_PID=$!

# For Python projects:
# python manage.py runserver &
# SERVER_PID=$!

# For other projects, adjust accordingly

echo "Waiting for server to be ready..."
sleep 5

# BASIC END-TO-END TEST
echo ""
echo "=========================================="
echo "Running Basic End-to-End Test"
echo "=========================================="

# Add your basic testing commands here

# Example for web applications:
# echo "1. Checking if server is responding..."
# curl -f http://localhost:3000 || exit 1
#
# echo "2. Testing basic functionality..."
# curl -X POST http://localhost:3000/api/test || exit 1

echo "OK: smoke test passed."

# CLEANUP
echo ""
echo "=========================================="
echo "Development environment is ready!"
echo "=========================================="
echo "Server PID: $SERVER_PID"
echo "Press Ctrl+C to stop the server"

# Keep the server running
wait $SERVER_PID
