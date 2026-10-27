-- =============================================================================
-- roll_rate — the pooled transition matrix, estimated from observed moves
-- =============================================================================
-- One row per (from_bucket, to_bucket) pair that was observed, with the count
-- and the rate. Rates are conditional on the starting bucket, so they sum to 1
-- across each from_bucket — which is what makes this a transition matrix
-- rather than a table of proportions.
--
-- This is the empirical counterpart to the matrix the generator used. The two
-- are produced by completely different routes — one counts what happened in
-- 500,000 account-months, the other is the matrix those months were drawn from
-- — so agreement between them is evidence that the estimation is right.
--
-- WHAT A POOLED MATRIX IS AND IS NOT
--
-- The true matrix varies by segment (riskier borrowers miss more often) and by
-- month on book (seasoning). Pooling across both produces a single matrix that
-- is the EXPOSURE-WEIGHTED AVERAGE of them — a real and useful quantity, and
-- the one a book-level MI pack reports, but not the matrix of any individual
-- account.
--
-- `07_roll_rate_segmented.sql` cuts it the ways that matter. The validation in
-- `src/rollrate.py` compares this pooled matrix against the weighted average of
-- the true matrices, which is the only like-for-like comparison available.
-- =============================================================================

CREATE OR REPLACE TABLE roll_rate AS
WITH counted AS (
    SELECT
        from_bucket,
        to_bucket,
        COUNT(*)                   AS transitions,
        SUM(from_balance)          AS balance_moved
    FROM bucket_transition
    GROUP BY from_bucket, to_bucket
),
totals AS (
    SELECT
        from_bucket,
        SUM(transitions)           AS opportunities,
        SUM(balance_moved)         AS balance_at_risk
    FROM counted
    GROUP BY from_bucket
)
SELECT
    c.from_bucket,
    c.to_bucket,
    c.transitions,
    t.opportunities,

    -- Account-weighted: the probability that an account in this bucket moves
    -- to that one. This is the transition probability the PD model needs.
    CAST(c.transitions AS DOUBLE) / t.opportunities           AS rate,

    -- Balance-weighted: the share of EXPOSURE that moves. Differs from the
    -- account rate whenever large and small accounts behave differently, and
    -- it is the figure a provision is actually sensitive to.
    c.balance_moved / NULLIF(t.balance_at_risk, 0)            AS balance_rate,

    CASE
        WHEN c.to_bucket > c.from_bucket THEN 'roll forward'
        WHEN c.to_bucket < c.from_bucket THEN 'cure'
        ELSE                                  'stay'
    END                                                       AS movement
FROM counted c
JOIN totals t ON t.from_bucket = c.from_bucket
ORDER BY c.from_bucket, c.to_bucket;
