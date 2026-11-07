These five JSON fixtures are copied unchanged from the sanitized 2026-11-07
losvps AGY 1.5.2 probe results. They are observed protocol evidence, not newly
executed acceptance results. `<PROBE_DIR>` is a redacted location.

The fake ACP uses the observed MCP 2025-11-25 initialize request, discover
extension, stdio descriptor, conversation metadata and prefixed notifications.
The fixture's MCP initialize response was from the probe's older echo server;
the new relay negotiates 2025-11-25 explicitly.
