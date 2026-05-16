#!/usr/bin/env bash
set -Eeuo pipefail

# Canonical secret/token pattern shared across checks.
secret_patterns=(
  'ghp_[A-Za-z0-9]{36}'
  'npm_[A-Za-z0-9]{36}'
  'sk-[A-Za-z0-9]{20,}'
  'sk-ant-[A-Za-z0-9-]{20,}'
  'sk_live_[A-Za-z0-9]{20,}'
  'rk_live_[A-Za-z0-9]{20,}'
  'xox[baprs]-[A-Za-z0-9-]{10,}'
  'xoxa-[A-Za-z0-9-]{10,}'
  'AIza[A-Za-z0-9_-]{35}'
  'BEGIN[[:space:]]+[A-Z ]*PRIVATE KEY'
  'AWS_ACCESS_KEY_ID[=:][^[:space:]]+'
  'AWS_SECRET_ACCESS_KEY[=:][^[:space:]]+'
  'DATABASE_URL=[^[:space:]]+'
  'CRON_SECRET[=:][^[:space:]]+'
  'PHANTOM_AES_KEY[=:][^[:space:]]+'
  'Authorization:[[:space:]]+(Bearer|Basic)[[:space:]]+[A-Za-z0-9._~+/-]{8,}'
  'Cookie:[[:space:]]+[A-Za-z0-9._-]+=[^;[:space:]]{8,}'
  'eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}'
)

secret_pattern_joined="$(IFS='|'; echo "${secret_patterns[*]}")"

export CI_CHECKS_SECRET_PATTERN="(${secret_pattern_joined})"
