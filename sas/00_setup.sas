/* ===========================================================================
 * 00_setup.sas  --  paths and options. THE ONE FILE YOU EDIT.
 * ===========================================================================
 * Set &csv_root to the folder in SAS OnDemand that holds accounts.csv,
 * loan_month.csv and recovery.csv, and &out_root to where the results should
 * be written. Everything else in sas/ runs unchanged.
 *
 * To find your path: in SAS Studio, open the Files (Home) panel on the left,
 * right-click the folder and choose Properties. It will look like
 * /home/u00000000/ecl.
 * =========================================================================== */

%let csv_root = /home/CHANGE_ME/ecl;
%let out_root = /home/CHANGE_ME/ecl;

/* ---------------------------------------------------------------------------
 * Thresholds come from the generated include, never from numbers typed here.
 * config.yaml is the single source; scripts/export_for_sas.py writes
 * 00_config.sas from it.
 * ------------------------------------------------------------------------- */
%include "&csv_root./00_config.sas";

libname ecl "&out_root.";

options
    /* Full source and macro expansion in the log. This port cannot be run on
     * the machine it was written on, so the log is the only diagnostic there
     * is, and a quiet log would be worse than a noisy one. */
    source source2 mprint
    /* Stop at the first error rather than running on and producing a partial
     * result that looks complete. Same reasoning as run_pipeline.py halting
     * on a failed quality check. */
    errorabend
    nofmterr
    ;

/* A counter macro used after every step. A SAS program that builds eleven
 * tables and says nothing is a program whose failure you discover at the end. */
%macro say(table, note=);
    %local n;
    proc sql noprint;
        select count(*) into :n trimmed from &table.;
    quit;
    %put NOTE: ===== &table. : &n. rows %if %length(&note.) %then (&note.);;
%mend say;
