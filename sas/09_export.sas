/* ===========================================================================
 * 09_export.sas  --  write the results back out for reconciliation
 * ===========================================================================
 * One CSV per reconcilable table. These go back to the repository and
 * `scripts/reconcile_sas.py` compares them to the DuckDB reference account by
 * account, to the tolerance set in config.yaml before either side was built.
 * =========================================================================== */

%macro dump(table, file);
    proc export data=&table. outfile="&out_root./sas_&file..csv"
                dbms=csv replace;
    run;
    %put NOTE: ===== exported &table. to &out_root./sas_&file..csv;
%mend dump;

%dump(ecl.roll_rate,          roll_rate)
%dump(ecl.pd_curve,           pd_curve)
%dump(ecl.segment_age_matrix, segment_age_matrix)
%dump(ecl.account_pd,         account_pd)
