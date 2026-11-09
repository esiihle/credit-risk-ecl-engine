/* ===========================================================================
 * 05_pd_term_structure.sas  --  matrices by segment and age, and the walk
 * ===========================================================================
 * The age-aware PD curve: the method Phase 4 measured as 9.2x more accurate
 * per account than a pooled one. It is also the hardest thing in the port,
 * because the matrix CHANGES AS THE WALK ADVANCES. An account 10 months old
 * today faces the 12-23 month hazard a year from now, so the step from month
 * h-1 to month h uses the band for age (start_age + h - 1), not the band it
 * started in.
 *
 * In DuckDB that is one join inside a recursive CTE. Here it is the same join
 * inside a %macro loop, which is the only structural difference.
 * =========================================================================== */

/* ---------------------------------------------------------------------------
 * The cells, with the thin-cell fallback.
 *
 * `SUM(COUNT(*)) OVER (PARTITION BY segment, from_bucket)` is a window
 * function, so the segment-pooled rate is built as its own table and joined.
 * ------------------------------------------------------------------------- */
proc sql;
    create table work.banded as
    select
        a.segment,
        case
            when t.months_on_book <  6 then '1. 0-5m'
            when t.months_on_book < 12 then '2. 6-11m'
            when t.months_on_book < 24 then '3. 12-23m'
            when t.months_on_book < 36 then '4. 24-35m'
            else                            '5. 36m+'
        end                                                   as age_band length=9,
        t.from_bucket,
        t.to_bucket
    from ecl.bucket_transition t
    inner join ecl.dim_account a
            on a.account_id = t.account_id;

    create table work.cell_counts as
    select segment, age_band, from_bucket, to_bucket, count(*) as transitions
    from work.banded
    group by segment, age_band, from_bucket, to_bucket;

    create table work.row_totals as
    select segment, age_band, from_bucket, sum(transitions) as opportunities
    from work.cell_counts
    group by segment, age_band, from_bucket;

    /* The fallback estimate: the segment's rate pooled over every age. */
    create table work.seg_cells as
    select segment, from_bucket, to_bucket, count(*) as n
    from work.banded
    group by segment, from_bucket, to_bucket;

    create table work.seg_totals as
    select segment, from_bucket, sum(n) as n_total
    from work.seg_cells
    group by segment, from_bucket;

    create table work.segment_pooled as
    select c.segment, c.from_bucket, c.to_bucket, c.n / t.n_total as pooled_rate
    from work.seg_cells c
    inner join work.seg_totals t
            on t.segment = c.segment and t.from_bucket = c.from_bucket;

    create table ecl.segment_age_matrix as
    select
        c.segment,
        c.age_band,
        c.from_bucket,
        c.to_bucket,
        c.transitions,
        t.opportunities,
        /* The cell's own rate where there is enough behind it, the segment's
         * pooled rate otherwise. Without this a single stray transition in a
         * thin cell becomes a 100% roll rate, and the walk compounds that into
         * a lifetime PD of 1 for everyone in that segment and band. */
        case when t.opportunities >= &min_transitions_per_cell.
             then c.transitions / t.opportunities
             else coalesce(p.pooled_rate, 0)
        end                                                   as rate,
        case when t.opportunities <  &min_transitions_per_cell.
             then 1 else 0 end                                as used_fallback
    from work.cell_counts c
    inner join work.row_totals t
            on t.segment = c.segment and t.age_band = c.age_band
           and t.from_bucket = c.from_bucket
    left join work.segment_pooled p
           on p.segment = c.segment and p.from_bucket = c.from_bucket
          and p.to_bucket = c.to_bucket
    order by c.segment, c.age_band, c.from_bucket, c.to_bucket;
quit;
%say(ecl.segment_age_matrix)

