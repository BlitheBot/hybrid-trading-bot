CREATE TABLE IF NOT EXISTS debate_log (
    id             SERIAL PRIMARY KEY,
    ticker         VARCHAR(10),
    bull_argument  TEXT,
    bear_argument  TEXT,
    trade_approved BOOLEAN,
    created_at     TIMESTAMPTZ DEFAULT NOW()
);
