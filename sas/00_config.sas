/* ===================================================================
 * 00_config.sas  --  GENERATED FILE, DO NOT EDIT
 *
 * Produced by scripts/export_for_sas.py from config.yaml. Every
 * threshold below is an accounting policy choice, and it lives in
 * exactly one place so the SAS engine and the DuckDB engine cannot
 * drift apart. Change config.yaml and rerun the export.
 * =================================================================== */

/* 90+ days past due is default, and default is absorbing */
%let default_dpd = 90;

/* IFRS 9 rebuttable presumption: 30 days past due means SICR */
%let sicr_dpd_backstop = 30;

/* lifetime PD must rise by this multiple to trigger SICR */
%let sicr_relative_pd_increase = 2.0;

/* and the level must be material before the multiple counts */
%let sicr_absolute_pd_floor = 0.05;

/* months of good behaviour before Stage 2 releases back to Stage 1 */
%let probation_months = 3;

/* below this a segment-age cell falls back to the segment rate */
%let min_transitions_per_cell = 50;

/* 12-month ECL horizon for Stage 1 */
%let horizon_stage_1 = 12;

/* the absorbing arrears bucket */
%let default_bucket = 4;

/* the reporting date the whole engine is run as at */
%let reporting_date = '31DEC2025'd;
