-- =============================================================================
-- account_pd — 12-month and lifetime PD for every account at the reporting date
-- =============================================================================
-- The point where a curve becomes a number per account, and the input Phase 6
-- discounts into a provision.
--
-- TWO HORIZONS, because IFRS 9 needs both and the staging decides which is used:
--
--   pd_12m        P(default in the next 12 months). Stage 1's horizon. Capped
--                 at the remaining term — a loan with 7 months left cannot
--                 default in month 9, and reading a 12-month PD off a curve
--                 without that cap credits the account with risk it cannot
--                 run.
--
--   pd_lifetime   P(default before maturity). Stage 2 and 3's horizon.
--
-- THREE ESTIMATES, deliberately kept side by side rather than collapsing to the
-- best one. Carrying all three is what makes the validation in `src/pd.py` a
-- comparison rather than an assertion — the question "is the extra
-- segmentation worth it?" cannot be answered by a table that holds only the
-- answer you already chose.
--
--   pooled        Phase 3's single matrix. One curve for the whole book.
--   segmented     by segment, pooled over age.
--   age_aware     by segment AND age band, with the matrix changing as the
--                 walk advances.
--
-- The `true_` columns come from the generator's answer key. They are carried
-- here for VALIDATION ONLY and no estimate reads them — the boundary is the
-- reason the book is simulated at all.
-- =============================================================================

CREATE OR REPLACE TABLE account_pd AS
WITH live AS (
    SELECT
        s.account_id,
        s.reporting_date,
        s.segment,
        s.arrears_bucket,
        s.months_on_book,
        s.months_remaining,
        s.scheduled_balance,
        s.stage,
        -- The 12-month horizon never runs past maturity.
        LEAST(12, GREATEST(s.months_remaining, 0))            AS horizon_12m,
        GREATEST(s.months_remaining, 0)                       AS horizon_lifetime
    FROM account_stage s
    WHERE s.reporting_date = CAST($reporting_date AS DATE)
      AND s.months_remaining > 0
)
SELECT
    l.account_id,
    l.reporting_date,
    l.segment,
    l.arrears_bucket,
    l.months_on_book,
    l.months_remaining,
    l.scheduled_balance,
    l.stage,
    l.horizon_12m,
    l.horizon_lifetime,

    -- --- the three estimates ---------------------------------------------
    COALESCE(pooled_12.cumulative_pd, 0.0)        AS pd_12m_pooled,
    COALESCE(pooled_life.cumulative_pd, 0.0)      AS pd_lifetime_pooled,

    COALESCE(seg_12.cumulative_pd, 0.0)           AS pd_12m_segmented,
    COALESCE(seg_life.cumulative_pd, 0.0)         AS pd_lifetime_segmented,

    COALESCE(age_12.cumulative_pd, 0.0)           AS pd_12m_age_aware,
    COALESCE(age_life.cumulative_pd, 0.0)         AS pd_lifetime_age_aware,

    -- --- the answer key, for validation only -------------------------------
    g.true_pd_12m,
    g.true_pd_lifetime
FROM live l

-- Pooled: Phase 3's curve, no segment, no age.
LEFT JOIN pd_curve pooled_12
       ON pooled_12.start_bucket = l.arrears_bucket
      AND pooled_12.horizon      = l.horizon_12m
LEFT JOIN pd_curve pooled_life
       ON pooled_life.start_bucket = l.arrears_bucket
      AND pooled_life.horizon      = l.horizon_lifetime

-- Segmented: the age-aware table read at age 0, which is the segment's curve
-- pooled over ages only in the sense that every walk starts from the first
-- band. Kept distinct from `pooled` so the two effects can be separated.
LEFT JOIN pd_term_structure seg_12
       ON seg_12.segment      = l.segment
      AND seg_12.start_age    = 0
      AND seg_12.start_bucket = l.arrears_bucket
      AND seg_12.horizon      = l.horizon_12m
LEFT JOIN pd_term_structure seg_life
       ON seg_life.segment      = l.segment
      AND seg_life.start_age    = 0
      AND seg_life.start_bucket = l.arrears_bucket
      AND seg_life.horizon      = l.horizon_lifetime

-- Age-aware: the account's own segment AND its own age on book.
LEFT JOIN pd_term_structure age_12
       ON age_12.segment      = l.segment
      AND age_12.start_age    = l.months_on_book
      AND age_12.start_bucket = l.arrears_bucket
      AND age_12.horizon      = l.horizon_12m
LEFT JOIN pd_term_structure age_life
       ON age_life.segment      = l.segment
      AND age_life.start_age    = l.months_on_book
      AND age_life.start_bucket = l.arrears_bucket
      AND age_life.horizon      = l.horizon_lifetime

LEFT JOIN ground_truth g ON g.account_id = l.account_id;
