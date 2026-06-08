"""
odds_collector.py - Automatic odds collection from The-Odds-API
Fetches odds for multiple bookmakers, salva à DB
"""

import os
import requests
import json
from datetime import datetime

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "68cb102bb514222745b6c22cd6fc9a6b")
ODDS_API_BASE = "https://api.the-odds-api.com/v4"

def collect_odds_multiple_bookmakers():
    """
    Fetch odds from The-Odds-API for multiple sports and bookmakers.
    Returns list of odds records ready to save to DB.
    """
    
    odds_records = []
    
    # Sports to fetch (with their event types)
    sports = [
        ('soccer_epl', 'Premier League'),
        ('soccer_la_liga', 'La Liga'),
        ('soccer_bundesliga', 'Bundesliga'),
        ('soccer_france_ligue_1', 'Ligue 1'),
        ('soccer_portugal', 'Liga Portugal'),
        ('soccer_italy_serie_a', 'Serie A'),
        ('soccer_fifa_world_cup', 'World Cup 2026'),
    ]
    
    # Bookmakers to include (popular + free tier)
    bookmakers = ['pinnacle', 'draftkings', 'betmgm', 'caesars', 'fanduel']
    
    for sport_key, sport_name in sports:
        try:
            # Get upcoming games
            url = f"{ODDS_API_BASE}/sports/{sport_key}/odds"
            params = {
                'apiKey': ODDS_API_KEY,
                'regions': 'us,eu',
                'markets': 'h2h,spreads,totals',
                'oddsFormat': 'decimal',
                'bookmakers': ','.join(bookmakers[:3])  # Limit to 3 per request
            }
            
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            
            games = resp.json()
            if not games:
                continue
            
            for game in games:
                event_id = game.get('id', '')
                home = game.get('home_team', '')
                away = game.get('away_team', '')
                
                # Extract odds from bookmakers
                for bookie in game.get('bookmakers', []):
                    bookmaker = bookie.get('key', '')
                    for market in bookie.get('markets', []):
                        market_key = market.get('key', '')
                        
                        # Handle 1X2 (Match Result)
                        if market_key == 'h2h':
                            outcomes = {o.get('name'): o.get('price') 
                                       for o in market.get('outcomes', [])}
                            if home in outcomes and away in outcomes:
                                odds_records.append({
                                    'event_id': event_id,
                                    'home_team': home,
                                    'away_team': away,
                                    'sport': sport_name,
                                    'bookmaker': bookmaker,
                                    'market': '1X2',
                                    'home_odd': outcomes.get(home),
                                    'draw_odd': outcomes.get('Draw'),
                                    'away_odd': outcomes.get(away),
                                    'captured_at': datetime.utcnow().isoformat()
                                })
                        
                        # Handle O/U 2.5
                        elif market_key == 'totals':
                            outcomes = {o.get('name'): o.get('price') 
                                       for o in market.get('outcomes', [])}
                            over = outcomes.get('Over 2.5') or outcomes.get('Over')
                            under = outcomes.get('Under 2.5') or outcomes.get('Under')
                            if over and under:
                                odds_records.append({
                                    'event_id': event_id,
                                    'home_team': home,
                                    'away_team': away,
                                    'sport': sport_name,
                                    'bookmaker': bookmaker,
                                    'market': 'O/U 2.5',
                                    'over_odd': over,
                                    'under_odd': under,
                                    'captured_at': datetime.utcnow().isoformat()
                                })
        
        except Exception as e:
            print(f"Error collecting {sport_key}: {e}")
            continue
    
    return odds_records

def collect_tennis_odds():
    """Fetch tennis odds (ATP/WTA)."""
    odds_records = []
    
    tennis_sports = [
        ('tennis_atp', 'ATP'),
        ('tennis_wta', 'WTA'),
    ]
    
    for sport_key, tour in tennis_sports:
        try:
            url = f"{ODDS_API_BASE}/sports/{sport_key}/odds"
            params = {
                'apiKey': ODDS_API_KEY,
                'regions': 'us',
                'markets': 'h2h',
                'oddsFormat': 'decimal',
                'bookmakers': 'pinnacle,betmgm'
            }
            
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            
            games = resp.json()
            for game in games:
                event_id = game.get('id', '')
                home = game.get('home_team', '')
                away = game.get('away_team', '')
                
                for bookie in game.get('bookmakers', []):
                    bookmaker = bookie.get('key', '')
                    for market in bookie.get('markets', []):
                        if market.get('key') == 'h2h':
                            outcomes = {o.get('name'): o.get('price') 
                                       for o in market.get('outcomes', [])}
                            odds_records.append({
                                'event_id': event_id,
                                'home_team': home,
                                'away_team': away,
                                'sport': f'{tour}',
                                'bookmaker': bookmaker,
                                'market': 'H2H',
                                'home_odd': outcomes.get(home),
                                'away_odd': outcomes.get(away),
                                'captured_at': datetime.utcnow().isoformat()
                            })
        
        except Exception as e:
            print(f"Error collecting {sport_key}: {e}")
            continue
    
    return odds_records

# Test
if __name__ == '__main__':
    print("Testing odds collection...")
    football_odds = collect_odds_multiple_bookmakers()
    print(f"✓ Collected {len(football_odds)} football odds")
    tennis_odds = collect_tennis_odds()
    print(f"✓ Collected {len(tennis_odds)} tennis odds")
