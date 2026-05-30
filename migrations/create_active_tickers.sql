CREATE TABLE IF NOT EXISTS active_tickers (
    ticker       VARCHAR(10) PRIMARY KEY,
    volume_1d    BIGINT,
    rank         INT,
    last_updated TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS active_tickers_rank_idx ON active_tickers (rank);
