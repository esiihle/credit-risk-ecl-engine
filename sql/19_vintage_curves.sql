-- =============================================================================
-- vintage_curves — the vintage triangle
-- =============================================================================
-- The oldest technique in consumer credit MI, and still the first thing a
-- credit committee asks for: group accounts by the month they were originated
-- and track what fraction of each cohort has defaulted by each age.
--
-- The point is to separate two things that calendar-time reporting mixes
-- together. A book's overall arrears rate moves when lending quality changes
-- AND when the book's age profile changes, and those call for opposite
-- responses — tighten the credit policy, or do nothing because last year's
-- growth is simply maturing. Holding age fixed is what makes cohorts
-- comparable at all.
--
-- -----------------------------------------------------------------------------
-- RIGHT-CENSORING: THE TRAP, AND THE THIRD OF ITS FAMILY IN THIS PROJECT
-- -----------------------------------------------------------------------------
-- A cohort originated in December 2024 has been watched for 12 months at this
-- reporting date. One from January 2021 has been watched for 59. Their
-- cumulative default rates are therefore NOT comparable, and the newer cohort
-- will always look better, because less of its life has happened yet.
--
-- Read naively, a vintage triangle on a growing book manufactures a story of
-- steadily improving credit quality out of nothing but the calendar. It is the
-- most common false signal in portfolio MI, and it is the same error this
-- project has already met twice:
--
--   Phase 2  roll rates       bias from the END of an account's series
--   Phase 5  LGD workouts     bias from the BEGINNING of a workout
--   Phase 7  vintage curves   bias from the END of the observation window
--
-- Every time, the naive denominator included observations whose outcome is not
-- yet known, and every time the bias ran in a direction the mechanism predicts
-- in advance.
--
-- So this table never hides how far a cohort has been watched. `is_censored`
-- marks every (vintage, age) cell that the cohort has NOT actually reached, and
-- `20_vintage_comparison.sql` refuses to compare across cohorts at any age one
-- of them has not reached.
--
-- -----------------------------------------------------------------------------
-- WHY MATURED ACCOUNTS STAY IN THE DENOMINATOR
-- -----------------------------------------------------------------------------
-- Terms here run 12 to 60 months, so by age 30 every 24-month loan in the
-- cohort has already repaid and left the book. Those accounts remain in the
-- denominator deliberately: they were observed for the whole of their lives and
-- did not default, which is information, not absence of it. Dropping them would
-- inflate the rate by shrinking the base to whoever happened to still be there.
--
-- It does mean a cohort's curve flattens at older ages partly because accounts
-- have left rather than because risk has fallen, so the curve answers "what
-- fraction of this cohort ever defaulted by age t" and not "what is the hazard
-- at age t". Cross-cohort comparison stays valid because term mix is drawn
-- independently of origination month — the cohorts differ in nothing but noise.
--
-- GRAIN: one row per (vintage, age in months). Censored cells are present and
-- flagged rather than absent, so a consumer cannot silently miss them.
-- =============================================================================

CREATE OR REPLACE TABLE vintage_curves AS
WITH cohort AS (
    SELECT
        vintage_month,
        vintage_month_index,
        COUNT(*)                                            AS cohort_size,
        SUM(principal)                                      AS cohort_principal
    FROM dim_account
    GROUP BY vintage_month, vintage_month_index
),
watched AS (
    -- How far each cohort has actually been observed. This is the fact that
    -- makes the rest of the table honest.
    SELECT a.vintage_month,
           MAX(f.months_on_book)                            AS watched_to_age
    FROM dim_account a
    JOIN fact_loan_month f ON f.account_id = a.account_id
    GROUP BY a.vintage_month
),
first_default AS (
    -- The age at which each account defaulted, once. Default is absorbing, so
    -- MIN is a formality — but taking it explicitly means a future change that
    -- let accounts re-default could not silently double-count them.
    SELECT a.account_id,
           a.vintage_month,
           MIN(f.months_on_book)                            AS default_age
    FROM dim_account a
    JOIN fact_loan_month f ON f.account_id = a.account_id
    WHERE f.is_new_default
    GROUP BY a.account_id, a.vintage_month
),
ages AS (
    SELECT unnest(generate_series(0, CAST($max_age AS INTEGER))) AS age
),
grid AS (
    SELECT c.vintage_month, c.vintage_month_index, c.cohort_size,
           c.cohort_principal, g.age, w.watched_to_age
    FROM cohort c
    CROSS JOIN ages g
    JOIN watched w ON w.vintage_month = c.vintage_month
),
accumulated AS (
    SELECT
        g.vintage_month,
        g.vintage_month_index,
        g.age,
        g.cohort_size,
        g.cohort_principal,
        g.watched_to_age,
        COUNT(d.account_id)                                 AS defaults_by_age
    FROM grid g
    LEFT JOIN first_default d
           ON d.vintage_month = g.vintage_month
          AND d.default_age  <= g.age
    GROUP BY g.vintage_month, g.vintage_month_index, g.age,
             g.cohort_size, g.cohort_principal, g.watched_to_age
)
SELECT
    vintage_month,
    vintage_month_index,
    age,
    cohort_size,
    cohort_principal,
    watched_to_age,
    defaults_by_age,
    CAST(defaults_by_age AS DOUBLE) / NULLIF(cohort_size, 0)
                                                            AS cumulative_default_rate,

    -- The honesty column. TRUE means this cohort has not lived this long yet,
    -- so the rate beside it is not a measurement — it is the last thing known
    -- about the cohort, carried forward.
    age > watched_to_age                                    AS is_censored,

    -- Binomial standard error on the cohort's rate. Quoted because a monthly
    -- cohort here holds roughly 400 accounts, which is far too few to support
    -- the conclusions a vintage chart invites: see methodology §8.3.
    SQRT(
        (CAST(defaults_by_age AS DOUBLE) / NULLIF(cohort_size, 0))
        * (1.0 - CAST(defaults_by_age AS DOUBLE) / NULLIF(cohort_size, 0))
        / NULLIF(cohort_size, 0)
    )                                                       AS standard_error
FROM accumulated
ORDER BY vintage_month_index, age;
