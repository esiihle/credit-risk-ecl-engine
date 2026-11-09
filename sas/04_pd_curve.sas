/* ===========================================================================
 * 04_pd_curve.sas  --  the pooled PD curve
 * ===========================================================================
 * THE THIRD AND LARGEST THING SAS CANNOT DO: RECURSIVE CTEs.
 *
 * DuckDB walks a state vector forward with WITH RECURSIVE: start with all the
 * probability in the account's current bucket, multiply by the transition
 * matrix once per month, and read the mass that has accumulated in the default
 * bucket. PROC SQL has no recursion.
 *
 * The replacement is a %macro that does the same thing one step at a time,
 * appending each horizon to the table and joining the previous horizon to the
 * matrix. Iteration and recursion compute the same sequence here because the
 * recursion is linear: every step depends only on the step before it.
 *
 * Checked, not assumed. The identical iterative walk was run against the
 * DuckDB recursive version on the full book and reproduced it with a worst
 * gap of 0.0 and probability mass conserved to 2.2e-15. See methodology §9.2.
 *
 * ---------------------------------------------------------------------------
 * TWO DUCKDB TRAPS THAT DO NOT EXIST HERE, AND ONE THAT DOES
 * ---------------------------------------------------------------------------
 * The DECIMAL truncation trap from §4.1 is a DuckDB problem only. Every SAS
 * numeric is an 8-byte float, so `1.0` cannot silently become DECIMAL(2,1) and
 * freeze the vector. The ::DOUBLE casts the DuckDB version needs have no SAS
 * equivalent and none is required.
 *
 * The absorbing-row trap is real in both. `roll_rate` is built from observed
 * transitions and nothing is ever observed leaving default, so bucket 4 has no
 * row at all. Supplied below as the identity row. Without it the probability
 * mass of a defaulted account vanishes at the next step and cumulative PD
 * quietly stops rising.
 *
 * The one that is worse in SAS: a missing value propagates through arithmetic
 * silently, so an absent matrix cell gives a missing probability rather than a
 * zero. Hence COALESCE on every cell.
 * =========================================================================== */

/* The 5x5 matrix, flat, with the absorbing row asserted. */
proc sql;
    create table ecl.matrix_pooled as
    select
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
        /* The absorbing row. A modelling fact, not a data fact, which is
         * exactly why it is written down rather than read from a table that
         * cannot contain it. */
        0 as m40, 0 as m41, 0 as m42, 0 as m43, 1 as m44
    from ecl.roll_rate;
quit;

/* How far to walk: the longest contractual term on the book. */
proc sql noprint;
    select max(term_months) into :max_term trimmed from ecl.dim_account;
quit;

%macro pd_walk(out=, max_horizon=);
    /* Horizon 0: all the probability sits in the starting bucket. */
    proc sql;
        create table &out. as
        select
            b.start_bucket,
            0 as horizon,
            case when b.start_bucket = 0 then 1 else 0 end as p0,
            case when b.start_bucket = 1 then 1 else 0 end as p1,
            case when b.start_bucket = 2 then 1 else 0 end as p2,
            case when b.start_bucket = 3 then 1 else 0 end as p3,
            case when b.start_bucket = 4 then 1 else 0 end as p4
        from (select 0 as start_bucket from ecl.matrix_pooled union all
              select 1 from ecl.matrix_pooled union all
              select 2 from ecl.matrix_pooled union all
              select 3 from ecl.matrix_pooled union all
              select 4 from ecl.matrix_pooled) b;
    quit;

    %local h;
    %do h = 1 %to &max_horizon.;
        proc sql;
            create table work._step as
            select
                w.start_bucket,
                &h. as horizon,
                w.p0*m.m00 + w.p1*m.m10 + w.p2*m.m20 + w.p3*m.m30 + w.p4*m.m40 as p0,
                w.p0*m.m01 + w.p1*m.m11 + w.p2*m.m21 + w.p3*m.m31 + w.p4*m.m41 as p1,
                w.p0*m.m02 + w.p1*m.m12 + w.p2*m.m22 + w.p3*m.m32 + w.p4*m.m42 as p2,
                w.p0*m.m03 + w.p1*m.m13 + w.p2*m.m23 + w.p3*m.m33 + w.p4*m.m43 as p3,
                w.p0*m.m04 + w.p1*m.m14 + w.p2*m.m24 + w.p3*m.m34 + w.p4*m.m44 as p4
            from &out. w
            cross join ecl.matrix_pooled m
            where w.horizon = &h. - 1;
        quit;
        /* Appended rather than inserted from a select on the same table:
         * reading and writing one table in a single PROC SQL statement is the
         * kind of thing that works until it does not. */
        proc append base=&out. data=work._step force nowarn; run;
    %end;
%mend pd_walk;

%pd_walk(out=work.walk_pooled, max_horizon=&max_term.)

/* Marginal PD needs the previous horizon, which is LAG() again, so it is a
 * self-join again. COALESCE handles horizon 0, where there is no previous. */
proc sql;
    create table ecl.pd_curve as
    select
        w.start_bucket,
        w.horizon,
        w.p4                                                  as cumulative_pd,
        w.p4 - coalesce(prev.p4, 0)                           as marginal_pd,
        w.p0 + w.p1 + w.p2 + w.p3                             as surviving
    from work.walk_pooled w
    left join work.walk_pooled prev
           on prev.start_bucket = w.start_bucket
          and prev.horizon      = w.horizon - 1
    order by w.start_bucket, w.horizon;
quit;
%say(ecl.pd_curve)

/* Probability mass must stay at 1 at every step. If it does not, the
 * absorbing row is missing or a matrix cell came through as missing. */
proc sql noprint;
    select max(abs(p0+p1+p2+p3+p4 - 1)) into :mass_error
    from work.walk_pooled;
quit;
%macro mass_check;
    %if %sysevalf(&mass_error. > 1e-9) %then %do;
        %put ERROR: probability mass drifted by &mass_error., so the matrix is;
        %put ERROR- not stochastic. Check the absorbing row and the COALESCEs.;
        %abort cancel;
    %end;
    %put NOTE: probability conserved at every horizon (worst drift &mass_error.);
%mend mass_check;
%mass_check
