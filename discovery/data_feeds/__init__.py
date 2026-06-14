"""Historical alternative-data feeds for the discovery backtester (overnight build).

Modules:
  finra_historical  — FINRA consolidated short-volume history (Task 2 / Task 4)
  edgar_historical  — SEC Form 4 insider-filing history (Task 5)

Every loader follows the same fail-open contract used across the discovery
pipeline: on missing/unavailable data it returns empty results so the consuming
strategy family degrades to an all-flat position vector rather than crashing.
"""
