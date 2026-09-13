#!/usr/bin/env bash
set -euo pipefail

diagram_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
browser_cache="${HOME}/Library/Caches/ms-playwright"

if [[ -z "${PUPPETEER_EXECUTABLE_PATH:-}" ]]; then
  for chromium in "$browser_cache"/chromium-*/chrome-mac-*/'Google Chrome for Testing.app'/Contents/MacOS/'Google Chrome for Testing'; do
    if [[ -x "$chromium" ]]; then
      export PUPPETEER_EXECUTABLE_PATH="$chromium"
    fi
  done
fi
case "${PUPPETEER_EXECUTABLE_PATH:-}" in
  "$browser_cache"/*) ;;
  *) echo "Set PUPPETEER_EXECUTABLE_PATH to an existing Playwright Chromium in $browser_cache" >&2; exit 1 ;;
esac
if [[ ! -x "$PUPPETEER_EXECUTABLE_PATH" || "$PUPPETEER_EXECUTABLE_PATH" == *'/../'* ]]; then
  echo "Existing Playwright Chromium executable required; no browser will be downloaded." >&2
  exit 1
fi

export PUPPETEER_SKIP_DOWNLOAD=true
export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
export npm_config_audit=false
export npm_config_fund=false

# Only the pinned npm package and its dependencies may be downloaded.
npx --yes @mermaid-js/mermaid-cli@11.12.0 \
  --input "$diagram_dir/architecture.mmd" \
  --output "$diagram_dir/architecture.svg" \
  --backgroundColor white --width 1800