/* The matrices laid out flat, one row per (segment, age band). */
proc sql;
    create table ecl.matrix_wide as
    select
        segment,
        age_band,
        coalesce(max(case when from_bucket=0 and to_bucket=0 then rate end), 0) as m00,
        coalesce(max(case when from_bucket=0 and to_bucket=1 then rate end), 0) as m01,
        coalesce(max(case when from_bucket=0 and to_bucket=2 then rate end), 0) as m02,
        coalesce(max(case when from_bucket=0 and to_bucket=3 then rate end), 0) as m03,
        coalesce(max(case when from_bucket=0 and to_bucket=4 then rate end), 0) as m04,
        coalesce(max(case when from_bucket=1 and to_bucket=0 then rate end), 0) as m10,
        coalesce(max(case when from_bucket=1 and to_bucket=1 then rate end), 0) as m11,
        coalesce(max(case when from_bucket=1 and to_bucket=2 then rate end), 0) as m12,
        coalesce(max(case when from_bucket=1 and to_bucket=3 then rate end), 0) as m13,
        coalesce(max(case when from_bucket=1 and to_bucket=4 then rate end), 0) as m14,
        coalesce(max(case when from_bucket=2 and to_bucket=0 then rate end), 0) as m20,
        coalesce(max(case when from_bucket=2 and to_bucket=1 then rate end), 0) as m21,
        coalesce(max(case when from_bucket=2 and to_bucket=2 then rate end), 0) as m22,
        coalesce(max(case when from_bucket=2 and to_bucket=3 then rate end), 0) as m23,
        coalesce(max(case when from_bucket=2 and to_bucket=4 then rate end), 0) as m24,
        coalesce(max(case when from_bucket=3 and to_bucket=0 then rate end), 0) as m30,
        coalesce(max(case when from_bucket=3 and to_bucket=1 then rate end), 0) as m31,
        coalesce(max(case when from_bucket=3 and to_bucket=2 then rate end), 0) as m32,
        coalesce(max(case when from_bucket=3 and to_bucket=3 then rate end), 0) as m33,
        coalesce(max(case when from_bucket=3 and to_bucket=4 then rate end), 0) as m34,
        0 as m40, 0 as m41, 0 as m42, 0 as m43, 1 as m44
    from ecl.segment_age_matrix
    group by segment, age_band;
quit;
%say(ecl.matrix_wide)

/* The starting points: every segment, every age a loan can be, every bucket.
 * DuckDB writes this with generate_series; SAS has no such function in PROC
 * SQL, so the ages come from a DATA step loop. */
data work.ages;
    do start_age = 0 to &max_term.;
        output;
    end;
run;

data work.buckets;
    do start_bucket = 0 to 4;
        output;
    end;
run;

proc sql;
    create table work.starts as
    select s.segment, a.start_age, b.start_bucket
    from (select distinct segment from ecl.segment_age_matrix) s
    cross join work.ages a
    cross join work.buckets b;
quit;
%say(work.starts, note=segments x ages x buckets)

/* ---------------------------------------------------------------------------
 * The age-aware walk.
 * ------------------------------------------------------------------------- */
%macro term_walk(out=, max_horizon=);
    proc sql;
        create table &out. as
        select
            st.segment,
            st.start_age,
            st.start_bucket,
            0 as horizon,
            case when st.start_bucket = 0 then 1 else 0 end as p0,
            case when st.start_bucket = 1 then 1 else 0 end as p1,
            case when st.start_bucket = 2 then 1 else 0 end as p2,
            case when st.start_bucket = 3 then 1 else 0 end as p3,
            case when st.start_bucket = 4 then 1 else 0 end as p4
        from work.starts st;
    quit;

    %local h;
    %do h = 1 %to &max_horizon.;
        proc sql;
            create table work._step as
            select
                w.segment,
                w.start_age,
                w.start_bucket,
                &h. as horizon,
                w.p0*m.m00 + w.p1*m.m10 + w.p2*m.m20 + w.p3*m.m30 + w.p4*m.m40 as p0,
                w.p0*m.m01 + w.p1*m.m11 + w.p2*m.m21 + w.p3*m.m31 + w.p4*m.m41 as p1,
                w.p0*m.m02 + w.p1*m.m12 + w.p2*m.m22 + w.p3*m.m32 + w.p4*m.m42 as p2,
                w.p0*m.m03 + w.p1*m.m13 + w.p2*m.m23 + w.p3*m.m33 + w.p4*m.m43 as p3,
                w.p0*m.m04 + w.p1*m.m14 + w.p2*m.m24 + w.p3*m.m34 + w.p4*m.m44 as p4
            from &out. w
            inner join ecl.matrix_wide m
                    on m.segment  = w.segment
                   /* THE AGE-AWARE JOIN. The band is chosen by the account's
                    * age at THIS step, which is start_age + the horizon it is
                    * stepping from, not the age it began the walk at. */
                   and m.age_band = case
                        when w.start_age + w.horizon <  6 then '1. 0-5m'
                        when w.start_age + w.horizon < 12 then '2. 6-11m'
                        when w.start_age + w.horizon < 24 then '3. 12-23m'
                        when w.start_age + w.horizon < 36 then '4. 24-35m'
                        else                                   '5. 36m+'
                       end
            where w.horizon = &h. - 1;
        quit;
        proc append base=&out. data=work._step force nowarn; run;
    %end;
