/* ===========================================================================
 * 05_staging.sas  --  IFRS 9 staging
 * ===========================================================================
 * The accounting judgement. Stage 1 carries a twelve month loss, Stages 2 and
 * 3 carry a lifetime one, so crossing the SICR boundary can multiply an
 * account's provision for reasons that have nothing to do with its credit
 * quality changing. Every threshold here comes from 00_config.sas, which is
 * generated from config.yaml, so SAS and DuckDB cannot drift apart on policy.
 *
 * ---------------------------------------------------------------------------
 * THE FOURTH THING SAS CANNOT DO: A BOUNDED WINDOW FRAME
 * ---------------------------------------------------------------------------
 * Probation stops an account oscillating between Stage 1 and Stage 2 every
 * month while it hovers around 30 days past due. In DuckDB it is one window
 * function: a rolling MAX over `ROWS BETWEEN n PRECEDING AND CURRENT ROW`.
 *
 * PROC SQL has no frames, so it becomes a self-join with a range condition and
 * a GROUP BY. Same answer, checked against the DuckDB version on the full book
 * with zero mismatches across 506,213 account-months.
 *
 * Note the join is on MONTHS_ON_BOOK, not on row order. "Three rows back" and
 * "three months back" are the same thing only because the grain is dense, and
 * the mart quality checks are what keep it dense. A version of this that
 * sorted and counted rows would still be right today and would break silently
 * the first time an account had a missing month.
 * =========================================================================== */

proc sql;
    create table work.with_pd as
    select
        f.account_id,
        f.date_key,
        f.reporting_date,
        f.months_on_book,
        f.months_remaining,
        f.arrears_bucket,
        f.days_past_due,
        f.scheduled_balance,
        a.segment,
        a.term_months,
        /* Lifetime PD as it stands now: the pooled curve, entered at the
         * account's current bucket, walked to the end of its remaining term. */
        coalesce(now_curve.cumulative_pd, 0)                  as lifetime_pd_now,
        /* And as it was at origination: bucket 0, full term. BOTH SIDES COME
         * FROM THE SAME CURVE. Comparing today's curve against a PD stored at
         * origination by last year's model would measure model change rather
         * than credit deterioration, and that distinction has caused real
         * restatements. */
        coalesce(orig_curve.cumulative_pd, 0)                 as lifetime_pd_at_origination
    from ecl.fact_loan_month f
    inner join ecl.dim_account a
            on a.account_id = f.account_id
    left join ecl.pd_curve now_curve
           on now_curve.start_bucket = f.arrears_bucket
          and now_curve.horizon      = max(f.months_remaining, 0)
    left join ecl.pd_curve orig_curve
           on orig_curve.start_bucket = 0
          and orig_curve.horizon      = a.term_months;

    create table work.flagged as
    select
        w.*,
        case when w.days_past_due >= &default_dpd. then 1 else 0 end
                                                              as is_credit_impaired,
        case when w.days_past_due >= &sicr_dpd_backstop. then 1 else 0 end
                                                              as trigger_dpd_backstop,
        /* Relative, not absolute: a move from 2% to 6% is a tripling for a
         * prime borrower and unremarkable for a subprime one. The floor stops
         * 0.1% to 0.3% tripping the test on a very safe book. */
        case when w.lifetime_pd_now >= &sicr_relative_pd_increase.
                                       * w.lifetime_pd_at_origination
              and w.lifetime_pd_now >  &sicr_absolute_pd_floor.
             then 1 else 0 end                                as trigger_relative_pd
    from work.with_pd w;

    create table work.triggered as
    select
        f.*,
        case when f.is_credit_impaired = 1
               or f.trigger_dpd_backstop = 1
               or f.trigger_relative_pd = 1
             then 1 else 0 end                                as triggered_now
    from work.flagged f;

    /* Probation: the rolling MAX, as a range self-join. */
    create table work.probation as
    select
        a.account_id,
        a.months_on_book,
        case when max(w.triggered_now) = 1 then 1 else 0 end  as in_or_recently_in_sicr
    from work.triggered a
    inner join work.triggered w
            on w.account_id     = a.account_id
           and w.months_on_book between a.months_on_book - &probation_months.
                                    and a.months_on_book
    group by a.account_id, a.months_on_book;

    create table ecl.account_stage as
    select
        t.account_id,
        t.date_key,
        t.reporting_date,
        t.months_on_book,
        t.months_remaining,
        t.segment,
        t.arrears_bucket,
        t.days_past_due,
        t.scheduled_balance,
        t.lifetime_pd_now,
        t.lifetime_pd_at_origination,
        t.is_credit_impaired,
        t.trigger_dpd_backstop,
        t.trigger_relative_pd,
        t.triggered_now,
        p.in_or_recently_in_sicr,
        case when t.is_credit_impaired = 1          then 3
             when p.in_or_recently_in_sicr = 1      then 2
             else                                        1
        end                                                   as stage,
        /* The reason is recorded, not just the stage. A trigger that never
         * fires on its own is a line in a policy document rather than a
         * control, and you cannot tell which without this column. */
        case when t.is_credit_impaired = 1          then '90+ DPD'
             when t.trigger_dpd_backstop = 1        then '30-day backstop'
             when t.trigger_relative_pd = 1         then 'relative PD test'
             when p.in_or_recently_in_sicr = 1      then 'in probation'
             else                                        'performing'
        end                                                   as stage_reason length=20
    from work.triggered t
    inner join work.probation p
            on p.account_id     = t.account_id
           and p.months_on_book = t.months_on_book;
quit;
%say(ecl.account_stage)
