#!/usr/bin/env bash
set -Eeuo pipefail

pgrep -af redis-server \
  | awk '/127[.]0[.]0[.]1:11000|127[.]0[.]0[.]1:13000/ {print $1}' \
  | xargs -r kill
