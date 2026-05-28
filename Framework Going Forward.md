# A Regime-Adaptive, Sector-Specific Stock Screening Framework for Position Trading and Long-Term Value Investing

## TL;DR
- **The current US macro regime (May 2026) is stagflationary late-cycle**: 2.0% Q1 GDP growth, 3.8% headline CPI / 2.8% core, 4.3% unemployment, Fed funds held at 3.50–3.75% with four FOMC dissents (the most since 1992), 10Y at ~4.67% (16-month high), WTI above $100 due to the US-Iran war and Strait of Hormuz closure, and a $1.9T federal deficit at 101% debt/GDP. This combination — sticky inflation + decelerating real growth + tight financial conditions + huge fiscal deficits + AI capex boom — punishes long-duration unprofitable growth and rewards real-asset, pricing-power, and short-duration cash-flow businesses.
- **A one-size 35/30/20/15 valuation/quality/growth/sentiment weighting is structurally wrong** for at least 4 of the 11 GICS sectors (Financials, Real Estate, Energy, Utilities) and arguably 3 more (Communication Services, Healthcare, Technology). The screener must (a) compute sector-specific metrics (P/TBV+ROTCE for banks; P/FFO+AFFO payout for REITs; EV/EBITDAX+reserve life for E&P; EV/Sales+Rule of 40 for SaaS), (b) rank within GICS sub-industries not across the whole market, and (c) tilt sector exposure based on a small set of macro state variables (10Y-2Y, real yields, ISM, USD, oil, credit spreads).
- **Kyle's scripts almost certainly suffer from survivorship bias, look-ahead bias, sector-neutral metric mismatch, negative-earnings handling problems, and a static factor weight that ignores regime.** The five highest-impact fixes in order: (1) point-in-time fundamentals lagged ≥45 days; (2) sector-specific metric maps and sub-industry percentile ranks; (3) explicit value-trap screens (Piotroski F-score ≥ 7 within deep value, Altman Z-score ≥ 2.99, accruals quality, net debt/EBITDA caps); (4) regime-conditional sector tilts driven by a four-variable state classifier; (5) walk-forward backtest using point-in-time Russell 3000 membership with realistic costs.

---

## Key Findings

1. **Regime in May 2026 is reflationary stagflation, not Goldilocks.** Headline CPI re-accelerated from 2.4% in February to 3.3% in March to 3.8% in April; real wages turned negative YoY for the first time since April 2023; ISM Manufacturing held at 52.7 but the Prices Paid sub-index surged to 84.6 (highest since April 2022). The April 29, 2026 FOMC vote of 8-4 — the most dissents since October 1992 — signals an institution at the edge of its policy frame.

2. **Fiscal trajectory is unsustainable and is itself a stock-market regime.** CBO's February 2026 baseline puts debt/GDP at 101% today and 120% by 2036, surpassing the 1946 post-WWII peak of 106%; interest costs cross $1T in FY2026 and $2T by 2036. Term premia are rising on this, and rising term premia structurally punish long-duration equities.

3. **Sector rotation alpha is real but small.** Stangl, Jacobsen & Visaltanachoti (Massey University working paper, December 2009, later published in *Journal of Empirical Finance* 16(5)) document that "even with perfect foresight and ignoring transactions costs, sector rotation generates, at best, a 2.3 percent annual outperformance from 1948 to 2007. In a more realistic setting, outperformance quickly dissipates." Tilt sizes should therefore be modest (≤10 pp from neutral).

4. **Factor performance is regime-conditional.** Asness, Frazzini & Pedersen ("Quality Minus Junk," *Review of Accounting Studies* 24(1), 2019) document a QMJ four-factor alpha of 0.66%/month (≈7.9% annualized) in the US 1956–2016 sample. Piotroski (2000, *Journal of Accounting Research* Vol. 38 Supplement) reported a 23% annual long-short return for high-F-score / high-B/M vs. low-F-score / low-B/M during 1976–1996 — but the strategy materially degraded post-publication. Value drawdowns 2017-Q1 2020 reached 42% (HML) per Alpha Architect citing Fama-French data — the largest in recorded history when extended to 2007.

5. **Sector-appropriate metrics matter enormously.** Damodaran's industry-page guidance (NYU Stern, updated annually): banks/insurance → P/BV + ROE; REITs → P/FFO/AFFO/NAV; energy → EV/EBITDA + reserve valuation; SaaS → EV/Sales scaled by growth-profitability. Per Software Equity Group's 2024–25 SaaS report, public SaaS companies above 40% on the weighted Rule of 40 trade at median 10.7x EV/Revenue — roughly 2–3x sub-threshold peers.

6. **The AI capex super-cycle is now the dominant secular force.** Hyperscaler 2026 capital expenditure has now topped $700 billion combined and is rising (Reuters/Yahoo Finance Morning Bid, May 1, 2026), up from ~$300B in 2025. Meta completed a $25 billion six-tranche investment-grade bond sale on May 1, 2026 (Invezz/Yahoo Finance), the same day it raised full-year AI capex guidance to $125–$145B. This is reshaping demand for Utilities (VST, CEG, NEE), Industrials/power infrastructure (ETN, GE Vernova, ROK, HUBB), Semiconductors (NVDA, AVGO, ASML), and Data Center REITs (EQIX, DLR).

7. **Energy/materials are 2026's leadership; healthcare and financials are laggards.** YTD through May 8, 2026 (per Wespath weekly snapshot citing S&P sector data): Energy +26.0%, Tech +16.7%, Materials +12.9%, Industrials +12.8%, Comm Services +12.3%, Real Estate +11.3%, Staples +10.5%, Utilities +5.6%, Discretionary +3.4%, Financials –5.0%, Healthcare –6.2%. The full-year S&P 500 Energy sector returned +47.4% in the 1-year window through April 30, 2026.

---

## Details

### 1. Current Global and Domestic Economic Status (May 2026)

**US macro snapshot.** Real GDP grew 2.0% SAAR in Q1 2026 (BEA advance estimate, April 30, 2026), rebounding from 0.5% in Q4 2025 — but the rebound was distorted by the post-shutdown federal-payroll snapback and Iran-war defense spending. Oxford Economics' Michael Pearce noted "the core of the economy remained solid in Q1, driven by the AI buildout and the tax cuts beginning to feed through." Q3 2025 had printed +4.4%; the run-rate is decelerating.

Headline CPI rose 3.8% YoY in April 2026 (highest since May 2023), with core CPI at 2.8% (BLS, May 12, 2026). Energy contributed >40% of the monthly gain. EY-Parthenon's Gregory Daco projects "CPI inflation could surpass 4% in May while core inflation approaches 3%." Real average hourly wages slipped 0.5% MoM and 0.3% YoY, the first negative real-wage YoY since April 2023.

