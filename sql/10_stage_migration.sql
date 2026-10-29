-- =============================================================================
-- stage_migration — month-on-month movement between IFRS 9 stages
-- =============================================================================
-- The same shape as the arrears roll rates in Phase 2, one level up: instead of
-- buckets it tracks stages, and instead of a collections view it gives the
-- provisioning view.
--
-- This is what a provision walk is built from. When the ECL moves between two
-- reporting dates, the question is always "how much of that was accounts
-- migrating stage, and how much was everything else" — and the answer starts
-- here.
--
-- Two measures, and they answer different questions:
--
--   accounts   how many moved. The operational view, and what a collections
--              team is managed on.
--   balance    how much EXPOSURE moved. The provisioning view. A handful of
--              large accounts migrating to Stage 2 moves the provision more
--              than a crowd of small ones, and the account count hides that
--              completely.
--
-- Censoring is handled exactly as in Phase 2: a migration exists only where the
-- next month is observed. See `05_bucket_transitions.sql` for why the naive
-- alternative biases every rate downward.
-- =============================================================================

CREATE OR REPLACE TABLE stage_migration AS
WITH sequenced AS (
    SELECT
        s.account_id,
        s.date_key,
        s.reporting_date,
        s.months_on_book,
        s.stage                                              AS from_stage,
        s.scheduled_balance                                  AS from_balance,
        LEAD(s.stage)          OVER account_timeline         AS to_stage,
        LEAD(s.months_on_book) OVER account_timeline         AS next_months_on_book
    FROM account_stage s
    WINDOW account_timeline AS (
        PARTITION BY s.account_id ORDER BY s.months_on_book
    )
),
moves AS (
    SELECT *
    FROM sequenced
    WHERE to_stage IS NOT NULL
      AND next_months_on_book = months_on_book + 1
),
counted AS (
    SELECT
        from_stage,
        to_stage,
        COUNT(*)                  AS accounts,
        SUM(from_balance)         AS balance
    FROM moves
    GROUP BY from_stage, to_stage
),
totals AS (
    SELECT from_stage,
           SUM(accounts) AS account_opportunities,
           SUM(balance)  AS balance_opportunities
    FROM counted GROUP BY from_stage
)
SELECT
    c.from_stage,
    c.to_stage,
    c.accounts,
    c.balance,
    t.account_opportunities,
    CAST(c.accounts AS DOUBLE) / t.account_opportunities     AS account_rate,
    c.balance / NULLIF(t.balance_opportunities, 0)           AS balance_rate,
    CASE
        WHEN c.to_stage > c.from_stage THEN 'deterioration'
        WHEN c.to_stage < c.from_stage THEN 'improvement'
        ELSE                                'unchanged'
    END                                                      AS direction
FROM counted c
JOIN totals t ON t.from_stage = c.from_stage
ORDER BY c.from_stage, c.to_stage;
