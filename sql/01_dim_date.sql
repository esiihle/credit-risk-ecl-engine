-- =============================================================================
-- dim_date — the calendar dimension, at MONTH-END grain
-- =============================================================================
-- Why a date dimension at all, when every fact row already carries a date?
--
-- Because "group by quarter", "compare to the same month last year" and "was
-- this a year end?" are questions about the CALENDAR, not about loans. Putting
-- those attributes in a dimension means they are defined once, consistently,
-- and every report that joins to it agrees. The alternative — deriving them in
-- each query — is how two reports end up disagreeing about which quarter
-- December belongs to.
--
-- GRAIN: one row per calendar month end.
--
-- Month end, not month start, because this is a lending book: balances,
-- arrears and provisions are all struck at the close of a reporting month.
--
-- A note on building the series. Adding one month repeatedly to 31 January
-- lands on 28 February and then stays at the 28th — the drift is silent and
-- would corrupt every join downstream. Generating month STARTS and taking
-- last_day() of each is immune to it.
-- =============================================================================

CREATE OR REPLACE TABLE dim_date AS
WITH month_starts AS (
    SELECT unnest(generate_series(
        date_trunc('month', CAST($start_date AS DATE)),
        date_trunc('month', CAST($end_date   AS DATE)),
        INTERVAL 1 MONTH
    )) AS month_start
),
calendar AS (
    SELECT CAST(last_day(month_start) AS DATE) AS month_end_date
    FROM month_starts
)
SELECT
    -- Surrogate key. An integer of the form YYYYMM: compact, sorts correctly,
    -- and readable in a result set, which a hash or a sequence is not.
    CAST(strftime(month_end_date, '%Y%m') AS INTEGER)       AS date_key,
    month_end_date,

    CAST(year(month_end_date)    AS INTEGER)                AS calendar_year,
    CAST(quarter(month_end_date) AS INTEGER)                AS calendar_quarter,
    CAST(month(month_end_date)   AS INTEGER)                AS calendar_month,
    strftime(month_end_date, '%B')                          AS month_name,
    strftime(month_end_date, '%Y-%m')                       AS year_month,

    -- A monotonic month counter. Differences between two of these give a span
    -- in months directly, with no date arithmetic and no leap-year thinking —
    -- which is what makes "months on book" and vintage offsets trivial later.
    CAST(year(month_end_date) * 12 + month(month_end_date) AS INTEGER)
                                                            AS month_index,

    month(month_end_date) = 12                              AS is_year_end,
    month(month_end_date) IN (3, 6, 9, 12)                  AS is_quarter_end,

    -- The date the ECL is struck at. Flagged here so every downstream query
    -- reads the reporting date from the dimension rather than hard-coding it.
    month_end_date = CAST($reporting_date AS DATE)          AS is_reporting_date
FROM calendar
ORDER BY month_end_date;
