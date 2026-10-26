-- =============================================================================
-- dim_account — one row per loan, as it was at origination
-- =============================================================================
-- GRAIN: one row per account_id.
--
-- This is a slowly changing dimension of type 1 — attributes are overwritten,
-- no history is kept. That is correct HERE because every attribute in it is
-- fixed at origination and never changes: the principal advanced, the
-- contractual term, the rate, the segment the applicant was scored into.
--
-- If this book later gained attributes that DO change over time — a refreshed
-- behavioural score, a repriced rate — those belong either in the fact table
-- (if they change monthly) or in a type-2 dimension with effective dates. Type
-- 1 on a changing attribute silently rewrites history, and a provision
-- restated because a dimension was overwritten is a finding an auditor does
-- not forget.
--
-- The banded columns exist so reporting can group without every query
-- inventing its own buckets. Two reports that band principal differently
-- produce two different answers to the same question.
-- =============================================================================

CREATE OR REPLACE TABLE dim_account AS
SELECT
    -- Natural key. Kept as the primary key because it is already stable,
    -- unique and meaningful; a surrogate integer would add a join without
    -- adding anything else.
    a.account_id,

    a.segment,
    CAST(a.origination_date AS DATE)                        AS origination_date,

    -- The vintage: the cohort an account belongs to for the rest of its life.
    -- Phase 7's vintage curves are a group-by on this column, which is why it
    -- is computed once here rather than in each report.
    CAST(strftime(a.origination_date, '%Y%m') AS INTEGER)   AS vintage_key,
    strftime(a.origination_date, '%Y-%m')                   AS vintage_month,
    CAST(year(a.origination_date) * 12 + month(a.origination_date) AS INTEGER)
                                                            AS vintage_month_index,

    a.principal,
    a.term_months,
    a.annual_rate,
    a.instalment,

    -- The month the loan is contractually repaid. Everything after this date
    -- is outside the account's life, and a fact row there would be a bug.
    CAST(last_day(a.origination_date + INTERVAL 1 MONTH * a.term_months) AS DATE)
                                                            AS maturity_date,

    -- --- Reporting bands ---------------------------------------------------
    CASE
        WHEN a.principal <  15000 THEN '1. under 15k'
        WHEN a.principal <  30000 THEN '2. 15k-30k'
        WHEN a.principal <  60000 THEN '3. 30k-60k'
        WHEN a.principal < 100000 THEN '4. 60k-100k'
        ELSE                           '5. 100k+'
    END                                                     AS principal_band,

    CASE
        WHEN a.term_months <= 12 THEN '1. up to 12m'
        WHEN a.term_months <= 24 THEN '2. 13-24m'
        WHEN a.term_months <= 36 THEN '3. 25-36m'
        WHEN a.term_months <= 48 THEN '4. 37-48m'
        ELSE                          '5. 49m+'
    END                                                     AS term_band,

    CASE
        WHEN a.annual_rate < 0.18 THEN '1. under 18%'
        WHEN a.annual_rate < 0.22 THEN '2. 18-22%'
        WHEN a.annual_rate < 0.25 THEN '3. 22-25%'
        ELSE                           '4. 25%+'
    END                                                     AS rate_band,

    -- --- The answer key ----------------------------------------------------
    -- Carried through deliberately and named so it cannot be mistaken for an
    -- estimate. The engine must never read these columns; the VALIDATION reads
    -- them, which is a different thing and the whole reason the book is
    -- simulated.
    a.true_risk_multiplier,
    a.true_lgd
FROM accounts a;
