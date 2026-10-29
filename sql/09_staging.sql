-- =============================================================================
-- account_stage — IFRS 9 staging, for every account-month
-- =============================================================================
-- THE ACCOUNTING JUDGEMENT OF THE WHOLE PROJECT.
--
--   Stage 1  performing              -> 12-MONTH expected credit loss
--   Stage 2  significant increase    -> LIFETIME expected credit loss
--   Stage 3  credit-impaired         -> LIFETIME expected credit loss
--
-- The jump from Stage 1 to Stage 2 replaces a twelve-month horizon with the
-- remaining life of the loan. On a 48-month loan that can multiply the
-- provision several times over for an account whose circumstances have barely
-- changed. **Staging moves the provision more than the PD model does**, which
-- is why every rule here is config, and why each trigger is recorded by name.
--
-- Computed for EVERY month, not just the reporting date, for two reasons: the
-- probation rule needs an account's recent history, and Phase 7 needs the
-- migration between stages over time.
--
-- -----------------------------------------------------------------------------
-- THE TRIGGERS
-- -----------------------------------------------------------------------------
-- Stage 3 — `days_past_due >= staging.default_dpd` (90). The Basel/IFRS 9
--           default definition, stated in config rather than assumed.
--
-- Stage 2 — significant increase in credit risk since origination. Three ways
--           in, any one sufficient:
--
--   a) THE 30-DAY BACKSTOP. IFRS 9 carries a rebuttable presumption that risk
--      has increased significantly once an account is 30 days past due. It is
--      a backstop, not the primary test: waiting for arrears to appear defeats
--      the purpose of a forward-looking standard.
--
--   b) THE RELATIVE PD TEST. Lifetime PD now against lifetime PD at
--      origination. RELATIVE, not absolute, because a move from 2% to 6% is a
--      tripling of risk for a prime borrower while 6% is unremarkable for a
--      subprime one. An absolute threshold would stage most of the subprime
--      book on day one and never catch a deteriorating prime account.
--
--   c) ...subject to AN ABSOLUTE FLOOR. Without it, a move from 0.1% to 0.3%
--      trips the doubling test, and a portfolio of very safe accounts would
--      migrate to Stage 2 on noise. The floor says: a relative increase only
--      counts once the level is material.
--
-- -----------------------------------------------------------------------------
-- PROBATION
-- -----------------------------------------------------------------------------
-- An account that stops triggering does not return to Stage 1 immediately; it
-- must behave for `staging.probation_months` first.
--
-- Without it, an account hovering around 30 days past due oscillates between
-- Stage 1 and Stage 2 every month, and the provision swings by the difference
-- between a 12-month and a lifetime loss each time. That volatility is an
-- artefact of the rule, not a change in credit risk, and explaining it to an
-- audit committee is impossible because there is nothing to explain.
--
-- Implemented as a rolling window: an account is held out of Stage 1 if any
-- trigger fired in the current month or the previous `probation_months`.
-- =============================================================================

CREATE OR REPLACE TABLE account_stage AS
WITH with_pd AS (
    SELECT
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

        -- Lifetime PD as things stand: from the CURRENT bucket, over the
        -- REMAINING term. Both parts matter — an account deep in arrears with
        -- two months left is a different risk from the same account with four
        -- years to run.
        COALESCE(now_curve.cumulative_pd, 0.0)              AS lifetime_pd_now,

        -- Lifetime PD as it looked at origination: from current, over the full
        -- original term. This is the benchmark the relative test is against,
        -- and it is recomputed rather than stored because the comparison must
        -- use the SAME curve for both sides. Comparing today's curve against a
        -- figure produced by last year's model would measure model change, not
        -- credit deterioration.
        COALESCE(origination_curve.cumulative_pd, 0.0)      AS lifetime_pd_at_origination
    FROM fact_loan_month f
    JOIN dim_account a
      ON a.account_id = f.account_id
    LEFT JOIN pd_curve now_curve
      ON now_curve.start_bucket = f.arrears_bucket
     AND now_curve.horizon      = GREATEST(f.months_remaining, 0)
    LEFT JOIN pd_curve origination_curve
      ON origination_curve.start_bucket = 0
     AND origination_curve.horizon      = a.term_months
),
triggered AS (
    SELECT
        *,
        days_past_due >= CAST($default_dpd AS INTEGER)      AS is_credit_impaired,
        days_past_due >= CAST($sicr_dpd AS INTEGER)         AS trigger_dpd_backstop,

        (lifetime_pd_now >= CAST($relative_increase AS DOUBLE)
                           * lifetime_pd_at_origination
         AND lifetime_pd_now > CAST($absolute_floor AS DOUBLE))
                                                            AS trigger_relative_pd
    FROM with_pd
),
flagged AS (
    SELECT
        *,
        (is_credit_impaired OR trigger_dpd_backstop OR trigger_relative_pd)
                                                            AS triggered_now
    FROM triggered
),
probation AS (
    SELECT
        *,
        -- Any trigger in the current month or the previous N months keeps the
        -- account out of Stage 1. A rolling MAX over a bounded frame is the
        -- whole probation rule.
        MAX(CASE WHEN triggered_now THEN 1 ELSE 0 END) OVER (
            PARTITION BY account_id
            ORDER BY months_on_book
            ROWS BETWEEN CAST($probation_months AS INTEGER) PRECEDING
                     AND CURRENT ROW
        ) = 1                                               AS in_or_recently_in_sicr
    FROM flagged
)
SELECT
    account_id,
    date_key,
    reporting_date,
    months_on_book,
    months_remaining,
    segment,
    arrears_bucket,
    days_past_due,
    scheduled_balance,
    lifetime_pd_now,
    lifetime_pd_at_origination,

    is_credit_impaired,
    trigger_dpd_backstop,
    trigger_relative_pd,
    triggered_now,
    in_or_recently_in_sicr,

    CASE
        WHEN is_credit_impaired        THEN 3
        WHEN in_or_recently_in_sicr    THEN 2
        ELSE                                1
    END                                                     AS stage,

    -- WHY the account is where it is. An auditor asking "why is this account
    -- in Stage 2?" needs an answer per account, and reconstructing it later
    -- from thresholds is guesswork. The reason is recorded when it is known.
    CASE
        WHEN is_credit_impaired                 THEN 'credit-impaired (90+ DPD)'
        WHEN trigger_dpd_backstop               THEN '30-day backstop'
        WHEN trigger_relative_pd                THEN 'lifetime PD increase'
        WHEN in_or_recently_in_sicr             THEN 'in probation after cure'
        ELSE                                         'performing'
    END                                                     AS stage_reason
FROM probation;
