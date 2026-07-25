"""Relevance, category, sentiment, and impact scoring for news headlines.

Deliberately a transparent keyword/lexicon model rather than a language model:
every score can be traced to the exact terms that produced it, which is what
"auditable" requires. It is also the honest engineering choice — a headline
classifier is not where this project's edge is claimed to be.

Sentiment is recorded as a *feature*, never as a trade instruction. The engine
uses it as a risk and sizing modifier; "positive headline ⇒ buy" is explicitly
not implemented.
"""

from __future__ import annotations

import re

from ..utils.numeric import clamp
from .base import NewsCategory, NewsImpact

# --- relevance ------------------------------------------------------------

_BTC_DIRECT = frozenset(
    {"bitcoin", "btc", "satoshi", "halving", "xbt"}
)
_CRYPTO_BROAD = frozenset(
    {
        "crypto", "cryptocurrency", "ethereum", "eth", "blockchain", "stablecoin",
        "usdt", "usdc", "tether", "coinbase", "binance", "kraken", "exchange",
        "defi", "altcoin", "digital asset", "digital assets", "token",
    }
)
_MACRO_TERMS = frozenset(
    {
        "federal reserve", "fed", "fomc", "interest rate", "interest rates", "cpi",
        "inflation", "nonfarm", "payrolls", "unemployment", "jobs report", "gdp",
        "treasury", "yield", "recession", "rate cut", "rate hike", "powell", "ecb",
        "monetary policy", "quantitative", "dollar index", "risk-off", "risk off",
    }
)

# --- categories -----------------------------------------------------------

_CATEGORY_TERMS: dict[NewsCategory, frozenset[str]] = {
    NewsCategory.ETF_INSTITUTIONAL: frozenset(
        {"etf", "institutional", "blackrock", "fidelity", "grayscale", "inflow",
         "outflow", "custody", "spot etf", "microstrategy", "treasury company"}
    ),
    NewsCategory.CRYPTO_REGULATION: frozenset(
        {"sec", "regulation", "regulator", "lawsuit", "cftc", "mica", "ban",
         "legislation", "compliance", "licence", "license", "enforcement", "court"}
    ),
    NewsCategory.SECURITY_HACK: frozenset(
        {"hack", "hacked", "exploit", "breach", "stolen", "attack", "vulnerability",
         "drained", "phishing", "rug pull"}
    ),
    NewsCategory.EXCHANGE_INCIDENT: frozenset(
        {"exchange halt", "withdrawals suspended", "outage", "insolvency", "bankrupt",
         "delisting", "halted trading", "downtime"}
    ),
    NewsCategory.STABLECOIN: frozenset(
        {"stablecoin", "tether", "usdt", "usdc", "depeg", "de-peg", "peg", "reserves"}
    ),
    NewsCategory.FED_POLICY: frozenset(
        {"federal reserve", "fomc", "powell", "rate cut", "rate hike", "monetary policy",
         "quantitative tightening", "quantitative easing", "dot plot"}
    ),
    NewsCategory.INFLATION_CPI: frozenset(
        {"cpi", "inflation", "consumer price", "ppi", "producer price", "pce", "deflation"}
    ),
    NewsCategory.INTEREST_RATES: frozenset(
        {"interest rate", "interest rates", "yield", "treasury", "bond", "basis points"}
    ),
    NewsCategory.EMPLOYMENT: frozenset(
        {"nonfarm", "payrolls", "unemployment", "jobless", "jobs report", "employment"}
    ),
    NewsCategory.GEOPOLITICAL: frozenset(
        {"war", "sanction", "sanctions", "conflict", "invasion", "military", "tariff",
         "election", "geopolitical", "escalation"}
    ),
    NewsCategory.CRYPTO_COMPANY: frozenset(
        {"coinbase", "binance", "kraken", "bitfinex", "circle", "ripple", "miner",
         "mining company", "funding round", "acquisition"}
    ),
    NewsCategory.MACRO_RELEASE: frozenset(
        {"gdp", "retail sales", "pmi", "ism", "consumer confidence", "housing starts"}
    ),
}

# --- sentiment ------------------------------------------------------------

