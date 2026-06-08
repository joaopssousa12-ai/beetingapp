"""
goals_markets.py v2 - SIMPLE Poisson approximation, NO scipy
Predicts O/U 2.5 and BTTS from goals history (local DB data only)
"""

import math

def poisson_pmf(k, lam):
    """Simple Poisson PMF without scipy."""
    if lam == 0:
        return 1.0 if k == 0 else 0.0
    try:
        return (lam ** k * math.exp(-lam)) / math.factorial(k)
    except (OverflowError, ValueError):
        return 0.0

def poisson_cdf(k, lam):
    """Cumulative Poisson CDF."""
    return sum(poisson_pmf(i, lam) for i in range(int(k) + 1))

def predict_ou_and_btts(home_team, away_team, match_history=None, home_gf=1.5, home_ga=1.2, away_gf=1.3, away_ga=1.4):
    """
    Predict O/U 2.5 and BTTS.
    
    Uses match_history if provided (list of matches), else uses defaults.
    
    Returns:
        dict with probabilities and confidence
    """
    
    # If match history provided, calculate averages
    if match_history:
        home_matches = [m for m in match_history 
                       if m.get('home_team') == home_team or m.get('away_team') == home_team]
        away_matches = [m for m in match_history 
                       if m.get('home_team') == away_team or m.get('away_team') == away_team]
        
        if home_matches:
            home_gf = sum(m.get('home_score' if m.get('home_team') == home_team else 'away_score', 0) 
                         for m in home_matches) / len(home_matches)
            home_ga = sum(m.get('away_score' if m.get('home_team') == home_team else 'home_score', 0) 
                         for m in home_matches) / len(home_matches)
        
        if away_matches:
            away_gf = sum(m.get('away_score' if m.get('away_team') == away_team else 'home_score', 0) 
                         for m in away_matches) / len(away_matches)
            away_ga = sum(m.get('home_score' if m.get('away_team') == away_team else 'away_score', 0) 
                         for m in away_matches) / len(away_matches)
    
    # Expected goals
    home_expected = home_gf * 1.1  # Home advantage
    away_expected = away_gf * 0.9  # Away discount
    total_expected = home_expected + away_expected
    
    # Poisson probabilities
    ou_over_25_prob = 1 - poisson_cdf(2, total_expected)
    ou_under_25_prob = poisson_cdf(2, total_expected)
    
    # BTTS = P(home≥1) × P(away≥1)
    p_home_scores = 1 - poisson_pmf(0, home_expected)
    p_away_scores = 1 - poisson_pmf(0, away_expected)
    btts_prob = p_home_scores * p_away_scores
    
    # Volatility (simple: high if gf/ga vary a lot)
    volatility = "Medium"
    conf_adj = 0
    
    # Confidence levels
    ou_conf = _get_confidence_level(ou_over_25_prob * 100, conf_adj)
    btts_conf = _get_confidence_level(btts_prob * 100, conf_adj)
    
    return {
        'ou_over_25': round(ou_over_25_prob * 100, 1),
        'ou_under_25': round(ou_under_25_prob * 100, 1),
        'ou_confidence': ou_conf,
        'btts_yes': round(btts_prob * 100, 1),
        'btts_no': round((1 - btts_prob) * 100, 1),
        'btts_confidence': btts_conf,
        'expected_goals': round(total_expected, 2),
        'volatility': volatility,
        'note': 'Goals-based model (Poisson approximation)'
    }

def _get_confidence_level(probability, adjustment=0):
    """Map probability to confidence."""
    abs_dev = abs(probability - 50)
    adjusted = abs_dev + adjustment
    
    if adjusted >= 20:
        return "HIGH"
    elif adjusted >= 5:
        return "MEDIUM"
    else:
        return "LOW"
