"""Test del lessico sentiment (funzioni pure, nessuna rete/DB)."""

import pytest

from modules.news_sentiment import sentiment


def test_tokenize_lowercase_and_ignore_punctuation():
    assert sentiment.tokenize("Record PROFIT! Beat, Upgrades") == {
        "record",
        "profit",
        "beat",
        "upgrades",
    }
    assert sentiment.tokenize("") == set()


def test_score_positive_negative_neutral():
    assert sentiment.sentiment_score("Company beats estimates, profits surge") > 0
    assert sentiment.sentiment_score("Company misses estimates, stock slumps") < 0
    assert sentiment.sentiment_score("Quarterly results published on tuesday") == 0.0


def test_score_bounds():
    assert -1.0 <= sentiment.sentiment_score("best wins buy bullish record") <= 1.0
    assert -1.0 <= sentiment.sentiment_score("worst crash fails losses layoffs") <= 1.0


def test_score_one_word_each_side():
    # 1 positiva + 1 negativa → 0.0 (annullamento), non un crash
    assert sentiment.sentiment_score("strong decline") == 0.0


def test_label_threshold():
    assert sentiment.sentiment_label(0.5, 0.35) == "positive"
    assert sentiment.sentiment_label(-0.5, 0.35) == "negative"
    assert sentiment.sentiment_label(0.1, 0.35) == "neutral"
    assert sentiment.sentiment_label(-0.1, 0.35) == "neutral"
    assert sentiment.sentiment_label(0.35, 0.35) == "positive"
    assert sentiment.sentiment_label(None, 0.35) is None