Fed funds target held at 3.50–3.75% at the April 29, 2026 FOMC (third consecutive hold). Vote 8-4 — Miran dissented to cut; Hammack, Kashkari, and Logan dissented against dovish "easing-bias" language. This was the most FOMC dissents since October 1992. Powell's chairmanship ended May 15; Kevin Warsh runs the June 16–17 meeting. The March 2026 dot plot implied one cut in 2026 and another in 2027 to ~3.1% neutral, though markets now price ~30–50% odds of a hike by December.

The 10Y closed at 4.67% on May 19, 2026 (16-month high); the 30Y at 5.2% (18-year high); 2Y ~4.09%. The 10Y-2Y spread is ~58 bps positive (re-steepened from prior inversion — historically a recession-warning signal once inversion ends).

Unemployment held at 4.3% in April (BLS, May 8, 2026); nonfarm payrolls +115k; labor force participation 61.8% (lowest since October 2021); U-6 at 8.2%. Chicago Fed's Goolsbee described the market as "stable without being good — low-hire, low-fire."

ISM Manufacturing was 52.7 in April 2026 (4th consecutive expansion month, matching the August-2022 high); new orders 54.1, employment 46.4 (contraction), Prices Paid 84.6 (highest since April 2022). Per ISM's Susan Spence, this reading "corresponds to a 1.8% increase in real GDP on an annualized basis."

**Fiscal/monetary backdrop.** CBO's February 2026 baseline projects a $1.9T deficit (5.8% of GDP) for FY2026 and debt-to-GDP rising from 101% to 120% by 2036, surpassing the 1946 record of 106%. Interest costs cross $1T in FY2026 and $2T by 2036. The 2025 reconciliation act (OBBBA) made TCJA permanent and added an estimated $2.4T to deficits before interest, partially offset by tariff revenues (Supreme Court ruled IEEPA tariffs unlawful in February 2026 but Section 232/301 remain).

Total Fed assets are at ~$6.7T (May 13, 2026 per American Action Forum tracker). QT ended December 1, 2025; the Fed resumed Reserve Management Purchases (~$40B/month T-bills through mid-April) to keep reserves "ample" (~$3.0T). This is not QE in spirit but is mildly liquidity-supportive at the margin.

**Global.** China's Q1 2026 GDP grew 5.0% YoY (NBS, April 16, 2026), above 4.8% consensus, driven by industrial output (+6.1%) and trade (imports +22.7%, exports +14.7%); consumption remains weak (retail sales +2.4%). Beijing targets 4.5–5.0% full-year growth. KPMG and Merics both note 2026 is the start of the 15th Five-Year Plan. Europe is growing weakly; the energy shock is biting harder than in the US. MSCI Emerging Markets is +14.4% YTD through May 8, 2026.

**Commodities/geopolitics.** Bloomberg Commodity Index +29.4% YTD May 8; WTI +77.5% YTD (~$108/bbl); Brent ~$110–114 with peaks near $114 the prior week. Saudi Aramco CEO Amin Nasser warned a Strait of Hormuz delay past mid-June pushes oil-market normalization into 2027. President Trump's May 2026 dismissal of Iran's counter-proposal as "garbage" and warning the ceasefire is on "life support" kept risk premium elevated.

**Regime classification.** On four state dimensions: cycle stage is late-cycle (ISM>50 but decelerating; unemployment rising; curve re-steepening after long inversion); inflation regime is reflationary/sticky; liquidity is tight policy rate with ample reserves, net neutral-to-mildly-tightening; risk appetite is risk-on at index level (S&P +6.0% YTD) with widening sector dispersion. **The screener must be built for a stagflationary late-cycle with rate volatility, energy shock, AI capex super-cycle, and unsustainable fiscal trajectory.**

### 2. Fiscal Trends 1996–2026 and Their Sector Consequences

