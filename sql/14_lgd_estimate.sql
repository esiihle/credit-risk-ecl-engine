-- =============================================================================
-- lgd_estimate, loss given default, estimated from observed recoveries
-- =============================================================================
-- LGD is the one genuinely uncertain input in this engine. Exposure is
-- arithmetic. PD is estimated from half a million observed transitions. LGD
-- rests on a few hundred defaults, each of which takes a year to resolve.
--
-- -----------------------------------------------------------------------------
-- THE WORKOUT CENSORING TRAP, the central idea of this phase
-- -----------------------------------------------------------------------------
-- An account that defaulted last month has barely begun its collections
-- workout. One that defaulted three years ago is finished. Both sit in the
-- same table, and the first looks catastrophic:
--
--     observed loss = 1 - (recovered SO FAR) / (exposure at default)
--
-- For an unresolved case that is not LGD. It is LGD plus however much is still
-- to come, and it is biased upward by construction.
--
-- This is the same shape as the Phase 2 roll-rate trap but with a different
-- mechanism. There the bias came from the END of an account's series; here it
-- comes from the BEGINNING of a workout. In both cases the naive denominator
-- includes observations whose outcome is not yet known, and in both cases the
-- bias runs one way.
--
-- So the estimate uses COMPLETE workouts only. Incomplete ones are counted and
-- reported, they are the exposure the estimate cannot speak for, but they do
-- not enter the average.
--
-- The cost is sample size, and on a book this size the cost is real: excluding
-- incomplete workouts throws away roughly a third of the defaults. That is the
-- trade, and it is the right side of it. An estimate built on fewer, resolved
-- cases is noisy; one built on more, unresolved cases is wrong.
--
-- -----------------------------------------------------------------------------
-- A FLOOR AND A CAP
-- -----------------------------------------------------------------------------
-- Realised LGD can fall outside [0, 1]: a recovery exceeding the exposure gives
-- a negative loss, and collection costs can push a loss above 100%. Neither is
-- an error. Both are clipped here because an LGD outside [0, 1] breaks the ECL
-- arithmetic downstream, and the count of clipped cases is reported rather than
-- hidden.
-- =============================================================================

CREATE OR REPLACE TABLE lgd_estimate AS
WITH resolved AS (
    SELECT
        r.account_id,
        a.segment,
        r.ead_at_default,
        r.recovery_to_date,
        r.months_in_workout,
        r.workout_complete,
        -- Clipped to [0, 1]: see the note above.
        LEAST(GREATEST(1.0 - r.recovery_to_date / NULLIF(r.ead_at_default, 0),
                       0.0), 1.0)                              AS realised_lgd,
        (1.0 - r.recovery_to_date / NULLIF(r.ead_at_default, 0)) < 0.0
            OR (1.0 - r.recovery_to_date / NULLIF(r.ead_at_default, 0)) > 1.0
                                                               AS was_clipped
    FROM recovery r
    JOIN dim_account a ON a.account_id = r.account_id
    WHERE r.ead_at_default > 0
),
by_segment AS (
    SELECT
        segment,

        -- The estimate: resolved workouts only.
        COUNT(*) FILTER (WHERE workout_complete)               AS resolved_defaults,
        AVG(realised_lgd) FILTER (WHERE workout_complete)      AS lgd_resolved_only,

        -- Exposure-weighted, which is what a provision is sensitive to: a few
        -- large defaults move the loss more than many small ones.
        SUM(realised_lgd * ead_at_default) FILTER (WHERE workout_complete)
            / NULLIF(SUM(ead_at_default) FILTER (WHERE workout_complete), 0)
                                                               AS lgd_exposure_weighted,

        -- The naive figure, carried deliberately so the bias can be shown
        -- rather than asserted. Never used downstream.
        COUNT(*)                                               AS all_defaults,
        AVG(realised_lgd)                                      AS lgd_all_defaults,

        COUNT(*) FILTER (WHERE NOT workout_complete)           AS unresolved_defaults,
        AVG(realised_lgd) FILTER (WHERE NOT workout_complete)  AS lgd_unresolved_only,

        SUM(CASE WHEN was_clipped THEN 1 ELSE 0 END)           AS clipped,
        STDDEV_SAMP(realised_lgd) FILTER (WHERE workout_complete)
                                                               AS lgd_stddev
    FROM resolved
    GROUP BY segment
)
SELECT
    segment,
    resolved_defaults,
    unresolved_defaults,
    all_defaults,
    lgd_resolved_only,
    lgd_exposure_weighted,
    lgd_all_defaults,
    lgd_unresolved_only,
    clipped,
    lgd_stddev,
    -- The standard error on the estimate. LGD is estimated on hundreds of
    -- cases rather than hundreds of thousands, so quoting it without an
    -- interval invites more confidence than the data supports.
    lgd_stddev / NULLIF(SQRT(resolved_defaults), 0)            AS standard_error,
    lgd_all_defaults - lgd_resolved_only                       AS censoring_bias
FROM by_segment
ORDER BY lgd_resolved_only;
