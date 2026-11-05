-- =============================================================================
-- ead_schedule — exposure at default, for every account and every future month
-- =============================================================================
-- ECL discounts an expected loss in each future month, so it needs the exposure
-- the account would carry IF it defaulted in that month — not the exposure it
-- carries today.
--
-- For a fixed-term amortising loan this is arithmetic rather than a model:
--
--     i          = (1 + annual_rate)^(1/12) - 1
--     balance_t  = P * ((1+i)^n - (1+i)^t) / ((1+i)^n - 1)
--
-- Written out in SQL rather than carried from Python so the whole exposure
-- calculation is reviewable in one place and portable to SAS in Phase 8.
--
-- WHY THIS IS THE EASY INPUT, AND WHY THAT WAS THE POINT OF THE PRODUCT CHOICE
--
-- On a term loan the borrower cannot increase their exposure: the schedule is
-- contractual and the balance only falls. On a credit card or an overdraft it
-- is the hard input — exposure at default depends on how much of an undrawn
-- limit a distressed borrower draws down before defaulting, which needs a
-- credit conversion factor, which is itself a model with its own data problem.
--
-- Choosing an unsecured TERM product in Phase 0 made EAD exact and left LGD as
-- the only uncertain input. That was deliberate: one genuinely uncertain input
-- is enough to demonstrate the machinery, and two would obscure which one was
-- driving the error.
--
-- GRAIN: one row per account per future month to maturity.
-- =============================================================================

CREATE OR REPLACE TABLE ead_schedule AS
WITH live AS (
    SELECT
        p.account_id,
        p.months_on_book,
        p.months_remaining,
        p.scheduled_balance                                    AS balance_today,
        a.principal,
        a.term_months,
        a.annual_rate,
        -- The monthly effective rate. Also the discount rate in Phase 6:
        -- IFRS 9 requires discounting at the effective interest rate, so the
        -- same figure that builds the schedule discounts the loss off it.
        POWER(1.0 + a.annual_rate, 1.0 / 12.0) - 1.0           AS monthly_rate
    FROM account_pd p
    JOIN dim_account a ON a.account_id = p.account_id
),
horizons AS (
    SELECT unnest(generate_series(1, CAST($max_horizon AS INTEGER))) AS horizon
)
SELECT
    l.account_id,
    h.horizon,
    l.months_on_book + h.horizon                               AS months_on_book_then,

    -- The annuity formula, evaluated at the future month.
    l.principal
        * (POWER(1.0 + l.monthly_rate, l.term_months)
           - POWER(1.0 + l.monthly_rate, l.months_on_book + h.horizon))
        / NULLIF(POWER(1.0 + l.monthly_rate, l.term_months) - 1.0, 0)
                                                               AS ead,

    -- The discount factor for a loss arising in this month, at the EIR.
    1.0 / POWER(1.0 + l.monthly_rate, h.horizon)               AS discount_factor,
    l.monthly_rate
FROM live l
JOIN horizons h ON h.horizon <= l.months_remaining
ORDER BY l.account_id, h.horizon;
