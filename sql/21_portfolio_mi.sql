-- =============================================================================
-- portfolio_mi, the book month by month
-- =============================================================================
-- Calendar-time reporting: how the book's stage mix, arrears mix and default
-- flow have moved. This is the view a credit committee actually sits in front
-- of, and it is the one the vintage curves exist to protect them from
-- over-reading.
--
-- THE TWO VIEWS ANSWER DIFFERENT QUESTIONS, AND SWAPPING THEM IS THE CLASSIC
-- MI ERROR. A rise in this table's Stage 2 share can mean lending has got
-- worse, or it can mean last year's growth is simply reaching the ages where
-- arrears appear. Nothing in calendar time can tell those apart. Vintage
-- curves hold age fixed and can.
--
-- On this book the second explanation is the whole story: cohort quality is
-- constant by construction, so every movement here is age profile and noise.
-- Shown rather than asserted in methodology §8.4.
--
-- GRAIN: one row per calendar month end.
-- =============================================================================

CREATE OR REPLACE TABLE portfolio_mi AS
WITH monthly AS (
    SELECT
        f.reporting_date,
        d.month_index,
        f.account_id,
        f.scheduled_balance,
        f.arrears_bucket,
        f.months_on_book,
        f.is_new_default,
        s.stage
    FROM fact_loan_month f
    JOIN dim_date d   ON d.date_key = f.date_key
    LEFT JOIN account_stage s
           ON s.account_id = f.account_id
          AND s.reporting_date = f.reporting_date
)
SELECT
    reporting_date,
    month_index,
    COUNT(*)                                                AS accounts_on_book,
    SUM(scheduled_balance)                                  AS balance_on_book,
    AVG(CAST(months_on_book AS DOUBLE))                     AS average_age_months,

    -- Stage mix, by balance, which is what the provision is sensitive to.
    SUM(CASE WHEN stage = 1 THEN scheduled_balance ELSE 0 END)
        / NULLIF(SUM(scheduled_balance), 0)                 AS stage_1_share,
    SUM(CASE WHEN stage = 2 THEN scheduled_balance ELSE 0 END)
        / NULLIF(SUM(scheduled_balance), 0)                 AS stage_2_share,
    SUM(CASE WHEN stage = 3 THEN scheduled_balance ELSE 0 END)
        / NULLIF(SUM(scheduled_balance), 0)                 AS stage_3_share,

    -- Arrears mix, by account.
    AVG(CASE WHEN arrears_bucket = 0 THEN 1.0 ELSE 0.0 END) AS current_share,
    AVG(CASE WHEN arrears_bucket BETWEEN 1 AND 3
             THEN 1.0 ELSE 0.0 END)                         AS in_arrears_share,

    -- The flow: new defaults arising in the month, as a rate on the accounts
    -- that were able to default. A flow and a stock answer different questions
    -- and are reported separately for that reason.
    SUM(CASE WHEN is_new_default THEN 1 ELSE 0 END)          AS new_defaults,
    SUM(CASE WHEN is_new_default THEN scheduled_balance ELSE 0 END)
                                                            AS new_default_balance,
    SUM(CASE WHEN is_new_default THEN 1 ELSE 0 END)::DOUBLE
        / NULLIF(COUNT(*), 0)                               AS new_default_rate
FROM monthly
GROUP BY reporting_date, month_index
ORDER BY month_index;
