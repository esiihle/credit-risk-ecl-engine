/* ===========================================================================
 * 02_mart.sas  --  dim_account, dim_date, fact_loan_month
 * ===========================================================================
 * The DuckDB mart, rebuilt. Same grain, same derived columns, same meaning:
 *
 *     one row per account per calendar month end on which it was on book.
 *
 * ---------------------------------------------------------------------------
 * THE FIRST THING SAS CANNOT DO: LAG() AS A WINDOW FUNCTION
 * ---------------------------------------------------------------------------
 * `fact_loan_month.is_new_default` is built in DuckDB with
 *
 *     LAG(is_default) OVER (PARTITION BY account_id ORDER BY months_on_book)
 *
 * PROC SQL has no window functions at all. The replacement is a left self-join
 * onto the previous month, which gives the same answer because the grain is
 * dense: an account on book in month t is on book in every month from 1 to t,
 * and `04_mart_quality.sql` is the check that keeps it that way.
 *
 * SAS does have a LAG() function, but it is a DATA step queue rather than an
 * ordered window, and it reads the previous ROW PROCESSED rather than the
 * previous row of the same account. Used here it would carry the last month of
 * one account into the first month of the next. The self-join has no such
 * failure mode, which is why this port does not reach for it.
 * =========================================================================== */

proc sql;
    create table ecl.dim_account as
    select
        a.account_id,
        a.segment,
        a.origination_date,
        a.principal,
        a.term_months,
        a.annual_rate,
        a.instalment,
        /* Vintage keys, defined once here so no report derives its own and
         * disagrees about which quarter a month belongs to. */
        put(a.origination_date, yymmn6.)                      as vintage_month length=7,
        year(a.origination_date) * 12 + month(a.origination_date)
                                                              as vintage_month_index,
        year(a.origination_date)                              as vintage_year,
        /* The monthly effective rate: used to amortise and, in Phase 6, to
         * discount. The same figure must serve both. */
        (1 + a.annual_rate) ** (1/12) - 1                     as monthly_rate
    from ecl.accounts a;
quit;
%say(ecl.dim_account)

/* The calendar. Month ends are built from month STARTS plus INTNX(...,'E'),
 * never by adding a month repeatedly to a date: 31 Jan + 1 month is 28 Feb,
 * and every month after that stays on the 28th. The drift is silent and every
 * join to the calendar would start missing. */
proc sql noprint;
    select min(reporting_date), max(reporting_date)
      into :first_month, :last_month
      from ecl.loan_month;
quit;

data ecl.dim_date;
    start = intnx('month', &first_month., 0, 'B');
    do while (start <= &last_month.);
        month_end_date = intnx('month', start, 0, 'E');
        date_key       = input(put(month_end_date, yymmddn8.), 8.);
        month_index    = year(month_end_date) * 12 + month(month_end_date);
        calendar_year  = year(month_end_date);
        calendar_quarter = cats(put(year(month_end_date), 4.), 'Q',
                                put(qtr(month_end_date), 1.));
        output;
        start = intnx('month', start, 1, 'B');
    end;
    format month_end_date yymmdd10.;
    keep date_key month_end_date month_index calendar_year calendar_quarter;
run;
%say(ecl.dim_date)

proc sql;
    create table ecl.fact_loan_month as
    select
        f.account_id,
        f.reporting_date,
        input(put(f.reporting_date, yymmddn8.), 8.)           as date_key,
        f.months_on_book,
        a.term_months - f.months_on_book                      as months_remaining,
        f.arrears_bucket,
        f.days_past_due,
        f.scheduled_balance,
        f.is_default,
        /* LAG(), as a self-join. COALESCE because the first month of an
         * account has no previous row, and an account that is in default in
         * its first observed month defaulted in it. */
        case when f.is_default = 1 and coalesce(p.is_default, 0) = 0
             then 1 else 0 end                                as is_new_default
    from ecl.loan_month f
    inner join ecl.dim_account a
            on a.account_id = f.account_id
    left  join ecl.loan_month p
            on p.account_id     = f.account_id
           and p.months_on_book = f.months_on_book - 1;
quit;
%say(ecl.fact_loan_month, note=one row per account per month on book)
