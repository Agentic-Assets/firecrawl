MarketSense: Do Small AI Models Understand Financial News?
----------------------------------------------------------

An Empirical Study of Compact Language Models and Stock Return Reactions

Sai Mahesh Sandeboina Independent Researcher, United States [saimaheshsandeboina931@gmail.com](mailto:saimaheshsandeboina931@gmail.com)

April 2026

Abstract
--------

This paper investigates whether compact, domain-adapted language models can detect economically meaningful sentiment in financial headlines. Using roughly one thousand news items for nine large-cap tickers between April and October 2025,we compare FinBERT—a finance-tuned model—with DistilBERT—a compressed general model. FinBERT's daily sentiment correlates positively with same-day stock returns, whereas DistilBERT shows no consistent signal. The results suggest that domain adaptationmattersmorethanmodel sizefor resource-efficientfinancialNLP.

1Introduction
-------------

Financial markets react to information quickly, and news sentiment is often used as a proxy for short-horizon beliefs. Recent progress in language modeling shows that large models can extract rich semantics, but in production settings (research labs, funds, and startups) smaller models are preferred for cost, latency, and governance reasons. This raises a practical question: Do small, domain-adapted models capture economically meaningfulsentiment?

We study this in the context of same-day stock returns using compact, widely-available models: FinBERT \[3, 2\]—a BERT-base model adapted to finance—and DistilBERT \[6, 4\]—a compressed general-purpose model fine-tuned on sentiment. Using headlines for nine large-cap tickers (AAPL, AMZN, GS, JPM, MS, MSFT, NVDA, TSLA,WMT) from ~April-October 2025, we compute daily sentiment per ticker and relate it to open→closereturns.

Our main result is simple and actionable: FinBERT's daily sentiment exhibits a small but statistically significant positive correlation with same-day returns, while DistilBERT shows no consistent signal. The implication is that domain adaptation can matter more than raw model size for resource-efficient financial NLP.

2 Methodology
-------------

Data collection.We query Google News RSS for "{TIcKER} stock" and parse results with feedparser. We retain headline, source, URL, and UTC publish time. We deduplicate by (ticker, headline). For prices we use daily OHLC from Stooq (free end-of-day feed) to avoid API throttling issues; we form same-day open→close returns rd = (Close - Open)/Open. See code and CSVs (headlines.csv, prices.csv) for reproducibility \[5, 1\].

Sentiment scoring. We score each headline with two models implemented via transformers \[7\]:

*   ·FinBERT \[3, 2\] (finance-tuned). We convert class probabilities to a scalar sentiment s = Pr(pos)Pr(neg).
*   DistilBERT \[6, 4\] (general sentiment). We likewise map SST-2 scores to s.

FIRECRAWLPAGEBREAK

For each (date, ticker) we average headline scores into a daily sentiment d,t. We also compute an optional 3-day rolling mean sd,t = >?=o sd-k,t for visualization stability.

