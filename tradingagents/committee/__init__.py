"""The investment committee: specialist analysts and a senior financial director.

Five analysts each compute their facts in code and have a model read them:
fixed income (reserve, maturities, issuers, guarantees), equities (the dividend
rule of ``tradingagents.dividend_portfolio`` with its alerts), risk (how the
current and target mix fared in past crises, concentration), tax (exemptions,
the regressive table, which money to move first) and macro (rates, inflation and
expectations from the central bank, as context). The financial director reads
the investor's written policy and the five reports and decides, within the
policy, what to recommend; code checks every decision against the policy and
falls back to the rule where it does not hold. The investor approves.

What the backtests support guides the design (``experiments/br_baseline``): a
rule, not the model, chooses the stocks; the model reads, weighs, explains and
catches risks. A shadow record keeps the director's choices next to the rule's,
so whether its discretion helps is measured from here on.
"""
