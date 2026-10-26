-- =============================================================================
-- Data quality checks on the mart, as SQL
-- =============================================================================
-- Each row of the result is one check: its name, how many rows failed it, and
-- whether it passed. Written as SQL rather than Python on purpose — these are
-- assertions about the WAREHOUSE, and someone reviewing the mart should be
-- able to read and rerun them without a Python environment.
--
-- A check that cannot fail is not a check, so each one here is written so that
-- a plausible bug would make it fail:
--
--   grain uniqueness      -- a duplicated join explodes the fact silently and
--                            every sum downstream is simply wrong
--   orphaned keys         -- a fact row with no dimension row disappears from
--                            any inner-joined report, so the book shrinks
--   gaps in the series    -- the defining property of a periodic snapshot; a
--                            missing month is a missing state, not a quiet one
--   balance monotonicity  -- an amortising loan's balance cannot rise
--   DPD vs bucket         -- two columns that must tell the same story
--   life boundaries       -- no rows before origination or after maturity
--   default is absorbing  -- a cure out of 90+ would contradict the model
-- =============================================================================

WITH grain_duplicates AS (
    SELECT COUNT(*) AS failures FROM (
        SELECT loan_month_key
        FROM fact_loan_month
        GROUP BY loan_month_key
        HAVING COUNT(*) > 1
    )
),
orphan_accounts AS (
    SELECT COUNT(*) AS failures
    FROM fact_loan_month f
    LEFT JOIN dim_account a ON a.account_id = f.account_id
    WHERE a.account_id IS NULL
),
orphan_dates AS (
    SELECT COUNT(*) AS failures
    FROM fact_loan_month f
    LEFT JOIN dim_date d ON d.date_key = f.date_key
    WHERE d.date_key IS NULL
),
series_gaps AS (
    -- Every account's months_on_book must run 0, 1, 2, ... with no holes.
    -- Comparing the span to the row count catches a gap without needing a
    -- self-join: if they differ, a month is missing.
    SELECT COUNT(*) AS failures FROM (
        SELECT account_id
        FROM fact_loan_month
        GROUP BY account_id
        HAVING MAX(months_on_book) - MIN(months_on_book) + 1 <> COUNT(*)
    )
),
balance_increases AS (
    SELECT COUNT(*) AS failures FROM (
        SELECT account_id
        FROM (
            SELECT
                account_id,
                scheduled_balance,
                LAG(scheduled_balance) OVER (
                    PARTITION BY account_id ORDER BY months_on_book
                ) AS previous_balance
            FROM fact_loan_month
        )
        WHERE previous_balance IS NOT NULL
          AND scheduled_balance > previous_balance + 0.01
    )
),
dpd_disagrees_with_bucket AS (
    SELECT COUNT(*) AS failures
    FROM fact_loan_month
    WHERE (arrears_bucket = 0 AND days_past_due <> 0)
       OR (arrears_bucket > 0 AND days_past_due = 0)
       OR (is_default AND days_past_due < 90)
       OR (NOT is_default AND days_past_due >= 90)
),
outside_account_life AS (
    SELECT COUNT(*) AS failures
    FROM fact_loan_month f
    JOIN dim_account a ON a.account_id = f.account_id
    WHERE f.months_on_book < 0
       OR f.months_on_book > a.term_months
),
default_not_absorbing AS (
    -- An account recorded in default and then not in default the next month
    -- would contradict the model. Catches a mis-sorted window or a bad join.
    SELECT COUNT(*) AS failures FROM (
        SELECT account_id
        FROM (
            SELECT
                account_id,
                is_default,
                LAG(is_default) OVER (
                    PARTITION BY account_id ORDER BY months_on_book
                ) AS was_default
            FROM fact_loan_month
        )
        WHERE was_default AND NOT is_default
    )
)
SELECT 'grain is unique'              AS check_name, failures FROM grain_duplicates
UNION ALL SELECT 'no orphaned accounts',        failures FROM orphan_accounts
UNION ALL SELECT 'no orphaned dates',           failures FROM orphan_dates
UNION ALL SELECT 'no gaps in monthly series',   failures FROM series_gaps
UNION ALL SELECT 'balance never increases',     failures FROM balance_increases
UNION ALL SELECT 'DPD agrees with bucket',      failures FROM dpd_disagrees_with_bucket
UNION ALL SELECT 'rows within account life',    failures FROM outside_account_life
UNION ALL SELECT 'default is absorbing',        failures FROM default_not_absorbing;
