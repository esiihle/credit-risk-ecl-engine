-- =============================================================================
-- ecl_account — expected credit loss, per account
-- =============================================================================
-- Everything the previous five phases built, multiplied together:
--
--     ECL = SUM over t of  marginal_PD(t) x LGD x EAD(t) x discount(t)
--
-- `marginal_PD(t)` is the probability of defaulting IN month t having survived
-- to t-1 — NOT the cumulative PD. Using the cumulative figure would count the
-- same default repeatedly, once in every month after it happened, and would
-- overstate the provision badly on long horizons.
--
-- -----------------------------------------------------------------------------
-- THE HORIZON IS THE ACCOUNTING JUDGEMENT, AND IT IS WHERE IFRS 9 LIVES
-- -----------------------------------------------------------------------------
-- The arithmetic above is identical for every account. What differs is how far
-- the sum runs, and that is decided by the stage set in Phase 3:
--
--   Stage 1  12-month ECL      t = 1 .. min(12, remaining term)
--   Stage 2  lifetime ECL      t = 1 .. remaining term
--   Stage 3  lifetime ECL      credit-impaired; see below
--
-- So an account's provision can multiply several times over without its credit
-- quality changing at all, purely by crossing the SICR threshold. That
-- discontinuity — the "staging cliff" — is the most-criticised feature of the
-- standard, and `17_ecl_portfolio.sql` measures how big it is on this book
-- rather than leaving it as an assertion.
--
-- Both horizons are therefore computed for EVERY account regardless of stage,
-- and `ecl_reported` picks the one the standard requires. Carrying the
-- counterfactual is what makes the cliff measurable.
--
-- -----------------------------------------------------------------------------
-- STAGE 3 TAKES A DIFFERENT FORMULA, NOT A LONGER HORIZON
-- -----------------------------------------------------------------------------
-- For an account already in default the sum above is zero, and correctly so:
-- default is absorbing, so there is no remaining probability of defaulting in
-- a FUTURE month. The loss is not expected, it is incurred:
--
--     ECL = LGD x EAD at the reporting date, undiscounted
--
-- Undiscounted because the loss has already arisen rather than being awaited in
-- some future month, and because the LGD it multiplies is not yet a
-- present-value measure either — discounting one side of the product and not
-- the other would be worse than discounting neither.
--
-- Getting this wrong is not a rounding matter. The answer key itself had the
-- same defect: it recorded every credit-impaired account at a 100% lifetime PD
-- and an ECL of exactly zero, leaving R939k of defaulted exposure with no
-- provision behind it. See methodology §7.2.
--
-- -----------------------------------------------------------------------------
-- WHICH ESTIMATES THIS CONSUMES
-- -----------------------------------------------------------------------------
--   PD   `pd_term_structure`, age-aware — the method Phase 4 measured as the
--        most accurate of the three, by a factor of nine per account.
--   LGD  `lgd_estimate.lgd_resolved_only` — resolved collections workouts only.
--        The simple mean rather than the exposure-weighted one, because here it
--        serves as each individual account's expected loss rate, and the simple
--        mean is the unbiased estimate of that. The exposure-weighted figure is
--        the right one for a portfolio total and is reported alongside it.
--   EAD  `ead_schedule` — the contractual amortisation, exact to the cent.
--   Disc `ead_schedule.discount_factor` — at the effective interest rate, as
--        IFRS 9 requires.
--
-- Nothing here reads `true_lgd`, `true_pd_*` or `true_ecl_*`. The answer key is
-- joined on at the end for comparison only, and never feeds the calculation.
--
-- GRAIN: one row per account on book at the reporting date.
-- =============================================================================

CREATE OR REPLACE TABLE ecl_account AS
WITH monthly AS (
    -- The loss expected in each individual future month, per account. This is
    -- the row-level working, kept as a CTE so the sums below are obviously the
    -- sums of something inspectable.
    SELECT
        a.account_id,
        a.stage,
        e.horizon,
        m.marginal_pd,
        e.ead,
        e.discount_factor,
        l.lgd_resolved_only                                     AS lgd,
        m.marginal_pd * l.lgd_resolved_only * e.ead * e.discount_factor
                                                                AS expected_loss
    FROM account_pd a
    JOIN ead_schedule e
      ON e.account_id = a.account_id
    JOIN pd_term_structure m
      ON m.segment      = a.segment
     AND m.start_age    = a.months_on_book
     AND m.start_bucket = a.arrears_bucket
     AND m.horizon      = e.horizon
    JOIN lgd_estimate l
      ON l.segment = a.segment
    -- Horizon 0 is the anchor of the recursive walk, not a month in which a
    -- loss can arise. Excluded explicitly rather than relied upon to be zero.
    WHERE e.horizon >= 1
),
summed AS (
    SELECT
        account_id,
        SUM(expected_loss)                                      AS ecl_lifetime,
        SUM(CASE WHEN horizon <= CAST($horizon_stage_1 AS INTEGER)
                 THEN expected_loss ELSE 0.0 END)               AS ecl_12m,
        -- Diagnostics: the pieces, so a surprising provision can be traced to
        -- whichever input produced it instead of being re-derived by hand.
        SUM(marginal_pd)                                        AS pd_lifetime_summed,
        MAX(lgd)                                                AS lgd_applied,
        MIN(discount_factor)                                    AS furthest_discount,
        COUNT(*)                                                AS months_in_sum
    FROM monthly
    GROUP BY account_id
)
SELECT
    a.account_id,
    a.reporting_date,
    a.segment,
    a.arrears_bucket,
    a.stage,
    a.months_on_book,
    a.months_remaining,
    a.scheduled_balance                                         AS ead_now,

    s.lgd_applied,
    s.months_in_sum,
    s.furthest_discount,

    -- The two horizons, for every account, whatever its stage.
    CASE WHEN a.stage = 3 THEN s.lgd_applied * a.scheduled_balance
         ELSE COALESCE(s.ecl_12m, 0.0) END                      AS ecl_12m,
    CASE WHEN a.stage = 3 THEN s.lgd_applied * a.scheduled_balance
         ELSE COALESCE(s.ecl_lifetime, 0.0) END                 AS ecl_lifetime,

    -- What IFRS 9 requires be carried on the balance sheet for this account.
    CASE
        WHEN a.stage = 3 THEN s.lgd_applied * a.scheduled_balance
        WHEN a.stage = 2 THEN COALESCE(s.ecl_lifetime, 0.0)
        ELSE                  COALESCE(s.ecl_12m, 0.0)
    END                                                         AS ecl_reported,

    CASE WHEN a.stage = 1 THEN 'ECL 12-month'
         ELSE 'ECL lifetime' END                                AS ecl_basis,

    -- The answer key, for comparison only. Never an input above.
    g.true_ecl_12m,
    g.true_ecl_lifetime,
    g.true_lgd,
    g.true_pd_lifetime,
    g.true_balance
FROM account_pd a
LEFT JOIN summed s     ON s.account_id = a.account_id
LEFT JOIN ground_truth g ON g.account_id = a.account_id
ORDER BY a.account_id;
