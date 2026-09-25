#!/usr/bin/env python3
"""
Continuous runner for Poly-Tifu trading bot.
Runs the bot in a loop with configurable intervals.
"""
import os
import sys
import asyncio
import time
import signal
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from src.config import Config
from src.crypto import KeyManager, InvalidPasswordError, CryptoError
from src.bot import TradingBot
from scripts.run_bot import check_env_mode, load_config_from_env, get_private_key_from_env

# ANSI color codes
class Colors:
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

def print_header(title: str) -> None:
    print(f"\n{Colors.BOLD}{Colors.BLUE}{'=' * 50}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BLUE}{title:^50}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BLUE}{'=' * 50}{Colors.RESET}\n")

def print_success(msg: str) -> None:
    print(f"{Colors.GREEN}✓{Colors.RESET} {msg}")

def print_error(msg: str) -> None:
    print(f"{Colors.RED}✗{Colors.RESET} {msg}")

running = True

def signal_handler(signum, frame):
    global running
    print(f"\n{Colors.YELLOW}Shutdown signal received...{Colors.RESET}")
    running = False

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

async def run_bot_loop(bot: TradingBot, interval_seconds: int = 60):
    """Run the bot continuously."""
    print_header("Continuous Trading Bot Started")
    print(f"{Colors.CYAN}Interval: {interval_seconds}s | Press Ctrl+C to stop{Colors.RESET}\n")
    
    iteration = 0
    while running:
        iteration += 1
        print(f"\n{Colors.BOLD}--- Iteration {iteration} ---{Colors.RESET}")
        
        try:
            # Show status
            from scripts.run_bot import print_status
            await print_status(bot)
            
            # Get market price if configured
            if bot.config.default_token_id:
                print(f"\n{Colors.BOLD}Market Price:{Colors.RESET}")
                price_data = await bot.get_market_price(bot.config.default_token_id)
                if price_data:
                    print(f"  {price_data}")
                else:
                    print("  Failed to get price")
            
            # Check for open orders
            orders = await bot.get_open_orders()
            if orders:
                print(f"\n{Colors.BOLD}Open Orders ({len(orders)}):{Colors.RESET}")
                for order in orders[:5]:
                    print(f"  - {order.get('side', '?')} {order.get('size', '?')} @ {order.get('price', '?')}")
            
        except Exception as e:
            print_error(f"Error in iteration: {e}")
        
        # Sleep until next iteration
        if running:
            print(f"\n{Colors.CYAN}Sleeping {interval_seconds}s...{Colors.RESET}")
            for _ in range(interval_seconds):
                if not running:
                    break
                await asyncio.sleep(1)
    
    print(f"\n{Colors.GREEN}Bot stopped gracefully.{Colors.RESET}")

def main():
    print_header("Polymarket Trading Bot - Continuous Mode")
    
    # Get interval from env (default 60s)
    interval = int(os.environ.get("POLY_BOT_INTERVAL", "60"))
    
    # Check for environment variable mode
    use_env_mode = check_env_mode()
    
    if use_env_mode:
        print_success("Using environment variables mode")
        config = load_config_from_env()
        private_key = get_private_key_from_env()
        print_success(f"Configuration loaded (gasless: {config.use_gasless})")
    else:
        print(f"{Colors.YELLOW}Environment variables not found, using encrypted key mode{Colors.RESET}")
        print(f"  Tip: Set POLY_PRIVATE_KEY and POLY_SAFE_ADDRESS in .env for easier setup\n")
        
        # Load configuration
        config = Config.load("config.yaml")
        print_success(f"Configuration loaded (gasless: {config.use_gasless})")
        
        # Decrypt private key
        from scripts.run_bot import decrypt_private_key
        private_key = decrypt_private_key()
    
    # Initialize bot
    try:
        bot = TradingBot(
            config=config,
            private_key=private_key,
        )
    except Exception as e:
        print_error(f"Failed to initialize bot: {e}")
        sys.exit(1)
    
    print_success("Bot initialized!")
    
    # Run continuous loop
    try:
        asyncio.run(run_bot_loop(bot, interval))
    except KeyboardInterrupt:
        print("\n\nInterrupted.")
        sys.exit(0)
    except Exception as e:
        print(f"\n{Colors.RED}Error: {e}{Colors.RESET}")
        sys.exit(1)

if __name__ == "__main__":
    main()
