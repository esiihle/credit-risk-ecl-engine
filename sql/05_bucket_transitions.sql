-- =============================================================================
-- bucket_transition — one row per OBSERVED month-to-month move
-- =============================================================================
-- GRAIN: one row per account per month, for every month whose successor is
-- actually observed.
--
-- THE CENSORING TRAP, which is the whole reason this file is not three lines.
--
-- The obvious implementation takes `LEAD(arrears_bucket)` and calls it the
-- destination. That silently mishandles the last row of every account, and the
-- last row of every account is never a random sample:
--
--   * accounts that DEFAULTED end on their default row. The move INTO default
--     is observed from the previous row, and there is no move out — default is
--     absorbing.
--   * accounts still on book at the reporting date are RIGHT-CENSORED. Their
--     final month has a successor in reality; we just have not seen it yet.
--   * accounts that MATURED simply stop.
--
-- `LEAD` returns NULL for all three. Treating NULL as "stayed in the same
-- bucket" — which is what happens if you COALESCE it, or if you compute rates
-- as "count of moves to j divided by count of rows in i" — adds a fake
-- self-transition for every account's last month. That inflates the stay rate
-- and understates every roll-forward rate, and the bias is worst exactly where
-- it matters: deep arrears buckets, where accounts are most likely to be in
-- their final observed month.
--
-- So: a transition exists only where the NEXT month is actually there. Rows
-- without one are excluded, not defaulted to anything.
--
-- The `months_on_book + 1` condition is belt and braces. `04_mart_quality.sql`
-- already proves there are no gaps in the series, but a transition computed
-- across a gap would be a two-month move counted as one, and that assumption
-- is cheap to state explicitly rather than inherit.
-- =============================================================================

CREATE OR REPLACE TABLE bucket_transition AS
WITH sequenced AS (
    SELECT
        f.account_id,
        f.date_key,
        f.reporting_date,
        f.months_on_book,
        f.arrears_bucket                                      AS from_bucket,
        f.scheduled_balance                                   AS from_balance,

        LEAD(f.arrears_bucket) OVER account_timeline          AS to_bucket,
        LEAD(f.months_on_book) OVER account_timeline          AS next_months_on_book
    FROM fact_loan_month f
    WINDOW account_timeline AS (
        PARTITION BY f.account_id ORDER BY f.months_on_book
    )
)
SELECT
    s.account_id,
    s.date_key,
    s.reporting_date,
    s.months_on_book,
    s.from_bucket,
    s.to_bucket,
    s.from_balance,

    -- Direction of travel, named once so reports do not each invent their own
    -- vocabulary for the same three cases.
    CASE
        WHEN s.to_bucket > s.from_bucket THEN 'roll forward'
        WHEN s.to_bucket < s.from_bucket THEN 'cure'
        ELSE                                  'stay'
    END                                                       AS movement,

    s.to_bucket = 4                                           AS rolled_to_default,
    s.from_bucket = 0                                         AS started_current
FROM sequenced s
WHERE s.to_bucket IS NOT NULL             -- the successor must actually exist
  AND s.next_months_on_book = s.months_on_book + 1;   -- and be the NEXT month
