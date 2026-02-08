"""
Unit tests for Wilson Score calculation.
"""
import pytest
from src.api import calculate_wilson_score


class TestWilsonScore:
    """Tests for Wilson score interval calculation."""
    
    def test_zero_total(self):
        """Return 0 for no ratings."""
        assert calculate_wilson_score(0, 0) == 0.0
    
    def test_all_positive(self):
        """High score for all positive with many samples."""
        # 100 positive out of 100
        score = calculate_wilson_score(100, 100)
        assert score > 0.95  # Should be very high
        assert score <= 1.0
    
    def test_all_negative(self):
        """Low score for all negative."""
        score = calculate_wilson_score(0, 100)
        assert score < 0.05  # Should be very low
        assert score >= 0.0
    
    def test_mixed_ratings(self):
        """Reasonable score for mixed ratings."""
        # 75% success rate
        score = calculate_wilson_score(75, 100)
        assert 0.6 < score < 0.8
    
    def test_low_sample_penalty(self):
        """Lower score for same ratio but fewer samples."""
        # 3/4 = 75% with few samples
        score_low = calculate_wilson_score(3, 4)
        # 75/100 = 75% with many samples
        score_high = calculate_wilson_score(75, 100)
        
        # Low sample size should have lower Wilson score despite same ratio
        assert score_low < score_high
    
    def test_new_agent_conservative(self):
        """New agent with 1 positive rating should have conservative score."""
        score = calculate_wilson_score(1, 1)
        # Should not be 100% with just 1 rating
        assert score < 0.8
        assert score > 0.1
    
    def test_symmetry(self):
        """Score for 90/100 should be higher than 10/100."""
        high = calculate_wilson_score(90, 100)
        low = calculate_wilson_score(10, 100)
        
        assert high > low
        assert high > 0.8
        assert low < 0.2
    
    def test_increasing_with_more_positive(self):
        """Score should increase as positive count increases."""
        scores = [
            calculate_wilson_score(i, 100)
            for i in range(0, 101, 10)
        ]
        
        # Should be monotonically increasing
        for i in range(len(scores) - 1):
            assert scores[i] <= scores[i + 1]
