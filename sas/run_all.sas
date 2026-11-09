/* ===========================================================================
 * run_all.sas  --  the whole SAS engine, in order
 * ===========================================================================
 * Edit the two paths in 00_setup.sas, then run this.
 *
 * On a first run, consider running the %include lines one at a time instead.
 * Each program prints its row counts to the log through %say, and a failure
 * localises immediately rather than after eleven tables have been built.
 *
 * The order matters and is the same as run_pipeline.py: staging needs the
 * pooled PD curve, and the term structure needs the stage.
 * =========================================================================== */

%include "/home/CHANGE_ME/ecl/sas/00_setup.sas";

%include "&csv_root./sas/01_import.sas";
%include "&csv_root./sas/02_mart.sas";
%include "&csv_root./sas/03_roll_rates.sas";
%include "&csv_root./sas/04_pd_curve.sas";
%include "&csv_root./sas/05_staging.sas";
%include "&csv_root./sas/06_pd_term_structure.sas";
%include "&csv_root./sas/09_export.sas";

%put NOTE: ============================================================;
%put NOTE: SAS engine complete. Download the sas_*.csv files and run;
%put NOTE:   python scripts/reconcile_sas.py --sas-dir <folder>;
%put NOTE: ============================================================;
