-- =============================================================================
-- pd_curve — cumulative probability of default, by starting bucket and horizon
-- =============================================================================
-- Propagates the Phase 2 transition matrix forward, month by month, using a
-- RECURSIVE CTE. The state vector (p0..p4) is multiplied by the matrix at each
-- step; the mass that has accumulated in bucket 4 after k steps is exactly
-- P(default within k months | started in bucket s).
--
-- This is matrix exponentiation written in SQL, and it is the empirical
-- counterpart of `generate.cumulative_pd_curve` — which does the same thing in
-- Python from the matrix the book was generated from. The two meet in
-- `tests/test_staging.py`.
--
-- -----------------------------------------------------------------------------
-- TWO TRAPS, both of which produce plausible numbers rather than errors.
-- -----------------------------------------------------------------------------
--
-- 1. DECIMAL TRUNCATION IN THE ANCHOR.
--
--    Writing `1.0 AS p0` in the anchor gives DECIMAL(2,1) — ONE decimal place.
--    The type propagates through every iteration, so each step rounds to a
--    tenth, and the state vector sticks at (0.9, 0.1) forever. The output is a
--    set of perfectly valid-looking probabilities that is completely wrong,
--    with no error and no warning.
--
--    Hence `::DOUBLE` on every literal in the anchor. It is not decoration.
--
-- 2. THE ABSORBING ROW IS ABSENT FROM THE DATA.
--
--    `roll_rate` is built from OBSERVED transitions, and nothing is ever
--    observed leaving default — it is absorbing, so there are no such rows.
--    Pivoting therefore yields NULL for the whole bucket-4 row, including its
--    diagonal.
--
--    Coalescing those NULLs to zero would mean an account that defaults is
--    multiplied by a row of zeros at the next step and its probability mass
--    VANISHES. Cumulative PD would then stop rising and could even fall — and
--    PD curves that plateau look superficially reasonable.
--
--    The absorbing row must be supplied as the identity row: m44 = 1. That is
--    a modelling fact, not a data fact, which is exactly why it has to be
--    written down rather than inferred from a table that cannot contain it.
-- =============================================================================

CREATE OR REPLACE TABLE pd_curve AS
WITH RECURSIVE
matrix AS (
    SELECT
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

        -- Bucket 4 is absorbing. No transition out of it is ever observed, so
        -- these cannot come from the data — the identity row is asserted.
        0.0::DOUBLE AS m40,
        0.0::DOUBLE AS m41,
        0.0::DOUBLE AS m42,
        0.0::DOUBLE AS m43,
        1.0::DOUBLE AS m44
    FROM roll_rate
),
starts AS (
    SELECT unnest([0, 1, 2, 3, 4]) AS start_bucket
),
walk AS (
    -- Horizon 0: all probability mass sits on the starting bucket.
    -- Every literal cast: see trap 1 above.
    SELECT
        s.start_bucket,
        0 AS horizon,
        CASE WHEN s.start_bucket = 0 THEN 1.0 ELSE 0.0 END::DOUBLE AS p0,
        CASE WHEN s.start_bucket = 1 THEN 1.0 ELSE 0.0 END::DOUBLE AS p1,
        CASE WHEN s.start_bucket = 2 THEN 1.0 ELSE 0.0 END::DOUBLE AS p2,
        CASE WHEN s.start_bucket = 3 THEN 1.0 ELSE 0.0 END::DOUBLE AS p3,
        CASE WHEN s.start_bucket = 4 THEN 1.0 ELSE 0.0 END::DOUBLE AS p4
    FROM starts s

    UNION ALL

    -- One month on: the state vector times the matrix.
    SELECT
        w.start_bucket,
        w.horizon + 1,
        (w.p0*m.m00 + w.p1*m.m10 + w.p2*m.m20 + w.p3*m.m30 + w.p4*m.m40)::DOUBLE,
        (w.p0*m.m01 + w.p1*m.m11 + w.p2*m.m21 + w.p3*m.m31 + w.p4*m.m41)::DOUBLE,
        (w.p0*m.m02 + w.p1*m.m12 + w.p2*m.m22 + w.p3*m.m32 + w.p4*m.m42)::DOUBLE,
        (w.p0*m.m03 + w.p1*m.m13 + w.p2*m.m23 + w.p3*m.m33 + w.p4*m.m43)::DOUBLE,
        (w.p0*m.m04 + w.p1*m.m14 + w.p2*m.m24 + w.p3*m.m34 + w.p4*m.m44)::DOUBLE
    FROM walk w
    CROSS JOIN matrix m
    WHERE w.horizon < CAST($max_horizon AS INTEGER)
)
SELECT
    start_bucket,
    horizon,
    p4                                   AS cumulative_pd,
    -- The marginal: the probability of defaulting IN this month, which is what
    -- the ECL calculation discounts. Non-negative by construction, because the
    -- chain is absorbing and the cumulative curve cannot fall.
    p4 - LAG(p4, 1, 0.0::DOUBLE) OVER (
        PARTITION BY start_bucket ORDER BY horizon
    )                                    AS marginal_pd,
    p0 + p1 + p2 + p3                    AS surviving,
    p0, p1, p2, p3, p4
FROM walk
ORDER BY start_bucket, horizon;
