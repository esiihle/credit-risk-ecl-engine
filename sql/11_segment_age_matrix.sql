-- =============================================================================
-- segment_age_matrix — transition matrices by segment AND age band
-- =============================================================================
-- Phase 2 produced roll rates cut by segment OR by age. Neither is enough here.
--
-- The true hazard depends on both: a subprime account at the seasoning peak is
-- a different risk from a subprime account in its final year, and the pooled
-- matrix is an average that describes neither. A PD curve built from the pooled
-- matrix therefore carries a systematic error that varies with an account's
-- age — which is exactly the sort of error that cancels in aggregate and
-- misprices every individual account.
--
-- So the cut is JOINT: one matrix per (segment, age band).
--
-- WHY BANDS RATHER THAN EXACT MONTHS
--
-- A matrix per exact month on book would track seasoning perfectly and be
-- estimated on almost nothing — the deep arrears cells would hold a handful of
-- transitions each and the resulting rates would be noise. Banding trades a
-- little resolution for estimates that hold still. The bands here are the same
-- ones Phase 2 used, so the two phases cut the book identically.
--
-- `min_transitions` guards the other direction: a cell estimated on too few
-- moves falls back to the segment's pooled rate rather than being believed.
-- Without that fallback a single stray transition in a thin cell becomes a
-- 100% roll rate, and the recursive walk in the next file compounds it.
-- =============================================================================

CREATE OR REPLACE TABLE segment_age_matrix AS
WITH banded AS (
    SELECT
        a.segment,
        CASE
            WHEN t.months_on_book <  6 THEN '1. 0-5m'
            WHEN t.months_on_book < 12 THEN '2. 6-11m'
            WHEN t.months_on_book < 24 THEN '3. 12-23m'
            WHEN t.months_on_book < 36 THEN '4. 24-35m'
            ELSE                            '5. 36m+'
        END                                                   AS age_band,
        t.from_bucket,
        t.to_bucket
    FROM bucket_transition t
    JOIN dim_account a ON a.account_id = t.account_id
),
cell_counts AS (
    SELECT segment, age_band, from_bucket, to_bucket, COUNT(*) AS transitions
    FROM banded
    GROUP BY segment, age_band, from_bucket, to_bucket
),
row_totals AS (
    SELECT segment, age_band, from_bucket, SUM(transitions) AS opportunities
    FROM cell_counts
    GROUP BY segment, age_band, from_bucket
),
-- The fallback: the segment's rate pooled over all ages, used where a
-- (segment, age, bucket) cell is too thin to estimate from.
segment_pooled AS (
    SELECT
        segment, from_bucket, to_bucket,
        CAST(COUNT(*) AS DOUBLE) / SUM(COUNT(*)) OVER (
            PARTITION BY segment, from_bucket
        )                                                     AS pooled_rate
    FROM banded
    GROUP BY segment, from_bucket, to_bucket
)
SELECT
    c.segment,
    c.age_band,
    c.from_bucket,
    c.to_bucket,
    c.transitions,
    t.opportunities,

    -- The estimate actually used: the cell's own rate where there is enough
    -- behind it, the segment's pooled rate otherwise.
    CASE
        WHEN t.opportunities >= CAST($min_transitions AS INTEGER)
            THEN CAST(c.transitions AS DOUBLE) / t.opportunities
        ELSE COALESCE(p.pooled_rate, 0.0)
    END                                                       AS rate,

    t.opportunities < CAST($min_transitions AS INTEGER)       AS used_fallback
FROM cell_counts c
JOIN row_totals t
  ON t.segment = c.segment AND t.age_band = c.age_band
 AND t.from_bucket = c.from_bucket
LEFT JOIN segment_pooled p
  ON p.segment = c.segment AND p.from_bucket = c.from_bucket
 AND p.to_bucket = c.to_bucket
ORDER BY c.segment, c.age_band, c.from_bucket, c.to_bucket;