| Period | Fiscal Stance | Monetary Stance | Inflation | Leading Sectors | Lagging Sectors |
|---|---|---|---|---|---|
| 1996–2000 (Clinton surpluses) | Surplus FY1998–2001 (peak +2.4% GDP) | Greenspan tightening then easing | ~2–3% | Tech, Telecom, Comm Services | Energy, Materials, Utilities |
| 2001–2007 (Bush tax cuts, war spending) | Deficits 2–3% GDP | Easy → tightening to 5.25% | ~2–3% | Energy, Materials, Financials, REITs | Tech (post-bubble), Comm Services |
| 2008–2009 (GFC) | TARP, ARRA ($787B at CBO scoring, revised to ~$840B in CBO's final 2015 report) | Fed cut to 0%, QE1 | Deflationary then recovery | Healthcare, Staples, Treasuries | Financials (-83% peak), REITs |
| 2010–2015 (ZIRP, QE2/QE3) | Deficits narrowing, sequestration 2013 | ZIRP, QE | <2% | Consumer Discretionary, Tech, REITs, Biotech | Energy (post-2014 crash), Materials |
| 2016–2019 (TCJA) | TCJA Dec 2017 cut corporate rate 35→21% | Gradual hikes to 2.5%, then 2019 cuts | ~2% | High-tax-domestic, Banks (briefly), Tech (FAANG) | High-international-exposure, EM, Materials |
| 2020–2021 (COVID) | ~$5T fiscal (CARES, ARP) | QE infinity, ZIRP | Surging from late 2020 | Tech, Discretionary, Homebuilders, Crypto-proxies | Energy (2020), Banks, Travel |
| 2022–2023 (Inflation shock, hiking cycle) | Deficits 5–6% GDP | 525 bps hikes in 16 months, QT | 9.1% peak | Energy (+65% in 2022), Defense, Pharma, Staples | Tech (–33% NASDAQ 2022), REITs, Long-duration bonds |
| 2024–2025 (Disinflation, AI capex) | Deficits sticky ~6% GDP | 100 bps of cuts, QT continuing then ending Dec 2025 | 2.4–3% | Tech (Mag 7), AI infra, Utilities (AI power), Comm Services | Healthcare, Small-caps, Energy (until late 2025) |
| 2026 YTD (Stagflation + Iran war) | $1.9T deficit, OBBBA + tariffs | Hold at 3.5–3.75%, ample reserves | 3.8% headline, sticky | Energy, Materials, Utilities, Defense, Power infra | Healthcare, Banks, Discretionary (consumer-rate-sensitive) |

**Key empirical regularities:**

1. **Sector rotation alpha is real but small after costs.** Stangl/Jacobsen/Visaltanachoti's 2.3% perfect-foresight ceiling implies tilt sizes should be modest (≤10 pp).

2. **Inflation regimes invert leadership.** Energy/Materials outperformed by ~10 pp annualized in the 1970s and 2022; underperformed by similar magnitudes in 1996–2000 and 2014–2020. Long-duration growth (Tech, Biotech) is the mirror image.

3. **QE was a duration trade.** From 2009–2021, every QE pulse compressed real rates and lifted highest-duration assets — unprofitable Tech, long-dated growth, REITs. QT 2022–2025 reversed this: ARKK –77% peak-to-trough; KRE regional-bank ETF fell ~30% in the first two weeks of March 2023 (per etf.com NYCB-article reporting on the SVB crisis).

4. **Fiscal deficits without monetary accommodation produce reflation and curve steepening** — exactly the 2026 setup. Historically (1970s, 2022) this favors short-duration, real-asset, and pricing-power businesses.

5. **TCJA (2017) and OBBBA (2025) winners.** Per Hanlon, Hoopes & Slemrod and follow-on event-study research (Wagner et al., *Journal of Banking & Finance*), highly-taxed domestic firms benefited (top quartile by effective tax rate earned ~2.77% abnormal return over the seven-day event window); high-interest-expense and high-international-mix firms were relative losers; SALT cap hurt high-tax-state housing. Brookings (Auerbach & Gale 2026 update) shows TCJA-induced investment was modest and reallocated rather than purely additive.

### 3. Granular Sector and Sub-Industry Deep Dive

For each sector I list the primary valuation metric, quality metric, growth metric, sub-industry KPIs, macro sensitivities, classic value traps, and what "great" looks like.

**3.1 Energy (XLE; +32.4% YTD 2026)**
- *Integrated O&G (XOM, CVX, SHEL):* EV/EBITDA (4–6x cycle midpoint), debt/capital <30%, ROCE >12% through-cycle, FCF breakeven <$50/bbl WTI. Quality = capital discipline (cap-ex/operating cash flow <50%). Great = Exxon's Permian + Guyana cost structure with ~$35/bbl breakeven.
- *E&P (EOG, COP, FANG):* EV/EBITDAX, reserve life (R/P >10y), F&D cost <$15/boe, cash margin per BOE, debt/EBITDA <1.5x. Traps: highly-leveraged single-basin operators when oil rolls over; hedged-away producers in rising-price regime.
- *Refining (VLO, PSX, MPC):* EV/EBITDA, crack-spread sensitivity, complexity index, utilization >90%. Cyclical — short P/E in trough, long P/E at peak. Watch 3-2-1 crack and WTI-Brent spread.
- *Midstream (ENB, ET, WMB, EPD):* EV/EBITDA 8–10x, distribution coverage >1.5x, leverage <4.5x, fee-based revenue >85%. Treat like utilities — rate-sensitive. P/DCF >12x = expensive.
- *Oilfield Services (SLB, HAL, BKR):* Operating leverage to rig count; P/Book, revenue/employee, ROIC through-cycle 5–15%. Avoid debt-laden land-drilling pure-plays.
- *Renewables (NEE, BEP, ENPH, FSLR):* P/E + 5Y EPS CAGR, project IRR vs WACC spread, IRA-tax-credit exposure. Per FactSet Q1 2026 Utilities preview, the Trump administration paused offshore-wind lease sales and federal-water permitting on Day 1, with later stop-work orders — a falling-knife sector.

*Macro drivers:* Oil price, OPEC+ supply discipline, US inventory levels, geopolitical risk premium. Current regime is bullish energy while Strait of Hormuz is closed; key risk is sudden ceasefire/deal that collapses the risk premium.

**3.2 Materials**
- *Chemicals (LIN, APD, SHW, DOW, LYB):* EV/EBITDA, ROCE >15% for specialty; 6–10% for commodity. Specialty (paints, gases) gets premium. Commodity chems trough at 4x EV/EBITDA, peak at 9x.
- *Metals & Mining (FCX, NEM, BHP, NUE):* EV/EBITDA, cash cost per ton, all-in sustaining cost (AISC) vs spot, reserve life and grade. Avoid single-mine names and high-cost producers.
- *Gold miners (NEM, GOLD, AEM):* P/NAV, AISC <$1,200/oz, reserve life >15y, FCF yield. Gold rerates on real-rate declines — currently challenging given elevated TIPS yield.
- *Construction materials/aggregates (VMC, MLM, EXP):* EV/EBITDA, ROIC, price/ton trajectory (oligopolistic pricing power), infrastructure-bill exposure. Local market share = moat.
- *Containers/packaging, Paper:* ROIC, FCF yield, debt/EBITDA. Low-growth; screen on capital return and contract length.

"Great" = wide-moat, low-cost-position, oligopolistic pricing (Linde, Vulcan). Trap = highly-leveraged commodity producers near cycle peak.

**3.3 Industrials**
- *Aerospace & defense (LMT, RTX, NOC, GD, BA, GE):* P/E + EV/EBITDA; backlog/revenue >2.5x = strong; FCF conversion; ROIC. Defense premium in current regime. Boeing remains a special situation — avoid until 737 MAX cash conversion proves sustainable.
- *Machinery (CAT, DE, ETN, ITW):* Through-cycle ROIC >15% for great names; book-to-bill, dealer inventory, end-market mix. Eaton/ITW = premium quality.
- *Rails (UNP, CSX, NSC, CP):* Operating ratio <60% = excellent; revenue per carload; fuel-surcharge mechanism. Pricing-power oligopoly; reliable compounders.
- *Trucking (ODFL, JBHT, KNX):* Operating ratio, tonnage trends, intermodal/dedicated mix. ODFL = best-in-class LTL.
- *Airlines (DAL, LUV, UAL):* PRASM, CASM-ex-fuel, load factor, debt/EBITDAR. Notoriously bad through-cycle returns — Buffett's classic mistake. Currently squeezed by jet fuel.
- *Shipping (ZIM, MATX):* P/B + cycle position; EV/EBITDA cyclical. Don't anchor on TTM P/E (it explodes at the peak right before the crash).
- *Electrical equipment / power infra (ETN, ROK, HUBB, GE Vernova):* One of the best places to be in 2026 — AI data-center capex + grid modernization. EV/EBITDA + backlog growth + ROIC trajectory.

**3.4 Consumer Discretionary**
- *Auto manufacturers (TSLA, GM, F, TM, STLA):* EV/EBITDA, operating margin, unit volumes, ASP, EV mix, inventory days. Tesla = growth multiple; Ford/GM = cyclical at 5–7x earnings. Tariff exposure substantial.
- *Auto parts (APTV, BWA, LEA, MGA):* EV/EBITDA, content-per-vehicle growth, EV/ICE transition.
- *Specialty/e-commerce/broadline retail (AMZN, HD, LOW, TJX, COST, WMT):* EV/Sales × operating margin trajectory for growers; P/E + SSS + ROIC for mature. Costco/Walmart are quality compounders; Amazon retail margin is the swing variable.
- *Restaurants (CMG, MCD, SBUX, QSR, DPZ):* Unit economics (cash-on-cash returns >25%), SSS, AUV growth, unit count; royalty model preferred. Chipotle great; struggling casual dining = trap.
- *Hotels/leisure (MAR, HLT, BKNG, RCL):* RevPAR, EBITDAR multiples, capital-light franchise mix.
- *Homebuilders (DHI, LEN, NVR, PHM):* P/Book + ROE, gross margin, community count, backlog. NVR's land-option model = wide moat. Rate-sensitive — currently a headwind.
- *Apparel (NKE, LULU, RL):* P/E + organic growth, gross margin trajectory, inventory days, DTC mix. Beware fashion risk and channel-fill traps.

**3.5 Consumer Staples**
- *Food producers (GIS, KHC, K, CPB):* EV/EBITDA, organic volume growth (currently negative — GLP-1 headwind), gross margin, brand share. Most are value traps with declining volumes.
- *Beverages — alcoholic (DEO, STZ, BUD):* P/E + organic growth, premiumization mix. STZ benefits from beer share gains.
- *Beverages — non-alc (KO, PEP, MNST, KDP):* P/E + organic volume + price/mix; >5% organic = great. Coke and Monster are wide-moat compounders.
- *Household/personal care (PG, CL, CLX, CHD, EL, UL, KMB):* P/E + organic growth, ROIC >25%. P&G the gold standard. Estée Lauder = value trap until China beauty + travel-retail clear.
- *Food retail (KR, WMT, COST, ACI):* SSS, gross margin, e-commerce mix.
- *Tobacco (PM, MO, BTI):* FCF yield, payout ratio, next-gen mix (IQOS, ZYN). Philip Morris (PM) is a quality growth story masquerading as tobacco.

**3.6 Healthcare (–6.2% YTD 2026 — sector laggard)**
- *Big pharma (LLY, MRK, JNJ, PFE, ABBV, BMY, NVS, AZN):* P/E + pipeline NPV + patent-cliff (LOE) exposure. Lilly = GLP-1 monster; Pfizer/BMY = LOE-cliff value traps.
- *Biotech (small/mid):* Probability-weighted DCF, cash runway (months at current burn), Phase II/III readout calendar. Most are binary; screen for cash >24 months and at least one Phase III asset.
- *Medical devices (MDT, ISRG, BSX, SYK, ABT, EW):* P/E + organic growth + ROIC. ISRG (da Vinci moat), Stryker, Edwards = great. ROIC >20%, debt/EBITDA <2.0x.
- *Healthcare services / distributors (CAH, MCK, COR):* Low-margin, high-ROIC, oligopolistic. Stable.
- *Health insurance/MCOs (UNH, ELV, CI, HUM, CVS):* P/E + MLR (target <85%), PMPM trends, Medicare Advantage exposure. UNH currently in restructuring/regulatory crisis — special-situation value.
- *Life sciences tools (TMO, DHR, A, WAT):* Quality compounders historically; currently de-rating on China/biotech-funding weakness. EV/EBITDA + organic growth + ROIC.
- *Hospitals (HCA, UHS, THC):* EV/EBITDA, admissions, payer mix.

**3.7 Financials (–5.0% YTD 2026 — laggard)**
- *Money center banks (JPM, BAC, C, WFC):* P/TBV and ROTCE paired — JPMorgan reported a 23% ROTCE in Q1 2026 (JPM 1Q26 press release April 14, 2026: "Return on tangible common equity (ROTCE) of 23%, demonstrating strong capital efficiency," on $16.5B net income and $50.5B managed revenue). The justified-multiple formula is P/TBV = (ROTCE − g)/(r − g). Also: CET1 ≥ regulatory minimum + 100 bps; efficiency ratio target <60% (JPM ~52%); provision coverage >200%. Avoid sub-1.0x P/TBV without asset-quality story.
- *Regional banks (USB, PNC, TFC, MTB, FITB):* Same metrics plus CRE exposure (avoid >25% loans concentration), deposit beta, and unrealized AFS losses as % of TCE. The 2023 SVB lesson remains live.
- *Investment banks/brokers (GS, MS):* ROE + book-value growth, capital-markets cycle position, debt/equity, VaR.
- *Insurance — P&C (CB, TRV, PGR, ALL, AIG):* Combined ratio <95%, reserve development (favorable preferred), BV growth + dividend yield, P/B. Progressive = compounder; Chubb = quality.
- *Insurance — life (MET, PRU, LNC, AFL):* P/Book, ROE, statutory capital, spread compression. Aflac and Met are core; LNC special situation.
- *Asset managers (BLK, BX, KKR, APO, BAM, TROW, BEN):* P/AUM + organic flows + management-fee margin + performance fees. Alts (BX, KKR, APO) = secular growth at 20–30x. Active managers (TROW, BEN) = melting ice cubes.
- *Exchanges (CME, ICE, NDAQ, CBOE):* Recurring revenue, EBITDA margin >50%, network-effect moats.
- *Fintech (V, MA, FI, PYPL, SQ, ADYEY):* V/MA = monopoly duopoly at 25–30x. PayPal and Block are show-me stories.

**3.8 Information Technology**
- *Software — SaaS (MSFT, CRM, ADBE, NOW, WDAY, SNOW, DDOG):* EV/Sales scaled by Rule of 40 (revenue growth % + FCF/EBITDA margin %). Per Software Equity Group's 2024–25 SaaS report, public SaaS companies scoring >40% on a Weighted Rule of 40 posted a median EV/Revenue multiple of 10.7x vs. ~3–5x for sub-threshold peers. Add net revenue retention >120% = great; CAC payback <18 months; gross margin >75%. McKinsey's August 2021 study confirms only ~16% of public software firms cleared Rule of 40 in any given year 2011–2021, with median top-quartile ARR growth 45% and net retention 130%.
- *Software — infrastructure (ORCL, NOW, PANW, CRWD, ZS, NET):* Same framework; ARR growth, NRR.
- *Semiconductors (NVDA, AVGO, AMD, INTC, MU, TXN, ON, MCHP):* P/E + revenue growth + gross margin; GM = key moat indicator (NVDA ~75%, INTC ~30%). Cyclical — book-to-bill, inventory days. NVDA tied to hyperscaler capex.
- *Semi equipment (ASML, AMAT, LRCX, KLAC):* P/E + WFE growth, China revenue risk. ASML monopoly on EUV.
- *Hardware (AAPL, DELL, HPQ, ANET):* P/E + services mix (AAPL) or AI-server exposure (DELL, ANET); CSCO melting ice cube.
- *IT services (ACN, IBM, INFY, WIT):* Headcount efficiency, organic growth, EBIT margin. Challenged by GenAI productivity disruption.
- *Payment processors (V, MA, FI, FIS):* V/MA covered above; FI/FIS lower-quality acquirer roll-ups.

**3.9 Communication Services**
- *Telecom (T, VZ, TMUS):* EV/EBITDA 6–8x, FCF yield, post-paid net adds, churn, leverage <3.5x net debt/EBITDA. T-Mobile is the growth name.
- *Traditional media (WBD, PARA, DIS, FOX):* EV/EBITDA, streaming subs vs. linear decline, debt/EBITDA. WBD/PARA = deep value/restructuring; DIS = special situation.
- *Entertainment (NFLX, DIS, ROKU, SPOT):* P/E + sub growth + ARPU + content ROI. Netflix pricing power proven post-2023.
- *Interactive media (GOOGL, META):* DCF + EV/EBITDA + capex visibility. Meta completed a $25 billion six-tranche investment-grade bond sale on May 1, 2026 (per Yahoo Finance/Invezz April 30, 2026), "the same day it raised its full-year AI capital expenditure guidance to $125 billion–$145 billion." Treat META/GOOGL as quality compounders at 20–22x with capex-payback the key question.
- *Gaming (EA, TTWO, RBLX, NTDOY):* User engagement, live-services mix, hit-driven volatility.
- *Advertising (OMC, IPG, TTD):* Organic growth, EBITDA margin, programmatic share.

**3.10 Utilities**
- *Regulated electric (NEE, SO, DUK, AEP):* P/E + rate-base growth + allowed ROE + payout ratio. Rate-base growth 5–8% = great; allowed ROE 9–10%. AI-data-center demand has changed the secular growth profile.
- *Gas utilities (ATO, SR), Water (AWK, WTRG):* Premium-valued for moat and acquisition runway.
- *Independent power producers / renewables utilities (VST, CEG, NRG, TLN):* EV/EBITDA + capacity factor + PPA contract length. Per FactSet's Q1 2026 Utilities preview, Vistra (VST) was projected to be the largest single contributor to S&P 500 Utilities sector Q1 2026 earnings growth (EPS estimate $1.28 vs. –$0.92 prior year); excluding VST, sector earnings growth falls from 9.6% to 5.3%.
- *Macro driver:* 10Y yield (negative correlation), regulatory ROE, electricity demand growth (AI tailwind).

**3.11 Real Estate (REITs)**
Never use P/E — depreciation distorts net income. Primary metrics:
- P/FFO and P/AFFO (sub-sector benchmarks: data centers/industrials 20–30x AFFO; office/retail 8–14x).
- AFFO payout ratio: <80% sustainable, >90% dividend-cut risk.
- Premium/discount to NAV (Green Street Advisors estimates).
- Net debt/EBITDA <6.5x for investment-grade.
- Same-store NOI growth; WALT.

Sub-sector outlook 2026:
- *Residential (AVB, EQR, ESS, MAA, INVH):* 3–5% same-store NOI; supply peaking; coastal premium over Sunbelt.
- *Office (BXP, VNO, SLG):* Avoid most — WFH headwinds, debt walls. Trophy assets only.
- *Industrial (PLD, EGP, REXR):* Slowing from 8% NOI growth to 3–4%; still high-quality.
- *Retail (SPG, REG, KIM, FRT):* Outperforming on tenant strength; Class A malls and strip centers compelling.
- *Data center (EQIX, DLR):* AI tailwind, but rich valuation; supply constraints in key markets.
- *Healthcare (WELL, VTR, OHI):* Senior housing recovery — Welltower (WELL) is core holding.
- *Specialty/tower (AMT, CCI, SBAC):* Rate-sensitive; debt walls; carrier-spending dependent.

REIT trap: high dividend yield + AFFO payout >100% + falling NAV = cut imminent.

### 4. Audit of `market_screener.py` and `backtest.py`

Without source-code access, I infer the architecture from your description: a multi-factor model scoring 1–100 with sector-rank-percentiled sub-scores at 35/30/20/15 across Valuation/Quality/Growth/Sentiment. Most likely structural flaws, ranked by impact on out-of-sample performance:

**A. One-size-fits-all factor weights ignore sector epistemology.** Banks/insurers should be ranked on P/TBV + ROTCE, not P/E and FCF yield. REITs require P/FFO + AFFO payout + NAV. E&Ps need reserve life + EV/EBITDAX + breakeven oil price. SaaS need Rule of 40 + NRR. Switch from sector-rank-percentile of generic metrics to sector-specific metric definitions via a GICS sub-industry → metric-list lookup.

**B. Sub-industry granularity instead of sector-level ranking.** Ranking all Financials together pools JPMorgan with Berkshire and Visa. Use the 11 sectors → 24 industry groups → ~70 industries → ~150 sub-industries hierarchy. Rank within sub-industry where N ≥ 8; otherwise step up to industry group.

**C. Static weights ignore macro regime.** In stagflationary 2026, sentiment/momentum should be upweighted, valuation moderated. In disinflationary easing regimes, growth and quality should dominate; in recessions, quality and low-vol; in reflationary recovery, value and small-cap.

**D. Negative-earnings handling.** If the screener excludes NaN/negative E as "missing," it likely excludes biotechs, early-stage SaaS, and turnarounds — a survivorship-style bias toward established earners. Use EV/Sales × Rule-of-40-style blend for negative-earnings names; rank them in a separate "early-stage" bucket.

**E. Value-trap screen is absent.** Cheap with deteriorating quality is the worst sub-decile. Add Piotroski F-score: Piotroski (2000, *Journal of Accounting Research* Vol. 38 Supplement) abstract: "an investment strategy that buys expected winners and shorts expected losers generates a 23% annual return between 1976 and 1996." Add Altman Z-score: Altman (1968, *Journal of Finance* Vol. 23 No. 4, pp. 589–609) reported 94% accuracy one year prior to bankruptcy and 72% two years prior, with cutoffs Z>2.99 "safe" and Z<1.81 "distress." Hard-screen out Z<1.81 and F<5 from any "value" bucket. (Caveat: Piotroski's post-publication out-of-sample performance has degraded materially.)

**F. Survivorship bias in backtest.** Pulling current S&P 500/Russell 3000 membership for history excludes delistings/bankruptcies/acquisitions — biases returns up by ~1–2% annually. Use point-in-time index membership.

**G. Look-ahead bias on fundamentals.** Companies file 10-Q ~45 days after quarter-end and 10-K ~60–90 days after year-end. Lag fundamentals by ≥45 days; better, use SEC filing date.

**H. Sentiment factor specification.** Decompose into (i) earnings revisions (Δ NTM EPS, last 90 days), (ii) price momentum (12-1 month return), (iii) short interest change. Earnings-revision breadth is the most robust single signal (Chan, Jegadeesh, Lakonishok 1996, *Journal of Finance*).

**Backtest methodology flaws** likely present: in-sample optimization (use walk-forward); insufficient transaction costs (add 15–25 bps liquid large/mid-cap, 50+ bps small-cap); rebalancing too frequently (quarterly cuts turnover ~3x with minimal loss for value/quality); no capacity/liquidity caps; no multiple-testing correction (apply White's reality check or Hansen's SPA). Quality red-flag screens likely missing: Net Operating Asset growth >10% (accruals — Sloan 1996, *Accounting Review*); accruals quality (TA-CFO); goodwill > 50% of equity; stock-based comp >10% of revenue; restated financials; 8-K material weakness.

### 5. Unified, Realigned Framework

**5.1 Architecture: 5-Layer Pipeline**

```
LAYER 1: Universe selection (point-in-time, liquidity-filtered)
    ↓
LAYER 2: GICS sub-industry classifier → metric map
    ↓
LAYER 3: Composite score (sector-specific weights, percentile-ranked WITHIN sub-industry)
    ↓
LAYER 4: Regime overlay (sector tilt + factor-weight modulation)
    ↓
LAYER 5: Risk screens (value-trap, accruals, leverage caps)
    ↓
Portfolio construction (sized by score × inverse-vol × liquidity)
```

**5.2 Sector-Specific Metric Maps**

| Sector | Valuation (primary) | Quality (primary) | Growth (primary) | Killer KPI | Hard exclude if... |
|---|---|---|---|---|---|
| Energy E&P | EV/EBITDAX | ROCE through-cycle | Production growth | Reserve life, breakeven WTI | Debt/EBITDA >2.5x |
| Energy Midstream | EV/EBITDA | Distribution coverage | Throughput growth | Fee-based % | Coverage <1.2x |
| Materials (chemicals) | EV/EBITDA | ROIC | Volume + price | Specialty mix % | ROIC <8% three years |
| Industrials | EV/EBITDA + P/E | ROIC, FCF/NI | Backlog growth | Book-to-bill | Aerospace: backlog <1x |
| Cons Disc retail | P/E + EV/Sales | ROIC, inventory turns | SSS growth | SSS, AUV | SSS negative 3Q in row |
| Cons Disc autos | P/E + EV/EBITDA | ROIC, FCF | Unit sales | Operating margin | Auto debt/EBITDA >3 |
| Cons Staples | P/E | ROIC >20% | Organic vol+price | Organic growth | Organic <0% 4Q in row |
| Healthcare pharma | P/E + DCF | ROIC, R&D efficiency | Pipeline NPV growth | LOE cliff | Single-asset risk |
| Healthcare biotech | EV/cash + DCF | Cash runway | Pipeline progression | Phase II/III readouts | Runway <12 months |
| Healthcare devices | P/E + EV/EBITDA | ROIC, GM | Organic growth | Procedure volumes | GM <40% |
| Financials banks | P/TBV + ROTCE | CET1, NPL coverage | TBV/share growth | NIM, efficiency ratio | CET1 <reg min+50 bps |
| Insurance P&C | P/Book + ROE | Combined ratio | BV growth | Combined ratio | CR >100% 3 yrs |
| Asset managers | P/AUM + EV/EBITDA | Organic flows | AUM growth | Fee rate | Net outflows 4Q |
| Tech SaaS | EV/Sales × Rule of 40 | GM, FCF margin | ARR growth, NRR | Rule of 40 | Rule of 40 <20% |
| Tech semis | P/E + EV/EBITDA | GM, ROIC | Revenue + GM trend | Book-to-bill | GM declining 4Q |
| Tech IT services | P/E | EBIT margin, headcount eff | Organic growth | Org. growth ex-FX | GenAI substitution risk |
| Comm telecom | EV/EBITDA + FCF yield | FCF/EBITDA, leverage | Sub net adds | Postpaid churn | Net debt/EBITDA >3.5x |
| Comm media/internet | P/E + DCF | Operating margin | Revenue + user growth | DAU/MAU | Margin compression w/o reinvestment |
| Utilities | P/E + dividend yield | Rate base growth, allowed ROE | EPS growth 5–7% | Capex visibility | Allowed ROE <9% |
| REITs | P/FFO + P/AFFO + NAV discount | AFFO payout, leverage | Same-store NOI | WALT, occupancy | AFFO payout >95% |

**5.3 Regime Classifier — State Variables and Sector Tilts**

State variables (all observable daily, lag where required):
1. Growth: ISM Manufacturing PMI (above 50 = expansion).
2. Inflation: 5y5y forward inflation breakeven OR YoY core CPI.
3. Yield curve: 10Y-2Y Treasury spread.
4. Credit/risk: ICE BofA US High Yield OAS (>500 bps = stress).

Five-regime classification:
- **R1 — Disinflationary expansion (Goldilocks)**: ISM>50, falling CPI, positive curve, tight HY. Overweight Tech, Comm, Discretionary; underweight Energy, Staples.
- **R2 — Reflationary expansion (current paradigm 2026)**: ISM>50, rising CPI, steepening/inverting curve, widening HY. Overweight Energy, Materials, Industrials, defense, real-asset REITs; underweight long-duration Tech, Discretionary, regional banks.
- **R3 — Late-cycle/stagflation**: ISM>50 but decelerating, sticky CPI, flat curve. Overweight Staples, Healthcare quality, Utilities, defense, energy; underweight cyclical Discretionary, low-quality.
- **R4 — Recession**: ISM<50, falling CPI, steepening curve, blown-out HY. Overweight Staples, Utilities, Healthcare; underweight Financials, Discretionary, Materials.
- **R5 — Recovery**: ISM<50 turning up, low CPI rising, steep curve, tightening HY. Overweight Financials, Industrials, Discretionary, small-caps; underweight Staples, Utilities.

**Current regime (May 2026): bordering R2/R3 — reflationary late-cycle.** Tilt energy/materials/utilities/industrials over neutral by 3–7 pp each; tilt Healthcare/Discretionary/Tech under by similar amounts. Cap tilt size at ±10 pp from neutral (Stangl/Jacobsen/Visaltanachoti's 2.3% perfect-foresight ceiling implies small bets are right).

**5.4 Sector-Specific Factor Weights (replacing global 35/30/20/15)**

| Sector | Valuation | Quality | Growth | Sentiment |
|---|---|---|---|---|
| Energy | 40 | 25 | 15 | 20 |
| Materials | 35 | 25 | 20 | 20 |
| Industrials | 30 | 30 | 25 | 15 |
| Cons Disc | 25 | 25 | 30 | 20 |
| Cons Staples | 35 | 35 | 15 | 15 |
| Healthcare (large pharma) | 30 | 30 | 25 | 15 |
| Healthcare (biotech) | 10 | 20 | 50 | 20 |
| Financials (banks) | 40 | 35 | 15 | 10 |
| Financials (insurance) | 35 | 40 | 15 | 10 |
| Financials (asset mgrs) | 25 | 30 | 30 | 15 |
| Tech (SaaS) | 15 | 25 | 45 | 15 |
| Tech (semis) | 25 | 25 | 30 | 20 |
| Tech (hardware/IT svc) | 30 | 30 | 25 | 15 |
| Comm Services | 25 | 30 | 30 | 15 |
| Utilities | 35 | 35 | 20 | 10 |
| Real Estate | 35 | 30 | 25 | 10 |

**5.5 Step-by-Step Strategy by Sector (Current Regime)**

- *Energy:* Long integrated majors (XOM, CVX) and best-in-class E&Ps (EOG, FANG, COP) with breakeven WTI <$50 and Net Debt/EBITDA <1.5x. Trim if WTI breaks $120 — historic demand destruction zone.
- *Materials:* Long quality compounders (LIN, SHW, VMC). Avoid leveraged commodity chems and small gold miners.
- *Industrials:* Overweight aerospace & defense (LMT, RTX, GD), power infra (ETN, GEV, ROK, HUBB), rails (UNP, CSX). Underweight airlines and trucking. Screen: ROIC >15% 5Y, FCF/Net Income >0.9.
- *Consumer Discretionary:* Stay quality (COST, TJX, BKNG, MCD, NVR). Avoid rate-sensitive autos and peak-EPS discretionary. Off-price (TJX, ROST) is recession-resilient.
- *Consumer Staples:* Pricing-power survivors (KO, PEP, MNST, PM, PG, CL). Avoid low-organic-growth packaged food.
- *Healthcare:* Selectively bullish despite sector weakness. Lilly (LLY) and Novo (NVO) for GLP-1; UNH special situation if MLR normalizes; quality compounders (ISRG, EW, SYK, TMO) at discounts to historic multiples. Avoid LOE-cliff pharma (PFE, BMY).
- *Financials:* Trim regional banks (CRE + AFS losses); hold money centers (JPM's 23% ROTCE justifies premium per the April 14, 2026 1Q26 release); overweight quality P&C (CB, TRV, PGR); overweight alternative asset managers (BX, KKR, APO, BAM).
- *Information Technology:* Focus quality compounders (MSFT, AAPL services-mix, NVDA at GARP multiples, AVGO). Avoid unprofitable high-multiple SaaS with Rule of 40 <30% and no path to profitability.
- *Communication Services:* Long META (with awareness that the May 1, 2026 $25B bond issuance funds $125–145B 2026 AI capex — payback math must work), GOOGL, T-Mobile; selectively Netflix; avoid traditional linear media.
- *Utilities:* Overweight AI-power names (VST, CEG, NRG, TLN, NEE). Avoid pure renewables until Trump-administration regulatory clarity. Regulated names (SO, AEP, DUK) are core ballast.
- *Real Estate:* Long Welltower (WELL), Prologis (PLD), Equinix (EQIX), Realty Income (O), best-of-breed apartment (AVB, EQR). Avoid office (BXP, VNO, SLG) and lower-quality strip-mall.

**5.6 Position Sizing for Position Trading vs. Long-Term Value**

| Holding period | Recommended approach |
|---|---|
| Position trade (1–6 months) | Sentiment 30%, Growth 25%, Quality 25%, Valuation 20%. Add 12-1 momentum filter (long top-quintile only). Stop-loss –15% or 50-day MA break. |
| Core long-term (1–5 years) | Valuation 40%, Quality 35%, Growth 15%, Sentiment 10%. Sector-neutral + sub-industry rank. Rebalance quarterly. Hard sell only on thesis break or score drop below 25th percentile sector-wide. |
| Value compounder hybrid (Buffett-style) | Quality 40%, Valuation 30%, Growth 20%, Sentiment 10%. |

**5.7 Predictive Efficiency Optimization**

1. Decay-weight earnings revisions (last 30 days × 2, 31–90 days × 1, older drop).
2. Information coefficient (IC) decay monitoring: factor weights proportional to rolling 3Y IC × IC-stability.
3. Factor orthogonalization: regress each on the others, use residuals.
4. Risk-adjusted sizing: weight ∝ score × (1/idiosyncratic vol) × liquidity_score.
5. Macro-conditional weighting with smooth transitions; in low-confidence regime states, default to neutral weights.

---

## Recommendations — Prioritized Python Restructuring

**Phase 1 — Foundation (Week 1–2)**
1. Add point-in-time fundamentals with `as_of` = max(quarter_end + 45 days, filing_date). Refuse to score if data older than 90 days or younger than 45 days from quarter end.
2. Build the GICS sub-industry → metric-map dispatch as a Python dict / YAML config (see schema below).
3. Switch ranking from sector-level to sub-industry (GICS 8-digit) with N≥8 fallback to industry group.

```python
SECTOR_METRICS = {
  "GICS_4030": {  # Banks
    "valuation": ["P_TBV", "P_E_forward"],
    "quality":   ["ROTCE", "CET1_ratio", "efficiency_ratio_inv", "NPL_coverage"],
    "growth":    ["TBV_per_share_5y_cagr", "loan_growth"],
    "sentiment": ["EPS_revision_90d", "price_mom_12_1"],
    "weights":   {"V": 0.40, "Q": 0.35, "G": 0.15, "S": 0.10},
    "hard_exclude": {"CET1_ratio<reg_min+0.005": True,
                     "AFS_unreal_loss/TCE>0.3": True}
  },
  "GICS_6010": {  # REITs
    "valuation": ["P_FFO_forward", "P_AFFO_forward", "premium_to_NAV"],
    "quality":   ["AFFO_payout_ratio_inv", "net_debt_EBITDA_inv", "occupancy"],
    "growth":    ["same_store_NOI_growth", "AFFO_per_share_5y_cagr"],
    "sentiment": ["FFO_revision_90d", "price_mom_12_1"],
    "weights":   {"V": 0.35, "Q": 0.30, "G": 0.25, "S": 0.10},
  },
  # ... etc. for all sub-industries
}
```

**Phase 2 — Quality Gates (Week 3)**
4. Piotroski F-score (9 binary tests).
5. Altman Z-score (use Z″ for non-manufacturers).
6. Beneish M-score for accruals/earnings-management red flag.
7. Sector-specific leverage screens (banks: CET1; non-financials: Net Debt/EBITDA; REITs: Net Debt/EBITDA + secured-debt ratio).
8. Hard-exclude stocks failing any "killer KPI."

**Phase 3 — Regime Overlay (Week 4)**
9. Five-regime classifier using ISM, 10Y-2Y, YoY core CPI, HY OAS. Logistic regression or rule-based.
10. Sector-tilt vectors (±10 pp max) and factor-weight modulation.
11. State-conditional factor weights with smooth EW-state-probability transitions.

**Phase 4 — Backtest Hardening (Week 5–6)**
12. Point-in-time index membership (CRSP, Norgate, or equivalent).
13. Walk-forward optimization: 5-year train / 1-year test rolling.
14. Transaction costs: 20 bps each side large-cap, 50 bps small-cap, plus market-impact ∝ trade-size/ADV.
15. Realistic capacity constraints (position ≤ 10% × ADV).
16. Reserve 2023–2026 entirely as final out-of-sample validation.
17. Report Sharpe, Calmar, max drawdown, hit rate, decile spread, IC by factor, factor turnover.

**Phase 5 — Production Hygiene (Week 7)**
18. Audit logging: every score reproducible from a single CLI invocation with as_of date.
19. Sensitivity testing: factor weight ±5 pp perturbation; if results swing >20%, model is overfit.
20. Multiple-testing correction: White's reality check or stationary bootstrap.
21. Dashboard: sector exposures, IC decay charts, regime probabilities, top/bottom-quintile constituents with score attribution.

**Phase 6 — Continuous Improvement**
22. Add alternative-data signals carefully (insider buying ratio, short interest change, sector-specific alt data) only after orthogonalization.
23. Maintain a fundamental analyst-override layer: never let the quant force you into a stock with an open SEC investigation or accounting restatement.

**Order-of-operations rule of thumb:**
1. Stop using global 35/30/20/15 weights immediately; hard-exclude Financials and REITs from the current model and run separate single-sector versions until the refactor lands.
2. Audit your data pipeline for point-in-time fidelity (1-day check, potentially reveals 50–100 bps of fake annual alpha).
3. Add Piotroski F-score and Altman Z-score as hard gates within value buckets (one afternoon, biggest immediate dollar value).
4. Implement the GICS sub-industry → metric-map dispatch (central architectural change).
5. Build the 5-regime classifier with the four variables in §5.3.
6. Walk-forward backtest; throw away current results.
7. Reserve 2023–2026 as out-of-sample.

**Thresholds that change the recommendation:**
- If ISM PMI < 48 AND HY OAS > 500 bps → switch to R4 (recession): overweight Staples/Healthcare/Utilities by 7 pp; underweight Financials/Discretionary/Materials by 7 pp.
- If 10Y-2Y inverts > 50 bps for 60 days → cut Financials by another 3 pp.
- If WTI > $120 → trim Energy by 3 pp; expect demand destruction.
- If core CPI < 2.5% two consecutive prints → pivot toward R1 (Goldilocks) weights — overweight Tech/Comm/Discretionary.

---

## Caveats

1. **Sector rotation alpha is fragile.** Even with perfect foresight, the maximum demonstrated outperformance is 2.3% pre-cost (Stangl/Jacobsen/Visaltanachoti 2009). Don't bet large on the regime classifier.
2. **Piotroski's 23% annual long-short return has not held up out-of-sample.** Post-publication (Portfolio123/Seeking Alpha analysis 2021), the long-short version generated approximately −9.5% annualized over the next 10 years and −11.75% over 20 years. Use F-score as a quality filter within value, not as a standalone strategy.
3. **The AI capex super-cycle could be a bubble.** Combined 2026 hyperscaler capex has topped $700 billion (Reuters Morning Bid, May 1, 2026) and is rising — up from ~$300B in 2025. Meta's May 1, 2026 $25 billion six-tranche IG bond sale and $125–145B 2026 capex guide is emblematic; payback math is unproven at this scale. The screener must avoid mechanically loading up on AI infra winners at peak multiples.
4. **Iran war geopolitics is the dominant macro driver.** A ceasefire/deal could collapse oil $30+ overnight, inverting the energy/materials thesis. Risk-manage with explicit position sizing.
5. **Trump-administration policy is high-uncertainty.** OBBBA, tariff legal status (post-Supreme Court IEEPA ruling), Fed independence (Powell staying as governor, Warsh as chair), and immigration enforcement create structural breaks the model will mishandle.
6. **Quality-at-any-price is the strategy that worked 2010–2024.** It may not work in a sticky-inflation, real-rate-positive regime. Premium multiples on "great companies" already discount a return to disinflation that may not arrive.
7. **Backtests overfit.** Even with walk-forward, your model's reported Sharpe will overstate live-trading Sharpe by ~30–50% on average (de Prado 2018). Plan for that.
8. **Data quality is your bottleneck.** Premium fundamental data (Compustat, FactSet, S&P CIQ) is necessary for serious work. Free-source Yahoo Finance has restated/PIT issues that materially distort backtests.
9. **You are one person with two scripts.** Resist over-engineering. A 20-line robust sector-aware screen will beat a 2000-line untuned one. Ship the V2 architecture, then iterate.
10. **Conflicting CBO/CEA fiscal projections.** CBO's February 2026 baseline shows debt rising to 120% of GDP by 2036; the Trump CEA projects debt falling to 94% by 2034 under OBBBA + tariffs + deregulation assumptions. The truth almost certainly lies closer to CBO's baseline, but Treasury issuance, term premia, and equity discount rates are sensitive to which path materializes — your screener should not over-anchor on any single rate path.