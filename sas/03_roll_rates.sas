/* ===========================================================================
 * 03_roll_rates.sas  --  bucket transitions and the pooled roll-rate matrix
 * ===========================================================================
 * THE SECOND THING SAS CANNOT DO: LEAD() AS A WINDOW FUNCTION.
 *
 * DuckDB takes the destination bucket with
 *
 *     LEAD(arrears_bucket) OVER (PARTITION BY account_id ORDER BY months_on_book)
 *
 * and then keeps only the rows where the next month is genuinely observed.
 * Here that is an INNER join onto month t+1 of the same account, which does
 * the keeping as part of the join rather than as a filter afterwards.
 *
 * That is not a shortcut, it is the censoring rule from Phase 2 restated. A
 * transition exists only where the next month exists. The last row of every
 * account has no successor, and counting it as "stayed" is what understates
 * roll-to-default by nearly 4%, worst in the deepest buckets, because those
 * are the accounts most likely to be in their final observed month.
 *
 * The inner join drops exactly one row per account, which is the arithmetic
 * `test_exactly_one_transition_is_lost_per_account` pins on the DuckDB side.
 * =========================================================================== */

proc sql;
    create table ecl.bucket_transition as
    select
        f.account_id,
        f.months_on_book,
        f.arrears_bucket                                      as from_bucket,
        n.arrears_bucket                                      as to_bucket,
        f.scheduled_balance
    from ecl.fact_loan_month f
    inner join ecl.fact_loan_month n
            on n.account_id     = f.account_id
           and n.months_on_book = f.months_on_book + 1;
quit;
%say(ecl.bucket_transition, note=one per account-month with an observed successor)

/* The pooled matrix. Row totals come from a separate aggregate and a join,
 * because `SUM(COUNT(*)) OVER (PARTITION BY from_bucket)` is a window function
 * and PROC SQL has none. */
proc sql;
    create table work.cell_counts as
    select from_bucket, to_bucket, count(*) as transitions
    from ecl.bucket_transition
    group by from_bucket, to_bucket;

    create table work.row_totals as
    select from_bucket, sum(transitions) as opportunities
    from work.cell_counts
    group by from_bucket;

    create table ecl.roll_rate as
    select
        c.from_bucket,
        c.to_bucket,
        c.transitions,
        t.opportunities,
        c.transitions / t.opportunities                       as rate
    from work.cell_counts c
    inner join work.row_totals t
            on t.from_bucket = c.from_bucket
    order by c.from_bucket, c.to_bucket;
quit;
%say(ecl.roll_rate)
