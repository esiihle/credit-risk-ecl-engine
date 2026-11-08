-- =============================================================================
-- vintage_comparison — the fair comparison, and the naive one beside it
-- =============================================================================
-- `19_vintage_curves.sql` builds the triangle. This file answers the question
-- the triangle is built to answer — "is our lending getting better or worse?" —
-- twice, once correctly and once the way it is usually done, so the difference
-- between them is a number rather than a warning.
--
--   fair   every cohort's cumulative default rate at a COMMON age, chosen as
--          the oldest age the YOUNGEST cohort in the comparison has reached.
--          Apples to apples: same elapsed life, same term mix, same segment
--          mix, so the only thing left to differ is cohort quality.
--
--   naive  every cohort's LATEST cumulative default rate — whatever it happens
--          to be today. This is what a vintage table shows when nobody thinks
--          about the observation window, and on a growing book it is a ramp
--          from high to low with no credit meaning at all.
--
-- The common age costs information: restricting 48 cohorts to a common 12
-- months throws away four years of observation on the oldest ones. That is the
-- trade. The alternative is not more information, it is a number that means
-- something different for every row.
--
-- For a comparison that keeps more of the history, the right move is to drop
-- the youngest cohorts rather than to extend the age — `$common_age` is a
-- parameter so both can be run, and `cohorts_included` records what the choice
-- cost.
--
-- GRAIN: one row per vintage.
-- =============================================================================

CREATE OR REPLACE TABLE vintage_comparison AS
WITH eligible AS (
    -- Cohorts that have actually lived to the comparison age. A cohort that
    -- has not is EXCLUDED rather than carried at its last known value, which
    -- is the whole point of the file.
    SELECT DISTINCT vintage_month, vintage_month_index, cohort_size, watched_to_age
    FROM vintage_curves
    WHERE watched_to_age >= CAST($common_age AS INTEGER)
),
fair AS (
    SELECT v.vintage_month,
           v.cumulative_default_rate                        AS rate_at_common_age,
           v.defaults_by_age                                AS defaults_at_common_age,
           v.standard_error                                 AS standard_error_at_common_age
    FROM vintage_curves v
    WHERE v.age = CAST($common_age AS INTEGER)
      AND NOT v.is_censored
),
naive AS (
    -- Each cohort at its own latest observed age: the comparison that is not
    -- one.
    SELECT v.vintage_month,
           v.age                                            AS latest_age,
           v.cumulative_default_rate                        AS rate_latest,
           v.defaults_by_age                                AS defaults_latest
    FROM vintage_curves v
    WHERE v.age = v.watched_to_age
)
SELECT
    e.vintage_month,
    e.vintage_month_index,
    e.cohort_size,
    e.watched_to_age,

    CAST($common_age AS INTEGER)                            AS common_age,
    f.rate_at_common_age,
    f.defaults_at_common_age,
    f.standard_error_at_common_age,

    n.latest_age,
    n.rate_latest,
    n.defaults_latest,

    -- How much of the naive figure is simply extra observation time.
    n.rate_latest - f.rate_at_common_age                    AS naive_excess,
    n.latest_age - CAST($common_age AS INTEGER)             AS extra_months_watched,

    (SELECT COUNT(*) FROM eligible)                         AS cohorts_included,
    (SELECT COUNT(DISTINCT vintage_month) FROM vintage_curves)
                                                            AS cohorts_total
FROM eligible e
JOIN fair  f ON f.vintage_month = e.vintage_month
JOIN naive n ON n.vintage_month = e.vintage_month
ORDER BY e.vintage_month_index;