Alignment and sample.We inner-join daily sentiment with price returns on (date,ticker).In the present run, this yields ~200-250 aligned observations (dependent on news frequency） across the nine tickers, with ~l40 trading days of prices (Stooq). All derived files are saved (headlines\_scored.csv, daily-sentiment.csv, joined\_sentiment\_returns.csv) to support replication.

Estimation and tests. We report Pearson correlations r(d,t,rd,t） overall and by sector/ticker, with p-values and bootstrap CIs where noted. Plots include sentiment-return scatter with fitted line, rolling sentiment time series,and per-ticker summaries.We treat these as associations,not trading rules.

3 Results
---------

3.1 Headline Sentiment vs Same-Day Returns
------------------------------------------

and 2 show the pooled scatterplots (all tickers, all days). FinBERT exhibits a small but clear positive slope; DistilBERT shows no consistent relation.This visual pattern matches our statistics in Section 3.2.

Figure 1: FinBERT daily sentiment vs same-day open—>close return (all tickers).

FIRECRAWLPAGEBREAK

OverallDistilBERTSentimentvsSame-DayReturn r=0.031,p=0.649,n=223
----------------------------------------------------------------

Figure 2: DistilBERT daily sentiment vs same-day open—→close return (all tickers).

3.2Per-Ticker Correlations
--------------------------

To summarize by name, we compute Pearson r between sentiment and returns per ticker. Figure 3 plots these values. FinBERT is positive for most symbols (notably AMZN, JPM, NVDA), while DistilBERT is mixed and often near zero. This supports the hypothesis that domain adaptation matters more than parameter count for this task.

FIRECRAWLPAGEBREAK

Figure 3: Per-ticker Pearson correlation (r) between daily sentiment and same-day return, for FinBERT and DistilBERT.

3.3 Rolling Sentiment (Stability Check)
---------------------------------------

We also examine 3-day rolling averages of sentiment by ticker to visualize stability and episodes where the two models disagree.Below we show FinBERT rolling sentiment for all nine names alongside DistilBERT for direct comparison.

Figure 4:FinBERT:3-dayrolling sentimentby ticker.

FIRECRAWLPAGEBREAK

3.41 Representative Per-Ticker Scatterplots
-------------------------------------------

For transparency, Figures 5 and 6 show example per-ticker scatterplots for both models.

Figure 5: AAPL: FinBERT (left)vs DistilBERT (right) sentiment vs return.

Figure 6:MSFT:FinBERT(left）vs DistilBERT(right）sentiment vs return.

Takeaway.Across pooled and per-ticker views,FinBERT's sentiment correlates positively with same-day returns, while DistilBERT's signal is weak or inconsistent. This pattern is robust to rolling aggregation and visible in multiple symbols.

4 Conclusion
------------

We ask whether small models"understand"financial news in thelimited,testable sense of aligning sentiment with same-day stock moves. Our evidence suggests that a compact, finance-tuned model (FinBERT) does extract a weak but economically meaningful signal, whereas a similarly compact general model (DistilBERT) does not. For many practical settings, domain adaptation beats raw size.

Limitations. We analyze headlines, not full articles; we consider same-day open→close returns only; and we do not control for confounders (macro releases, earnings timing). Correlations are small and should not be read as trading advice. Re-running the pipeline on different date windows will change sample sizes and noise characteristics.

FIRECRAWLPAGEBREAK

Future work. Extend to multi-day horizons, intraday bars, richer event controls, and more compact domain models (e.g., finance-tuned DistilBERT or MiniLM). Explore cross-sectional predictability and interaction with volatility regimes.

Ethics and reproducibility. We cite all software and models used \[7, 3, 2, 6, 4, 5, 1\] and provide intermediate CSVs and figures to enable replication.

References
----------

*   \[1\] Stooq: Free historical market data. [https://stooq.com](https://stooq.com/)
    , 2025.

2.  \[2\]Prosus AI. Prosusai/finbert (hugging face model card). [https://huggingface.co/ProsusAI/finbert](https://huggingface.co/ProsusAI/finbert)
    , 2020.
3.  \[3\]Dogu Araci. Finbert: Financial sentiment analysis with pre-trained language models. In Proceedings of the ACL Student Research Workshop,2019.

*   \[4\] Hugging Face. distilbert-base-uncased-finetuned-sst-2-english (hugging face model card). [https://huggingface.co/distilbert-base-uncased-finetuned-sst-2-english,2020](https://huggingface.co/distilbert-base-uncased-finetuned-sst-2-english,2020)
    .
*   \[5\] Mark Kellan and community. feedparser: Universalfeed parserforpython. [https://github.com/kurtmckee/feedparser](https://github.com/kurtmckee/feedparser)
    , 2024.

6.  \[6\]Victor Sanh, Lysandre Debut, Julien Chaumond, and Thomas Wolf. Distilbert,a distilled version of bert: smaller, faster,cheaper and lighter. arXiv preprint arXiv:1910.01108, 2019.

*   \[7\] Thomas Wolf, Lysandre Debut, Victor Sanh, et al. Transformers: State-of-the-art natural language processing. In Proceedings ofEMNLP 2020:System Demonstrations,2020.
