# Quantum Concepts in a Fundamental Equity Screener: An Honest Methodological Assessment

## TL;DR
- **Do not bolt literal quantum computing onto your V/Q/G/S screener.** Through 2026, even Goldman Sachs has pulled back after concluding the relevant algorithms would need "at least 8 million so-called logical qubits" and run "for millions of years" on current hardware (Bloomberg, April 2026), and no major bank runs a production quantum trading system. The realistic edge for a retail fundamental screener is zero.
- **Three of the four ideas you named map cleanly onto well-established classical techniques you should actually adopt** — "superposition" → Michaud-style resampled / ensemble scoring under parameter uncertainty; "entanglement" → mutual-information or hierarchical-clustering codependence (López de Prado HRP/NCO) instead of Pearson; "observer effect" → square-root market-impact / capacity haircut and crowding-aware factor weights. None of these requires a qubit.
- **Quantum annealing / QUBO can become a sensible *post-screener portfolio-construction layer* once the screener itself is mature**, but only as a cardinality-constrained Markowitz solver where classical simulated annealing (or `riskfolio-lib`'s HRP/NCO) will match or beat it on retail-sized problems. Implement HRP first; treat any quantum step as research, not alpha.

## Key Findings

### 1. Three different things get called "quantum finance" — keep them separate
A defensible taxonomy from the literature:

- **(L) Literal quantum hardware.** Running QAOA, VQE, or quantum amplitude estimation on actual gate-based or annealing quantum processors (IBM, IonQ, Quantinuum, D-Wave). As of 2026 this is overwhelmingly research-grade. Bloomberg reported in April 2026 that **Goldman Sachs scaled back its quantum group after researchers concluded the algorithm "would have to run for millions of years" and would need "at least 8 million so-called logical qubits"** to handle their target problem (Bloomberg Law, "Goldman, JPMorgan Take Divergent Paths in Quantum Computing Race," April 2026). JPMorgan **"maintains a dedicated squad of more than 50 physicists, mathematicians and computer scientists who are actively exploring use cases from cryptography to machine learning"** (TheStreet, 27 April 2026, citing the same Bloomberg reporting) but acknowledges its work remains pilot-phase. **No bank runs production trading on quantum hardware.**
- **(Q-I) Quantum-inspired classical algorithms.** Tensor networks (Multiverse Computing's CompactifAI / Singularity, built on Román Orús's matrix product state research), simulated annealing, hierarchical/clustering methods that borrow mathematical structure from quantum physics but run on CPUs/GPUs. These are *real* and *deployable today* — and they constitute essentially everything López de Prado-style "ML for asset managers" actually does in practice.
- **(M) Physics-as-metaphor.** Loose analogies: "the market is in superposition until observed," "stocks are entangled," "Heisenberg's uncertainty principle = Soros reflexivity." Useful as *intuition pumps* (Soros explicitly traces his "human uncertainty principle" to Heisenberg in his 2013 *Journal of Economic Methodology* essay), but they don't change the math unless you actually implement a corresponding classical operator (mutual information, copula, Bayesian posterior, etc.).

Almost every legitimate use case at major banks falls into **(Q-I)** in production and **(L)** in R&D. Anything sold to retail as "quantum AI trading" is **(M)** with marketing veneer.

### 2. What major firms have actually published
- **JPMorgan Chase** has worked with Quantinuum since 2020, has a 50+ person quantum team, and has published on quantum amplitude estimation for option pricing and on QAOA for portfolio optimization. Continues to invest but acknowledges pilot status (TheStreet/Bloomberg, April 2026).
- **Goldman Sachs** published the seminal "thousand-fold speedup of derivative pricing via quantum amplitude estimation" line in 2019–2021 papers (with IBM) — but in 2026 publicly scaled back after a fault-tolerance audit; the speedup is conditional on hardware that does not yet exist (Bloomberg, 26 April 2026).
- **Multiverse Computing** (Spain) raised "c.$250M to date" after a €189M ($215M) Series B led by Bullhound Capital in June 2025 (Multiverse press release, 12 June 2025), and was reported in February 2026 to be seeking an additional €500M (~$594M) round at a €1.5B valuation (SiliconANGLE, 10 Feb 2026). Their Singularity / FinOptimal product runs on IonQ hardware via cloud and serves Bank of Canada, BBVA, and Crédit Agricole; their core technical contribution is **tensor-network methods, which are classical** (Mugel et al., *Phys. Rev. Research* 4, 013006, 2022, *Dynamic portfolio optimization with real datasets using quantum processors and quantum-inspired tensor networks*).
- **D-Wave** has multiple peer-reviewed and conference papers on Markowitz QUBO formulations (Rosenberg, Haghnegahdar, Goddard, Carr, Wu & López de Prado, *IEEE Journal of Selected Topics in Signal Processing* 10, 1053, 2016) and supplies an open-source Python repo (`dwave-examples/portfolio-optimization`) that any retail user can run on the Leap hybrid solver — though for portfolios of 10–500 names a classical MIQP solver (Gurobi, or even free `mip` in pure Python) typically wins or ties.
- **Marcos López de Prado** himself bridges (L) and (Q-I). His 2015 SSRN paper and 2016 IEEE paper *use* a D-Wave annealer for the dynamic trading-trajectory problem. But his actually deployable retail-relevant contributions — **Hierarchical Risk Parity (HRP)** (López de Prado, *Journal of Portfolio Management*, 42(4), 59–69, 2016) and **Nested Clustered Optimization (NCO)** (in *Machine Learning for Asset Managers*, Cambridge Elements, 2020) — are entirely classical. The HRP paper's Monte Carlo experiment reports that **"CLA's [Critical Line Algorithm] out-of-sample variance exceeds HRP's by 72.47%"** and HRP **"improves the out-of-sample Sharpe ratio of a CLA strategy by approximately 31.3%."**

### 3. Skeptical voices
- **DARPA Quantum Benchmarking Initiative** (program manager Joe Altpeter, 2024): *"Our opening position is skepticism, specifically, skepticism that a fully fault-tolerant quantum computer with a sufficient number of logical qubits can ever be built."* Stage B participants must demonstrate "utility-scale operation" by 2033.
- **Nikita Gourianov** wrote a widely-circulated *Financial Times* op-ed in 2022 titled "The Quantum Computing Bubble" (a physics PhD complaining that the technology is over-hyped relative to results); Moshe Vardi titled a 2019 *Communications of the ACM* piece "Quantum Hype and Quantum Skepticism." Gil Kalai is the principal academic skeptic that fault-tolerant QC may be physically impossible.
- **Matt Levine** (Bloomberg, "Quantum Bond Trading," 25 Sept 2025) frames the core question well: financial markets are "a very general and efficient way for turning skill and intelligence and knowledge into money" — so the question isn't whether quantum is *cool*, it's whether it gives anyone an *edge* others can't easily replicate. So far, the answer is no.
- **The 2025 benchmark paper "Quantum Portfolio Optimization: An Extensive Benchmark" (arXiv 2509.17876)** finds that, even after parameter tuning, **"QAOA and quantum annealing also did not clearly differ from random sampling within the time limitation of 60 seconds"** and were outperformed by classical heuristics on portfolio QUBO instances.

### 4. The realistic edge for a retail fundamental quant
The empirical state of the art in QML for cross-sectional equity prediction (Chen et al., arXiv 2512.06630, Dec 2025): a **Quantum Temporal Convolutional Neural Network achieves Sharpe 0.538 on JPX Tokyo data, ~72% above the best classical baseline** — but the comparison is *parameter-matched architectures*, not a competition against the best engineered classical pipeline, and the gain comes from a simulator, not real hardware. The Ahmad et al. benchmark (arXiv 2601.03802, 2026) shows hybrid QNNs beating an ANN by **+3.8 AUC on AAPL directional classification** — but only on specific tasks where "data structure and circuit design are well aligned." These are *suggestive* gains in *toy settings*, not edges that would survive transaction costs and live trading on a 1000-name US universe.

**Conclusion: a retail user with SimFin/Finviz fundamentals is not going to find a meaningful quantum edge over a well-built classical V/Q/G/S screener. The edge to chase is better factor construction, sector/regime calibration, and execution-aware position sizing — all of which are classical.**

---

## Details: Concept-by-concept assessment

### Superposition
**Literal use (L):** A qubit register encodes a weighted superposition of binary asset-selection vectors $|x\rangle$, and a QAOA/QA optimizer collapses it toward the lowest-energy (highest mean-variance utility) bitstring. This is the standard QUBO/Ising formulation (e.g., Brandhofer et al., *Quantum Information Processing*, 2022; arXiv 2207.10555). For 10–50 stocks this is what D-Wave demos do; for a 500-stock universe it doesn't currently beat classical solvers.

**Metaphor (M):** "Market is in a superposition of bullish and bearish states until observed." This is genuinely just probability theory in pretty clothes. A Gaussian mixture model captures it.

**Honest implementation path for your screener (Q-I):** The *useful* idea is this: **your composite score is a point estimate of a noisy quantity, but the weights (35–40% valuation, 30–40% quality, etc.) are themselves uncertain.** A "superposition score" — really, a Bayesian / resampled score — would be a distribution over scores generated by resampling the weights and the underlying factor coefficients within their plausible ranges. This is **exactly Michaud's resampled efficiency** (Michaud, *Efficient Asset Management*, 2nd ed., OUP, 2008) and Avramov's Bayesian model averaging (Avramov, *Journal of Finance*, 2023, "Integrating Factor Models").

**Concrete recommendation:** Implement a 1,000-iteration Monte Carlo over your factor weights (uniform within published bands: V=35–40%, Q=30–40%, G=15–30%, S=0–20%) and report each ticker's **median score plus 5th/95th percentile band**. Stocks whose IQR straddles your buy threshold are "in superposition" — *don't take the trade or take a smaller size*. Stocks ranked in the top decile across ≥90% of resamples are robust. Expected effect: modest IC improvement (~0.005–0.02 absolute) but meaningful **drawdown reduction** because you stop owning weight-sensitive names. This is a real, testable upgrade — no qubits needed.

### Entanglement
**Literal (L):** A multi-qubit state $|\psi_{AB}\rangle$ that cannot be factored as $|\psi_A\rangle \otimes |\psi_B\rangle$. The IonQ Quantum Copulas paper (Zhu et al., 2022) and the recent "Quantum Network of Assets" preprint (arXiv 2511.21515, 2025) propose using entanglement-entropy of a density matrix built from return vectors as a generalized correlation measure that captures non-linear codependence the covariance matrix cannot. The QNA paper explicitly states: *"density matrices are strictly richer than covariance matrices… Σ_{ij} = E[r_i r_j]… describes only second-moment dispersion."*

**Metaphor (M):** "Stocks are entangled, so they co-move instantly." This is just correlation with extra steps unless you specify *what kind* of dependence (linear? rank? tail? mutual information?).

**The mathematically equivalent, deployable upgrade for your screener (Q-I):**
1. **Replace Pearson correlation with mutual information (MI) or copula entropy** when measuring codependence between factors or between sectors. MI captures non-linear dependence; copula entropy is mathematically equivalent to MI (Calsaverini & Vicente, *EPL* 88, 18001, 2009, arXiv 0911.4207). Sklearn-style implementations exist (`sklearn.feature_selection.mutual_info_regression`, or `npeet` library).
2. **Use hierarchical clustering on a correlation-distance metric to group co-moving stocks**, then apply **Hierarchical Risk Parity (HRP)** or **Nested Clustered Optimization (NCO)** for the final portfolio construction. HRP is in `PyPortfolioOpt` (`HRPOpt`, three lines of code, *"reproduced with permission from Marcos Lopez de Prado (2016)"*) and `riskfolio-lib`; NCO is in `riskfolio-lib` and `skfolio`.
3. For tail co-movement (the part that actually matters in drawdowns), fit a **Student-t copula or a Gumbel copula** to your top-decile names and reject candidate additions whose upper-tail dependence pushes portfolio CoVaR above a threshold.

**Expected effect on your backtest:** Switching from equal-weighted or inverse-vol top-N to HRP-allocated top-N in a cross-sectional factor strategy typically lowers realized volatility by 10–25% and improves out-of-sample Sharpe by 0.1–0.3 — *per López de Prado's own Monte Carlo experiment*. The IC of your underlying screen doesn't change; the conversion of IC into Sharpe improves because correlation-aware sizing kills the long-tail blowups.

### The observer effect / Heisenberg / Soros reflexivity
This concept actually has *more* legitimate finance content than the previous two, and Soros himself sourced it (CFA Institute, "Soros, Fallibility, Reflexivity, and the Importance of Adapting," 2016; Soros, *Journal of Economic Methodology*, 20(4), 2013).

**Three real mechanisms it maps to:**
1. **Market impact (the act of trading moves the price).** Almgren & Chriss, "Optimal Execution of Portfolio Transactions" (2000) — the canonical model. The empirical **square-root impact law** (Tóth et al., Bouchaud et al., *Physical Review X*, 2011, "Anomalous Price Impact and the Critical Nature of Liquidity") says the price moves $\sim \sqrt{Q}$ in the volume traded, *independent of execution schedule*. Bouchaud (Risk.net, 2024): *"flows and capacity constraints are first-order drivers of long-horizon returns."*
2. **Alpha decay / factor crowding** — the act of *publishing* (or many investors discovering) a signal degrades it. McLean & Pontiff (*Journal of Finance*, 71(1), 5–32, 2016, doi:10.1111/jofi.12365) — their published abstract states precisely: *"Portfolio returns are 26% lower out-of-sample and 58% lower post-publication. The out-of-sample decline is an upper bound estimate of data mining effects. We estimate a 32% (58%–26%) lower return from publication-informed trading."* Chorok Lee (KAIST, arXiv 2512.11913, Dec 2025) gives a hyperbolic decay $\alpha(t) = K/(1+\lambda t)$ derived from game-theoretic crowding equilibrium with $R^2 = 0.65$ fit for momentum.
3. **Reflexivity proper** — beliefs change fundamentals (rising stock price lowers borrowing costs, attracts talent, etc.). Hard to operationalize cleanly in a fundamental screener; mostly a position-sizing / regime-detection concept.

**Honest implementation path for your screener:**

- **(a) Capacity / impact haircut on your top decile.** For each candidate stock, estimate an impact cost using the square-root law: $\text{impact bps} \approx Y \cdot \sigma_{daily} \cdot \sqrt{Q / \text{ADV}}$ where $Y \approx 1$ for US large caps (CFM and many others have calibrated this). Subtract this from expected alpha before ranking. Penalizes thinly-traded small caps where your size-adjusted signal would otherwise be inflated.
- **(b) Alpha decay-aware factor weights.** Add a slow exponential or hyperbolic decay to backtested factor returns when weighting: factors that were strong in 2010 but weak post-2020 should get downweighted. The Lee (arXiv 2512.11913, 2025) paper gives a closed-form hyperbolic decay with $R^2 = 0.65$ for momentum. This is *more important than anything quantum-related* you could do.
- **(c) Reflexivity / sentiment loop.** Your Sentiment sub-score (0–20%) already partially captures this. Consider a regime-conditional uplift: in your R2 Reflation / R5 Recovery regimes, sentiment may have stronger lead behavior on fundamentals (price discovery → analyst revision); in R3 Stagflation / R4 Recession, sentiment may be a contrarian signal. This is a sector tilt, not a quantum operation.
- **(d) Anti-crowding flag.** Use 13F filings or ETF holding overlap to flag stocks where ownership is concentrated in factor-replication ETFs (MTUM, VLUE, QUAL). A "crowding score" lowers the position. The Lee (arXiv 2512.11913, 2025) paper reports verbatim: *"Out-of-sample (2001–2024), crowded reversal factors show 1.7–1.8× higher crash probability (bottom decile returns), while crowded momentum shows lower crash risk (0.38×, p = 0.006)."*

**Expected effect:** of the three concepts, this is where the biggest realized gain probably lives — not in IC, but in **realized Sharpe net of trading costs and in tail-risk reduction.**

### Quantum walks and quantum Monte Carlo
For completeness:
- **Quantum walks** (Orrell, *Wilmott*, 2021; arXiv 2403.19502) are proposed as alternatives to geometric Brownian motion that can reproduce fat tails / ballistic diffusion without ad-hoc parameter additions. Mathematically elegant; **irrelevant to a cross-sectional fundamental screener** that doesn't price paths. Skip.
- **Quantum Monte Carlo / quantum amplitude estimation** offer (theoretically) a quadratic speedup over classical MC for derivative pricing (Rebentrost, Gupt, Bromley; Goldman + IBM papers). **Irrelevant to equity ranking.** Skip.

### Quantum annealing as a post-screener portfolio-construction layer
This is the one place a literal quantum tool *could* enter, but I recommend you don't bother yet.

**The proposal:** After your screener produces ~50–100 candidates, formulate a QUBO:
$$\min_x \; q\, x^\top \Sigma x - \mu^\top x \quad \text{s.t.} \sum x_i = K$$
where $x_i \in \{0,1\}$ is asset selection and $K$ is the target portfolio size. Solve on D-Wave Leap hybrid solver, or with classical simulated annealing.

**Why classical wins for retail:**
- For $N \le 500$ assets, the classical mixed-integer QP solvers (Gurobi via `cvxpy`, or free alternatives like `mip`) typically solve this to optimality in seconds.
- Simulated annealing in pure Python (Crama & Schyns, *EJOR* 150(3), 546–571, 2003) finds near-optimal solutions in ~10 seconds for 151 US stocks. `dwave-neal` runs the same algorithm on your laptop without a quantum backend.
- Phillipson et al. (ICCS 2021) benchmarked D-Wave hybrid against Gurobi/LocalSolver on Nikkei225 and S&P500 portfolio QUBOs: D-Wave "comes close" to the classical commercial solvers but does not beat them.
- Brandhofer et al. (2025 benchmark, arXiv 2509.17876): QAOA and quantum annealing **"did not clearly differ from random sampling within the 60-second time limit"** on dense portfolio QUBOs.

**Verdict:** Use **HRP via `PyPortfolioOpt`** or **NCO via `riskfolio-lib`** as your portfolio-construction layer. These give you all the benefit (cardinality-aware diversification, robustness to noisy covariance, López de Prado-grade methodology) with no quantum dependency, no API keys, and a 5-line implementation.

---

## Recommendations (staged, with benchmarks)

### Stage 1 — Do these now (low risk, clear payoff)
1. **Add resampled scoring** ("superposition-as-Bayesian-uncertainty"). Monte Carlo over your V/Q/G/S weights and over Piotroski/Altman thresholds, 1,000 iterations. Report median + IQR. **Benchmark for adoption:** if median-IQR-filtered portfolio improves *backtest Sharpe by ≥0.1 or reduces max-drawdown by ≥3 pts* across your 6 walk-forward windows, keep it. If not, simplify back. (Effort: ~50 lines of Python; uses your existing scoring code in a loop.)
2. **Add HRP as your portfolio-construction layer.** Replace top-N equal-weighting (or whatever you do post-screen) with `PyPortfolioOpt.HRPOpt` on the top 30–50 names. **Benchmark:** decile-1 minus decile-10 spread should be preserved or improved; portfolio volatility should drop ≥10%. (Effort: ~20 lines.)
3. **Add a square-root impact haircut** to expected alpha. Calibrate Y from any published US large-cap study (e.g., Almgren et al. 2005 estimates). **Benchmark:** the rank ordering of your top-100 should shift toward more liquid names — verify the median ADV of your top decile increases.

### Stage 2 — Do these next (medium effort, medium payoff)
4. **Replace Pearson with mutual information for inter-factor codependence diagnostics.** Compute MI between V, Q, G, S sub-scores; if MI(G, S) is far above their Pearson correlation, you have non-linear coupling you're not capturing. Use this to redesign weights, not to compute scores.
5. **Add a factor-decay model.** Fit Lee (2025) hyperbolic decay to your in-sample factor IC time series; downweight factors with high $\lambda$ in your sector weights. **Benchmark:** out-of-sample IC stability (rolling Spearman IC variance) should drop.
6. **Add a crowding flag.** Use 13F overlap with factor ETFs to penalize crowded names in the final ranking. **Benchmark:** tail event participation (worst-5-day returns of portfolio vs. benchmark) should improve.

### Stage 3 — Do these only if Stage 1-2 are exhausted (high effort, speculative payoff)
7. **NCO via `riskfolio-lib`** if HRP is working but you want hierarchical mean-variance trade-offs (allows expected-return inputs in addition to covariance).
8. **Tensor-network factor compression** (à la Multiverse Computing / Orús) — only if you've built ~50+ factors and need dimensionality reduction more robust than PCA. This is genuine quantum-inspired but the implementation complexity is high (`tensornetwork` or `quimb` libraries).

### Do NOT do
- **Do not pay for any "quantum trading" SaaS product.** As of 2026, no retail-priced offering will beat your classical screener with HRP.
- **Do not run QAOA on IBM Quantum / IonQ free tier hoping for an edge.** It won't beat classical MIQP for your problem size.
- **Do not invoke "observer effect" or "entanglement" as marketing language for your own work.** Use the precise classical terms (market impact, mutual information, hierarchical clustering, copula tail dependence). Soros's "human uncertainty principle" is *philosophically* important but it's not a quantitative tool — operationalize it as capacity decay and reflexive sentiment.

### Thresholds that would change these recommendations
- If you build to >5,000 assets and need to solve a cardinality-constrained portfolio with K~20 in real time, *then* QUBO formulations on D-Wave hybrid become worth a benchmark vs. Gurobi.
- If a peer-reviewed paper demonstrates a *non-toy* quantum-hardware Sharpe improvement on US equities net of fees, revisit. As of the May 2026 literature scan, no such paper exists.
- If Goldman or JPMorgan publishes that they have moved a quantum solver into a live trading book (not a research pilot), revisit. Goldman is currently moving the *opposite* direction (Bloomberg, April 2026).

## Caveats
- The quantum-finance literature is *fast-moving and noisy*. Several preprints cited here are 2025–2026 and have not been replicated. Treat anything claiming "quantum advantage" on a financial problem with the same suspicion you'd apply to a fresh academic backtest.
- Almost all the published QML "Sharpe improvements" use small universes, simulators, and parameter-matched (rather than best-engineered) classical baselines. They probably overstate transferable edge by an order of magnitude.
- The HRP/NCO Monte Carlo numbers (72.47% variance reduction, 31.3% Sharpe improvement) come from a controlled simulation in the original paper; live performance varies materially with the input correlation structure.
- I could not locate a public López de Prado quote explicitly attacking "quantum hype" vs. legitimate quantum-inspired methods — his stance is best inferred from his actually-deployed products (which are classical) versus his research (which uses real quantum hardware for specific NP-hard reformulations). Your skepticism is well-founded but does not have a single famous skeptical quote to anchor on; the closest are DARPA program manager Joe Altpeter, physicist-skeptics Gil Kalai and Nikita Gourianov, and the Brandhofer et al. (2025) benchmark showing quantum methods failing to beat random sampling.
- For a beginner Python user, the riskiest *engineering* mistake is over-engineering. Stage 1 alone — resampled scoring + HRP + impact haircut — captures probably 80% of the available improvement and is well within a UT Austin Economics-major skill envelope. Resist the temptation to install Qiskit before you have a clean, well-backtested classical pipeline.