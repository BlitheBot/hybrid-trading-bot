CREATE TABLE IF NOT EXISTS grok_sentiment (
    ticker       VARCHAR(10) PRIMARY KEY,
    direction    VARCHAR(10),
    score        INT,
    last_updated TIMESTAMPTZ DEFAULT NOW()
);
