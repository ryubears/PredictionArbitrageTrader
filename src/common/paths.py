"""
Where the project keeps its files, and the name it goes by.
"""

from pathlib import Path

NAME = "PredictionArbitrageTrader"     # In the alerts' subjects and the user agent the venues see. SportsArb until 2026-10-12.
ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "data"     # Databases, logs, and venue key files. Not committed.
