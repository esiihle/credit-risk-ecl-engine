-- =============================================================================
-- ecl_portfolio, the provision as a credit committee would read it
-- =============================================================================
-- Per-account ECL is the engine's output. A coverage ratio by stage is what
-- anyone actually looks at, because it is the number that has to be explained
-- when it moves.
--
-- -----------------------------------------------------------------------------
-- THE STAGING CLIFF, MEASURED RATHER THAN ASSERTED
-- -----------------------------------------------------------------------------
-- IFRS 9's most-criticised feature is that the provision jumps discontinuously
-- when an account crosses the SICR threshold: the horizon switches from 12
-- months to lifetime, and the loss allowance multiplies, with no change in the
-- account's credit quality beyond having crossed a line.
--
-- `16_ecl_account.sql` computes BOTH horizons for every account regardless of
-- stage, so the size of that jump is a measurable property of this book rather
-- than a point of theory. `cliff_multiple` below is, for the accounts actually
-- in Stage 2, what their provision is as a multiple of what it would have been
-- had they stayed in Stage 1, the same accounts, the same credit quality, the
-- same month, counted two ways.
--
-- GRAIN: one row per (stage, segment), plus rollup rows. `grouping_level` says
-- which, so a consumer never double-counts by summing across levels.
-- =============================================================================

CREATE OR REPLACE TABLE ecl_portfolio AS
WITH base AS (
    SELECT
        stage,
        segment,
        ead_now,
        ecl_reported,
        ecl_12m,
        ecl_lifetime,
        true_ecl_reported
    FROM ecl_reconciliation
),
by_stage_segment AS (
    SELECT 'stage x segment' AS grouping_level,
           CAST(stage AS VARCHAR) AS stage, segment, * EXCLUDE (stage, segment)
    FROM (SELECT stage, segment,
                 COUNT(*) AS accounts, SUM(ead_now) AS exposure,
                 SUM(ecl_reported) AS ecl, SUM(true_ecl_reported) AS true_ecl,
                 SUM(ecl_12m) AS ecl_12m_all, SUM(ecl_lifetime) AS ecl_lifetime_all
          FROM base GROUP BY stage, segment)
),
by_stage AS (
    SELECT 'stage' AS grouping_level,
           CAST(stage AS VARCHAR) AS stage, 'all' AS segment, * EXCLUDE (stage)
    FROM (SELECT stage,
                 COUNT(*) AS accounts, SUM(ead_now) AS exposure,
                 SUM(ecl_reported) AS ecl, SUM(true_ecl_reported) AS true_ecl,
                 SUM(ecl_12m) AS ecl_12m_all, SUM(ecl_lifetime) AS ecl_lifetime_all
          FROM base GROUP BY stage)
),
total AS (
    SELECT 'portfolio' AS grouping_level, 'all' AS stage, 'all' AS segment,
           COUNT(*) AS accounts, SUM(ead_now) AS exposure,
           SUM(ecl_reported) AS ecl, SUM(true_ecl_reported) AS true_ecl,
           SUM(ecl_12m) AS ecl_12m_all, SUM(ecl_lifetime) AS ecl_lifetime_all
    FROM base
),
stacked AS (
    SELECT * FROM by_stage_segment
    UNION ALL SELECT * FROM by_stage
    UNION ALL SELECT * FROM total
)
SELECT
    grouping_level,
    stage,
    segment,
    accounts,
    exposure,
    ecl,
    true_ecl,
    ecl / NULLIF(exposure, 0)                              AS coverage_ratio,
    true_ecl / NULLIF(exposure, 0)                         AS true_coverage_ratio,
    ecl / NULLIF(true_ecl, 0) - 1.0                        AS relative_error,

    -- The cliff: lifetime provision as a multiple of the 12-month one, for the
    -- same accounts. Meaningful for Stage 2 (where the switch has happened) and
    -- for Stage 1 (where it is the jump those accounts would take on crossing).
    ecl_lifetime_all / NULLIF(ecl_12m_all, 0)              AS cliff_multiple,
    ecl_12m_all,
    ecl_lifetime_all
FROM stacked
ORDER BY grouping_level, stage, segment;