_POSITIVE = frozenset(
    {
        "surge", "surges", "rally", "rallies", "soar", "soars", "gain", "gains", "jump",
        "jumps", "record high", "all-time high", "approval", "approved", "adoption",
        "inflow", "inflows", "bullish", "upgrade", "breakthrough", "partnership",
        "milestone", "boost", "boosts", "optimism", "recovery", "rebound", "surged",
        "climbs", "accumulate", "buy", "buying", "green light", "greenlight",
    }
)
_NEGATIVE = frozenset(
    {
        "plunge", "plunges", "crash", "crashes", "drop", "drops", "fall", "falls",
        "slump", "tumble", "tumbles", "hack", "hacked", "exploit", "stolen", "ban",
        "banned", "lawsuit", "sue", "sues", "sued", "fraud", "investigation", "probe",
        "bearish", "downgrade", "outflow", "outflows", "liquidation", "liquidations",
        "collapse", "bankrupt", "insolvency", "warning", "warns", "risk", "selloff",
        "sell-off", "fear", "reject", "rejected", "delay", "delayed", "halt", "halted",
        "sinks", "slides", "decline", "declines",
    }
)
_NEGATION = frozenset({"not", "no", "never", "without", "denies", "denied", "rejects"})

_HIGH_IMPACT_TERMS = frozenset(
    {
        "fomc", "federal reserve", "rate decision", "cpi", "inflation report", "nonfarm",
        "etf approval", "etf approved", "sec approves", "hack", "exploit", "collapse",
        "bankrupt", "ban", "emergency", "halted", "depeg", "de-peg", "all-time high",
        "crash", "flash crash", "liquidation", "war", "invasion",
    }
)

_WORD_RE = re.compile(r"[a-z0-9'-]+")


def score_relevance(headline: str) -> float:
    """0-1 relevance to BTC trading.

    Direct Bitcoin mentions score highest, broad crypto next, macro terms lower
    but non-zero because rate and inflation news demonstrably moves crypto.
    """
    text = headline.lower()
    words = set(_WORD_RE.findall(text))

    score = 0.0
    if words & _BTC_DIRECT:
        score = 1.0
    elif words & _CRYPTO_BROAD or any(term in text for term in _CRYPTO_BROAD if " " in term):
        score = 0.65
    if any(term in text for term in _MACRO_TERMS):
        score = max(score, 0.55)
    return clamp(score, 0.0, 1.0)


def classify_category(headline: str) -> NewsCategory:
    """Best-matching category by term overlap."""
    text = headline.lower()
    best_category = NewsCategory.OTHER
    best_hits = 0
    for category, terms in _CATEGORY_TERMS.items():
        hits = sum(1 for term in terms if term in text)
        if hits > best_hits:
            best_category, best_hits = category, hits
    if best_hits == 0:
        words = set(_WORD_RE.findall(text))
        if words & _BTC_DIRECT:
            return NewsCategory.BITCOIN
    return best_category


def score_sentiment(headline: str) -> float:
    """-1 … +1 lexicon sentiment with simple negation handling."""
    tokens = _WORD_RE.findall(headline.lower())
    if not tokens:
        return 0.0

    score = 0
    for index, token in enumerate(tokens):
        polarity = 1 if token in _POSITIVE else (-1 if token in _NEGATIVE else 0)
        if polarity == 0:
            continue
        # "not approved" must not read as positive.
        window = tokens[max(0, index - 3) : index]
        if any(w in _NEGATION for w in window):
            polarity *= -1
        score += polarity

    if score == 0:
        return 0.0
    # Squash so a headline stuffed with adjectives cannot dominate.
    return clamp(score / 3.0, -1.0, 1.0)


def score_impact(headline: str, relevance: float, category: NewsCategory) -> NewsImpact:
    """Potential market impact."""
    text = headline.lower()
    if any(term in text for term in _HIGH_IMPACT_TERMS) and relevance >= 0.5:
        return NewsImpact.HIGH
    high_impact_categories = {
        NewsCategory.FED_POLICY,
        NewsCategory.INFLATION_CPI,
        NewsCategory.SECURITY_HACK,
        NewsCategory.EXCHANGE_INCIDENT,
        NewsCategory.ETF_INSTITUTIONAL,
    }
    if category in high_impact_categories and relevance >= 0.6:
        return NewsImpact.MEDIUM
    if relevance >= 0.8:
        return NewsImpact.MEDIUM
    return NewsImpact.LOW


def score_confidence(headline: str, relevance: float, source: str) -> float:
    """How much weight this classification deserves.

    Short or vague headlines carry less information; official sources carry more.
    """
    length_factor = clamp(len(headline) / 80.0, 0.3, 1.0)
    official = any(
        marker in source.lower()
        for marker in ("federalreserve", "sec.gov", "treasury", "bls.gov", "ecb")
    )
    base = 0.55 + 0.35 * relevance
    if official:
        base += 0.1
    return clamp(base * length_factor + 0.15, 0.0, 1.0)
