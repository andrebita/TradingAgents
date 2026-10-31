"""A rule-based dividend portfolio of large B3 stocks, with the LLM as its risk assistant.

The rules come from the backtests in ``experiments/br_baseline`` (README,
"Dividendos entre as grandes" and "Filtro de armadilhas"): among the 50 most
traded companies, those that paid dividends in each of the last three years,
the 10 with the highest 24-month dividend yield, equally weighted and
rebalanced every June and December. From April 2012 to October 2026 that rule
earned about 11-14% a year against 7.5% for the Ibovespa and 9.8% for DIVO11,
without passing the pre-registered significance threshold, and fell further in
crises (-59% against -47%).

The LLM adds no return in those tests. It reads: each week it summarizes the
filings and press of the holdings with an alert level, and at a rebalance it
estimates, from anonymized statements, the probability of a dividend cut. That
estimate ranked the cuts that followed well (AUC 0.69) but the market had
priced them, so it is shown as an alert and never removes a stock.

Orders are planned to keep monthly sales within the individual investor's
R$ 20,000 income-tax exemption where possible, and a shadow portfolio records
every target so the rule can be judged against BOVA11 and DIVO11 from here on.
"""
