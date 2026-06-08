"""
recent_form.py v2 - SIMPLE version using local DB data only, no HTTP
Calculates recent form (last 5-10 matches) as multiplier 0.85-1.15
"""

def calculate_recent_form(team_name, match_list, last_n=5):
    """
    Calculate recent form from a list of match dicts.
    
    Args:
        team_name: Team name to find
        match_list: List of match dicts with 'home_team', 'away_team', 'home_score', 'away_score'
        last_n: Number of recent matches to analyze
    
    Returns:
        dict with:
        - form_multiplier: 0.85-1.15 (points/max_possible)
        - wins, draws, losses, points
        - form_strength: "Excellent", "Good", "Average", "Poor", "Critical"
    """
    
    # Find matches for this team
    team_matches = []
    for match in match_list:
        if match.get('home_team') == team_name:
            team_matches.append({
                'goals_for': match.get('home_score', 0),
                'goals_against': match.get('away_score', 0)
            })
        elif match.get('away_team') == team_name:
            team_matches.append({
                'goals_for': match.get('away_score', 0),
                'goals_against': match.get('home_score', 0)
            })
    
    # Get last N matches (most recent first, so reverse)
    recent = team_matches[-last_n:] if team_matches else []
    
    if not recent:
        return {
            'form_multiplier': 1.0,
            'wins': 0, 'draws': 0, 'losses': 0,
            'points': 0, 'max_possible': 0,
            'form_strength': 'No data',
            'matches_analyzed': 0
        }
    
    # Calculate points (3W, 1D, 0L)
    wins = draws = losses = 0
    for m in recent:
        if m['goals_for'] > m['goals_against']:
            wins += 1
        elif m['goals_for'] == m['goals_against']:
            draws += 1
        else:
            losses += 1
    
    points = wins * 3 + draws
    max_possible = len(recent) * 3
    ppg = points / max_possible if max_possible else 0
    
    # Map to 0.85-1.15 range
    form_mult = 0.85 + (ppg * 0.30)  # Scale 0-1 to 0.85-1.15
    form_mult = max(0.85, min(1.15, form_mult))
    
    # Classify strength
    if ppg >= 2.4:
        strength = "Excellent"
    elif ppg >= 1.8:
        strength = "Good"
    elif ppg >= 1.2:
        strength = "Average"
    elif ppg >= 0.6:
        strength = "Poor"
    else:
        strength = "Critical"
    
    return {
        'form_multiplier': round(form_mult, 3),
        'wins': wins, 'draws': draws, 'losses': losses,
        'points': points, 'max_possible': max_possible,
        'form_strength': strength,
        'matches_analyzed': len(recent),
        'ppg': round(ppg, 2)
    }
