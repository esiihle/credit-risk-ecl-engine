-- =============================================================================
-- ecl_reconciliation, is the ECL ARITHMETIC right?
-- =============================================================================
-- A separate question from "is the ECL accurate", and the two were conflated by
-- a single 2% tolerance until Phase 6 pulled them apart.
--
-- ECL is a product of three estimated inputs, so to first order its relative
-- error is the SUM of their relative errors. Phase 4's gate permits 15%
-- relative error on lifetime PD and Phase 5's permits 2pp on LGD. A correctly
-- formed ECL sum over inputs carrying 6% PD error therefore CANNOT be within
-- 2% of truth, and demanding that it be was not a gate on this phase's work at
-- all, it was a second, 7.5x stricter, implicit gate on Phase 4's.
--
-- Worse, it fails in both directions. A sum formed WRONGLY, cumulative PD
-- instead of marginal, the wrong horizon for a stage, no discounting, can
-- still land within a few percent on a short book and pass.
--
-- So this file feeds the engine's own ECL expression the EXACT inputs from
-- `ground_truth_monthly` and requires it to reproduce the answer key to the
-- cent. What it tests is only the thing Phase 6 builds:
--
--   * marginal probability of default, not cumulative
--   * the horizon each IFRS 9 stage dictates
--   * discounting at the effective interest rate
--   * the separate formula credit-impaired accounts need
--
-- If this reconciles exactly and the estimated-input ECL is a few percent off,
-- the arithmetic is right and the inputs are as good as their own phases said
-- they were. That is a far more useful thing to be able to say than one number
-- within a tolerance.
--
-- GRAIN: one row per account on book at the reporting date.
-- =============================================================================

CREATE OR REPLACE TABLE ecl_reconciliation AS
WITH from_true_inputs AS (
    -- The engine's formula, with every estimate replaced by its true value.
    -- Deliberately written to mirror `16_ecl_account.sql` line for line: if
    -- the two expressions ever diverge, this check stops being a check on that
    -- file and becomes a check on a copy of it.
    SELECT
        a.account_id,
        SUM(t.true_marginal_pd * g.true_lgd * t.true_ead * t.true_discount_factor)
                                                            AS ecl_lifetime_true_inputs,
        SUM(CASE WHEN t.horizon <= CAST($horizon_stage_1 AS INTEGER)
                 THEN t.true_marginal_pd * g.true_lgd
                      * t.true_ead * t.true_discount_factor
                 ELSE 0.0 END)                              AS ecl_12m_true_inputs
    FROM account_pd a
    JOIN ground_truth_monthly t ON t.account_id = a.account_id
    JOIN ground_truth g         ON g.account_id = a.account_id
    WHERE t.horizon >= 1
    GROUP BY a.account_id
)
SELECT
    e.account_id,
    e.segment,
    e.stage,
    e.ead_now,
    e.months_remaining,

    -- Stage 3 takes the impaired formula here too, for the same reason it does
    -- in the engine: there is no future month in which the default occurs.
    CASE WHEN e.stage = 3 THEN e.true_lgd * e.ead_now
         ELSE COALESCE(f.ecl_12m_true_inputs, 0.0) END      AS ecl_12m_true_inputs,
    CASE WHEN e.stage = 3 THEN e.true_lgd * e.ead_now
         ELSE COALESCE(f.ecl_lifetime_true_inputs, 0.0) END AS ecl_lifetime_true_inputs,

    e.true_ecl_12m,
    e.true_ecl_lifetime,

    -- The arithmetic residuals: these are what must be zero.
    ABS(CASE WHEN e.stage = 3 THEN e.true_lgd * e.ead_now
             ELSE COALESCE(f.ecl_12m_true_inputs, 0.0) END
        - e.true_ecl_12m)                                   AS residual_12m,
    ABS(CASE WHEN e.stage = 3 THEN e.true_lgd * e.ead_now
             ELSE COALESCE(f.ecl_lifetime_true_inputs, 0.0) END
        - e.true_ecl_lifetime)                              AS residual_lifetime,

    -- And the engine's own figures, for the accuracy question.
    e.ecl_12m,
    e.ecl_lifetime,
    e.ecl_reported,
    CASE WHEN e.stage = 1 THEN e.true_ecl_12m
         ELSE e.true_ecl_lifetime END                       AS true_ecl_reported
FROM ecl_account e
LEFT JOIN from_true_inputs f ON f.account_id = e.account_id
ORDER BY e.account_id;
