-- =============================================================================
-- pd_term_structure — PD curves that know the account's segment AND its age
-- =============================================================================
-- Phase 3's `pd_curve` propagated ONE pooled matrix. That matrix is an average
-- over segments and over ages, so the curve it produces describes no actual
-- account: it overstates risk for a prime account and understates it for a
-- subprime one, and it misses seasoning entirely.
--
-- This file fixes both. The walk starts from a given (segment, age, bucket) and
-- **changes matrix as it advances**, because an account that is 10 months old
-- today is 22 months old a year from now and faces a different hazard then.
-- That is the part a single matrix power cannot express, and the reason this is
-- a recursive CTE with a join inside the recursive term rather than
-- exponentiation.
--
-- GRAIN: one row per (segment, start_age, start_bucket, horizon).
--
-- Same two traps as Phase 3's curve, and they bite the same way here:
--   * every literal in the anchor is cast to DOUBLE, or DECIMAL(2,1) freezes
--     the state vector at one decimal place and the curve silently stops moving
--   * the absorbing row is asserted as the identity, because no transition out
--     of default is ever observed and coalescing those NULLs to zero makes
--     probability mass vanish
--
-- See `08_pd_curve.sql` for the worked demonstration of both.
-- =============================================================================

CREATE OR REPLACE TABLE pd_term_structure AS
WITH RECURSIVE
wide AS (
    -- One row per (segment, age band): the 5x5 matrix laid out flat so the
    -- recursive term can do arithmetic without aggregating.
    SELECT
        segment,
        age_band,
        COALESCE(MAX(CASE WHEN from_bucket=0 AND to_bucket=0 THEN rate END), 0.0)::DOUBLE AS m00,
        COALESCE(MAX(CASE WHEN from_bucket=0 AND to_bucket=1 THEN rate END), 0.0)::DOUBLE AS m01,
        COALESCE(MAX(CASE WHEN from_bucket=0 AND to_bucket=2 THEN rate END), 0.0)::DOUBLE AS m02,
        COALESCE(MAX(CASE WHEN from_bucket=0 AND to_bucket=3 THEN rate END), 0.0)::DOUBLE AS m03,
        COALESCE(MAX(CASE WHEN from_bucket=0 AND to_bucket=4 THEN rate END), 0.0)::DOUBLE AS m04,
        COALESCE(MAX(CASE WHEN from_bucket=1 AND to_bucket=0 THEN rate END), 0.0)::DOUBLE AS m10,
        COALESCE(MAX(CASE WHEN from_bucket=1 AND to_bucket=1 THEN rate END), 0.0)::DOUBLE AS m11,
        COALESCE(MAX(CASE WHEN from_bucket=1 AND to_bucket=2 THEN rate END), 0.0)::DOUBLE AS m12,
        COALESCE(MAX(CASE WHEN from_bucket=1 AND to_bucket=3 THEN rate END), 0.0)::DOUBLE AS m13,
        COALESCE(MAX(CASE WHEN from_bucket=1 AND to_bucket=4 THEN rate END), 0.0)::DOUBLE AS m14,
        COALESCE(MAX(CASE WHEN from_bucket=2 AND to_bucket=0 THEN rate END), 0.0)::DOUBLE AS m20,
        COALESCE(MAX(CASE WHEN from_bucket=2 AND to_bucket=1 THEN rate END), 0.0)::DOUBLE AS m21,
        COALESCE(MAX(CASE WHEN from_bucket=2 AND to_bucket=2 THEN rate END), 0.0)::DOUBLE AS m22,
        COALESCE(MAX(CASE WHEN from_bucket=2 AND to_bucket=3 THEN rate END), 0.0)::DOUBLE AS m23,
        COALESCE(MAX(CASE WHEN from_bucket=2 AND to_bucket=4 THEN rate END), 0.0)::DOUBLE AS m24,
        COALESCE(MAX(CASE WHEN from_bucket=3 AND to_bucket=0 THEN rate END), 0.0)::DOUBLE AS m30,
        COALESCE(MAX(CASE WHEN from_bucket=3 AND to_bucket=1 THEN rate END), 0.0)::DOUBLE AS m31,
        COALESCE(MAX(CASE WHEN from_bucket=3 AND to_bucket=2 THEN rate END), 0.0)::DOUBLE AS m32,
        COALESCE(MAX(CASE WHEN from_bucket=3 AND to_bucket=3 THEN rate END), 0.0)::DOUBLE AS m33,
        COALESCE(MAX(CASE WHEN from_bucket=3 AND to_bucket=4 THEN rate END), 0.0)::DOUBLE AS m34,
        -- Absorbing: asserted, never estimated.
        0.0::DOUBLE AS m40, 0.0::DOUBLE AS m41, 0.0::DOUBLE AS m42,
        0.0::DOUBLE AS m43, 1.0::DOUBLE AS m44
    FROM segment_age_matrix
    GROUP BY segment, age_band
),
starts AS (
    SELECT
        s.segment,
        ages.start_age,
        buckets.start_bucket
    FROM (SELECT DISTINCT segment FROM segment_age_matrix) s
    CROSS JOIN (SELECT unnest(generate_series(0, CAST($max_age AS INTEGER))) AS start_age) ages
    CROSS JOIN (SELECT unnest([0, 1, 2, 3, 4]) AS start_bucket) buckets
),
walk AS (
    SELECT
        st.segment,
        st.start_age,
        st.start_bucket,
        0 AS horizon,
        CASE WHEN st.start_bucket = 0 THEN 1.0 ELSE 0.0 END::DOUBLE AS p0,
        CASE WHEN st.start_bucket = 1 THEN 1.0 ELSE 0.0 END::DOUBLE AS p1,
        CASE WHEN st.start_bucket = 2 THEN 1.0 ELSE 0.0 END::DOUBLE AS p2,
        CASE WHEN st.start_bucket = 3 THEN 1.0 ELSE 0.0 END::DOUBLE AS p3,
        CASE WHEN st.start_bucket = 4 THEN 1.0 ELSE 0.0 END::DOUBLE AS p4
    FROM starts st

    UNION ALL

    SELECT
        w.segment,
        w.start_age,
        w.start_bucket,
        w.horizon + 1,
        (w.p0*m.m00 + w.p1*m.m10 + w.p2*m.m20 + w.p3*m.m30 + w.p4*m.m40)::DOUBLE,
        (w.p0*m.m01 + w.p1*m.m11 + w.p2*m.m21 + w.p3*m.m31 + w.p4*m.m41)::DOUBLE,
        (w.p0*m.m02 + w.p1*m.m12 + w.p2*m.m22 + w.p3*m.m32 + w.p4*m.m42)::DOUBLE,
        (w.p0*m.m03 + w.p1*m.m13 + w.p2*m.m23 + w.p3*m.m33 + w.p4*m.m43)::DOUBLE,
        (w.p0*m.m04 + w.p1*m.m14 + w.p2*m.m24 + w.p3*m.m34 + w.p4*m.m44)::DOUBLE
    FROM walk w
    JOIN wide m
      ON m.segment = w.segment
     -- THE AGE-AWARE PART: the matrix is chosen by the account's age AT THIS
     -- STEP, not at the start of the walk. An account 10 months old today
     -- faces the 12-23 month hazard a year from now.
     AND m.age_band = CASE
            WHEN w.start_age + w.horizon <  6 THEN '1. 0-5m'
            WHEN w.start_age + w.horizon < 12 THEN '2. 6-11m'
            WHEN w.start_age + w.horizon < 24 THEN '3. 12-23m'
            WHEN w.start_age + w.horizon < 36 THEN '4. 24-35m'
            ELSE                                   '5. 36m+'
         END
    WHERE w.horizon < CAST($max_horizon AS INTEGER)
)
SELECT
    segment,
    start_age,
    start_bucket,
    horizon,
    p4                                   AS cumulative_pd,
    p4 - LAG(p4, 1, 0.0::DOUBLE) OVER (
        PARTITION BY segment, start_age, start_bucket ORDER BY horizon
    )                                    AS marginal_pd,
    p0 + p1 + p2 + p3                    AS surviving
FROM walk
ORDER BY segment, start_age, start_bucket, horizon;
