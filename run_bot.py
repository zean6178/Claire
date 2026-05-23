#!/usr/bin/env python3
"""
DLMM Memecoin Fee-Farming Bot
Run: python3 run_bot.py

Environment variables (or set in .env):
  SOLANA_RPC_URL    - Solana RPC endpoint (use paid RPC for speed)
  WALLET_KEY_PATH   - Path to wallet JSON keypair file
  MAX_TOTAL_SOL     - Max SOL to deploy across all positions
  MAX_PER_POSITION_SOL - Max SOL per single position
"""

import sys
import os

# Optional: load .env file
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Ensure the project root is in path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dlmm_bot.bot import main

if __name__ == "__main__":
    main()
