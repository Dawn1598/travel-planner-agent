#!/bin/sh
# Use official Chrome because bundled Playwright browsers do not support Ubuntu 20.04.
set -eu
root=/opt/travel-planner
curl -fL --retry 2 --connect-timeout 15 --max-time 240 \
  https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
  -o "$root/incoming/chrome.deb"
mkdir -p "$root/shared/browser"
dpkg-deb -x "$root/incoming/chrome.deb" "$root/shared/browser"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libxcomposite1 libxdamage1 \
  libxrandr2 libgbm1 libxkbcommon0 libxfixes3 libpango-1.0-0 libcairo2 \
  libdbus-1-3 libnspr4 fonts-liberation
"$root/shared/browser/opt/google/chrome/chrome" --version
