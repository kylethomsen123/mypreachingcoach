#!/usr/bin/env bash
set -euo pipefail

: "${OPENAI_API_KEY:?Set OPENAI_API_KEY in your environment}"
: "${ANTHROPIC_API_KEY:?Set ANTHROPIC_API_KEY in your environment}"
: "${SENDGRID_API_KEY:?Set SENDGRID_API_KEY in your environment}"

echo "Keys loaded from environment."
# Add your real commands below
