# Architecture History & Implementation Notes

Resolved decisions and rationale moved out of CLAUDE.md. Not needed for day-to-day work.

---

## Crypto Scalp: WebSocket → REST Migration

Alpaca paper accounts do not deliver real-time WebSocket tick data without a paid Algo Trader Plus plan. `scalp_loop` was migrated from a WebSocket listener to polling the REST API every 60 seconds. The health state key was renamed from `websocket_connected` to `crypto_polling_active` at that time.

---

## Historical Delivery Notes

### Family 4 — Insider Flow `insider_buy_value` feed
The per-bar `insider_buy_value` column needed by `InsiderFlowPositionStrategy` was added as part of Task 5 via `discovery/data_feeds/edgar_historical.attach_insider_buy_value`. Prior to Task 5 the family ran all-flat because no historical Form 4 feed existed.

### Earnings Protection: superseded flag
`EARNINGS_PROTECTION_ENABLED` supersedes the legacy `EARNINGS_FILTER_ENABLED` flag. `EARNINGS_FILTER_ENABLED` is kept in config as a fallback only when earnings protection is disabled.
