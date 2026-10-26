-- =============================================================================
-- fact_loan_month — the fact table
-- =============================================================================
-- GRAIN, stated once and enforced by a test:
--
--     ONE ROW PER ACCOUNT PER CALENDAR MONTH END ON WHICH THE ACCOUNT
--     WAS ON BOOK.
--
-- This is a PERIODIC SNAPSHOT fact table, not a transaction fact. The
-- distinction decides how it behaves:
--
--   * a transaction fact records events — a payment, a drawdown — and rows
--     appear only when something happens
--   * a periodic snapshot records STATE at regular intervals, whether or not
--     anything happened, and that is what provisioning needs: the question is
--     always "what did the book look like at month end", never "what happened
--     in between"
--
-- The practical consequence is that a month with no activity still produces a
-- row. A gap in an account's monthly series is therefore a DEFECT, not an
-- absence of events, and `04_mart_quality.sql` looks for exactly that.
--
-- ADDITIVITY — the thing most often got wrong:
--
--   * `scheduled_balance` is SEMI-ADDITIVE. Summing it across accounts at one
--     month end gives the book's exposure, which is meaningful. Summing it
--     across months gives a number with no interpretation at all — an account
--     owing 10,000 for twelve months did not owe 120,000.
--   * `is_default` and `is_new_default` are additive across accounts; only the
--     second is additive across time, because the first counts the same
--     default again every month it persists.
--
-- These are flagged in column comments because a semi-additive measure summed
-- the wrong way produces a confident, catastrophic number.
-- =============================================================================

CREATE OR REPLACE TABLE fact_loan_month AS
SELECT
    -- --- Keys --------------------------------------------------------------
    lm.account_id,
    d.date_key,
    a.vintage_key,

    -- A compact surrogate for the grain itself. Having it as one column makes
    -- the uniqueness test a one-liner and gives later joins a single key.
    lm.account_id || '-' || CAST(d.date_key AS VARCHAR)      AS loan_month_key,

    d.month_end_date                                         AS reporting_date,

    -- --- Position in the account's life ------------------------------------
    -- Recomputed from the two dimensions rather than carried from the source,
    -- so it is consistent with dim_date by construction. If the generator and
    -- the calendar ever disagreed, this is where it would show.
    d.month_index - a.vintage_month_index                    AS months_on_book,
    a.term_months - (d.month_index - a.vintage_month_index)  AS months_remaining,

    -- --- State at month end -------------------------------------------------
    lm.arrears_bucket,
    lm.days_past_due,
    lm.scheduled_balance,          -- SEMI-ADDITIVE: across accounts, never across months
    lm.is_default,

    -- Derived flags that every later phase needs, defined once here.
    lm.days_past_due >= 30                                   AS is_30_plus,
    lm.days_past_due >= 90                                   AS is_90_plus,
    lm.arrears_bucket > 0                                    AS is_delinquent,

    -- The first month an account appears in default. Additive across time,
    -- unlike `is_default`, which would count one default once per month for
    -- the rest of the account's life.
    lm.is_default
        AND NOT COALESCE(
            LAG(lm.is_default) OVER (
                PARTITION BY lm.account_id ORDER BY lm.months_on_book
            ), FALSE)                                        AS is_new_default,

    -- --- The answer key, carried for validation only -----------------------
    a.true_lgd,
    a.true_risk_multiplier
FROM loan_month lm
INNER JOIN dim_account a
       ON a.account_id = lm.account_id
INNER JOIN dim_date d
       ON d.month_end_date = CAST(last_day(lm.reporting_date) AS DATE)
ORDER BY lm.account_id, d.date_key;
