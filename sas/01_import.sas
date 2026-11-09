/* ===========================================================================
 * 01_import.sas  --  read the three source CSVs
 * ===========================================================================
 * Only source tables cross from DuckDB: accounts, loan_month and recovery.
 * Nothing derived, and none of the answer key. The SAS engine rebuilds every
 * intermediate table itself, which is the only reason comparing the two
 * engines means anything.
 *
 * A DATA step with explicit INFILE rather than PROC IMPORT. PROC IMPORT
 * guesses column types from the first rows of the file, and a guess that comes
 * out INTEGER on one book and CHARACTER on another would make the port fail in
 * a way that looks like a logic error. Declaring the types removes the guess.
 * =========================================================================== */

data ecl.accounts;
    infile "&csv_root./accounts.csv" dsd firstobs=2 truncover lrecl=4096;
    length account_id $20 segment $20;
    input account_id $ segment $ origination_date : yymmdd10.
          principal term_months annual_rate instalment;
    format origination_date yymmdd10.;
run;
%say(ecl.accounts)

data ecl.loan_month;
    infile "&csv_root./loan_month.csv" dsd firstobs=2 truncover lrecl=4096;
    length account_id $20;
    input account_id $ reporting_date : yymmdd10. months_on_book
          arrears_bucket scheduled_balance is_default days_past_due;
    format reporting_date yymmdd10.;
run;
%say(ecl.loan_month)

data ecl.recovery;
    infile "&csv_root./recovery.csv" dsd firstobs=2 truncover lrecl=4096;
    length account_id $20;
    input account_id $ default_date : yymmdd10. months_on_book_at_default
          ead_at_default months_in_workout workout_complete recovery_to_date;
    format default_date yymmdd10.;
run;
%say(ecl.recovery)

/* ---------------------------------------------------------------------------
 * Two checks before anything is built on this data. An import that silently
 * read 0 rows, or shifted a column, produces an engine that runs cleanly and
 * reconciles to nothing.
 * ------------------------------------------------------------------------- */
proc sql noprint;
    select count(*) into :n_acc trimmed from ecl.accounts;
    select count(*) into :n_lm  trimmed from ecl.loan_month;
    select sum(case when arrears_bucket < 0 or arrears_bucket > 4 then 1 else 0 end)
        into :bad_bucket trimmed from ecl.loan_month;
quit;

%macro import_checks;
    %if &n_acc. = 0 or &n_lm. = 0 %then %do;
        %put ERROR: the CSVs read as empty. Check &csv_root. and the file names.;
        %abort cancel;
    %end;
    %if &bad_bucket. > 0 %then %do;
        %put ERROR: &bad_bucket. rows have an arrears bucket outside 0-4,;
        %put ERROR- which means the columns did not line up on import.;
        %abort cancel;
    %end;
    %put NOTE: import checks passed: &n_acc. accounts, &n_lm. account-months.;
%mend import_checks;
%import_checks
