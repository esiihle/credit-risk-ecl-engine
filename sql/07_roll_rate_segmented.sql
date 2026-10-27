-- =============================================================================
-- roll_rate_segmented — the same matrix, cut the ways that matter
-- =============================================================================
-- One long table rather than three wide ones, keyed by `cut_type` and
-- `cut_value`. A report filters to the cut it wants; adding a fourth cut later
-- adds rows, not a table and not a schema change.
--
-- The three cuts answer three different questions:
--
--   segment              Does the risk grading work? Roll-forward rates must
--                        order prime < near-prime < subprime. If they do not,
--                        the segmentation is decorative.
--
--   months on book       Seasoning. The true hazard peaks part-way through a
--                        loan's life, so a single pooled matrix is an average
--                        over ages and fits no particular account. This cut is
--                        what shows the pooled figure hiding that.
--
--   calendar quarter     STABILITY. This is the one a monitoring pack leads
--                        with: roll rates drifting quarter on quarter is the
--                        earliest visible sign that the book is changing, and
--                        it appears long before the default rate moves.
--
-- Small cells are the hazard here. A rate computed on nine accounts will swing
-- wildly and look like a trend, so `opportunities` is carried on every row and
-- every report that uses this table is expected to show it.
-- =============================================================================

CREATE OR REPLACE TABLE roll_rate_segmented AS
WITH labelled AS (
    SELECT
        t.*,
        a.segment,
        CASE
            WHEN t.months_on_book <  6 THEN '1. 0-5m'
            WHEN t.months_on_book < 12 THEN '2. 6-11m'
            WHEN t.months_on_book < 24 THEN '3. 12-23m'
            WHEN t.months_on_book < 36 THEN '4. 24-35m'
            ELSE                            '5. 36m+'
        END                                                   AS months_on_book_band,
        CAST(d.calendar_year AS VARCHAR) || ' Q'
            || CAST(d.calendar_quarter AS VARCHAR)            AS calendar_quarter
    FROM bucket_transition t
    JOIN dim_account a ON a.account_id = t.account_id
    JOIN dim_date    d ON d.date_key   = t.date_key
),
by_cut AS (
    SELECT 'segment' AS cut_type, segment AS cut_value,
           from_bucket, to_bucket, COUNT(*) AS transitions
    FROM labelled GROUP BY segment, from_bucket, to_bucket

    UNION ALL
    SELECT 'months_on_book_band', months_on_book_band,
           from_bucket, to_bucket, COUNT(*)
    FROM labelled GROUP BY months_on_book_band, from_bucket, to_bucket

    UNION ALL
    SELECT 'calendar_quarter', calendar_quarter,
           from_bucket, to_bucket, COUNT(*)
    FROM labelled GROUP BY calendar_quarter, from_bucket, to_bucket
)
SELECT
    cut_type,
    cut_value,
    from_bucket,
    to_bucket,
    transitions,
    SUM(transitions) OVER (
        PARTITION BY cut_type, cut_value, from_bucket
    )                                                          AS opportunities,
    CAST(transitions AS DOUBLE) / SUM(transitions) OVER (
        PARTITION BY cut_type, cut_value, from_bucket
    )                                                          AS rate
FROM by_cut
ORDER BY cut_type, cut_value, from_bucket, to_bucket;
