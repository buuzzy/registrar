#!/bin/sh
SRC="/CLIProxyAPI/config.yaml"
WORK="/tmp/config.yaml"

cp "$SRC" "$WORK"

if [ -n "$CLI_PROXY_API_KEY" ]; then
  sed -i "s|sk-change-me-to-your-key|$CLI_PROXY_API_KEY|g" "$WORK"
fi

if [ -n "$CLI_PROXY_MANAGEMENT_KEY" ]; then
  sed -i "s|secret-key: 'admin123'|secret-key: '$CLI_PROXY_MANAGEMENT_KEY'|g" "$WORK"
fi

cp "$WORK" "$SRC" 2>/dev/null || cat "$WORK" > "$SRC"

exec /CLIProxyAPI/CLIProxyAPIPlus
