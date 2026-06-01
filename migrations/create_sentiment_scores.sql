CREATE TABLE IF NOT EXISTS sentiment_scores (
    ticker         VARCHAR(10) PRIMARY KEY,
    direction      VARCHAR(10),
    score          INT,
    headline_count INT,
    last_updated   TIMESTAMPTZ DEFAULT NOW()
);