%mend term_walk;

%term_walk(out=work.walk_term, max_horizon=&max_term.)

proc sql;
    create table ecl.pd_term_structure as
    select
        w.segment,
        w.start_age,
        w.start_bucket,
        w.horizon,
        w.p4                                                  as cumulative_pd,
        w.p4 - coalesce(prev.p4, 0)                           as marginal_pd,
        w.p0 + w.p1 + w.p2 + w.p3                             as surviving
    from work.walk_term w
    left join work.walk_term prev
           on prev.segment      = w.segment
          and prev.start_age    = w.start_age
          and prev.start_bucket = w.start_bucket
          and prev.horizon      = w.horizon - 1
    order by w.segment, w.start_age, w.start_bucket, w.horizon;
quit;
%say(ecl.pd_term_structure)

/* ---------------------------------------------------------------------------
 * Per-account PD at the reporting date, all three methods side by side.
 *
 * Built from account_stage rather than from the fact table, because the stage
 * is needed here and staging is what produced it. Keeping all three estimates
 * rather than only the best one is deliberate: "is the extra segmentation
 * worth it" is a question about the DIFFERENCE between them, and a table
 * holding only the chosen answer cannot be used to ask it.
 * ------------------------------------------------------------------------- */
proc sql;
    create table ecl.account_pd as
    select
        s.account_id,
        s.reporting_date,
        s.segment,
        s.arrears_bucket,
        s.months_on_book,
        s.months_remaining,
        s.scheduled_balance,
        s.stage,
        min(&horizon_stage_1., max(s.months_remaining, 0))    as horizon_12m,
        max(s.months_remaining, 0)                            as horizon_lifetime,

        coalesce(pooled_12.cumulative_pd,   0)                as pd_12m_pooled,
        coalesce(pooled_life.cumulative_pd, 0)                as pd_lifetime_pooled,
        coalesce(seg_12.cumulative_pd,      0)                as pd_12m_segmented,
        coalesce(seg_life.cumulative_pd,    0)                as pd_lifetime_segmented,
        coalesce(age_12.cumulative_pd,      0)                as pd_12m_age_aware,
        coalesce(age_life.cumulative_pd,    0)                as pd_lifetime_age_aware
    from ecl.account_stage s

    /* Pooled: one curve for the whole book, no segment, no age. */
    left join ecl.pd_curve pooled_12
           on pooled_12.start_bucket = s.arrears_bucket
          and pooled_12.horizon      = min(&horizon_stage_1., max(s.months_remaining, 0))
    left join ecl.pd_curve pooled_life
           on pooled_life.start_bucket = s.arrears_bucket
          and pooled_life.horizon      = max(s.months_remaining, 0)

    /* Segmented: the account's own segment, but entered at age 0, so the
     * walk uses the first age band throughout. */
    left join ecl.pd_term_structure seg_12
           on seg_12.segment      = s.segment
          and seg_12.start_age    = 0
          and seg_12.start_bucket = s.arrears_bucket
          and seg_12.horizon      = min(&horizon_stage_1., max(s.months_remaining, 0))
    left join ecl.pd_term_structure seg_life
           on seg_life.segment      = s.segment
          and seg_life.start_age    = 0
          and seg_life.start_bucket = s.arrears_bucket
          and seg_life.horizon      = max(s.months_remaining, 0)

    /* Age aware: entered at the account's actual age, so the walk changes
     * band as it advances. */
    left join ecl.pd_term_structure age_12
           on age_12.segment      = s.segment
          and age_12.start_age    = s.months_on_book
          and age_12.start_bucket = s.arrears_bucket
          and age_12.horizon      = min(&horizon_stage_1., max(s.months_remaining, 0))
    left join ecl.pd_term_structure age_life
           on age_life.segment      = s.segment
          and age_life.start_age    = s.months_on_book
          and age_life.start_bucket = s.arrears_bucket
          and age_life.horizon      = max(s.months_remaining, 0)

    where s.reporting_date = &reporting_date.
      and s.months_remaining > 0;
quit;
%say(ecl.account_pd, note=accounts on book at the reporting date)
