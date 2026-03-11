# RProxy

ChatGPT Account Pool Proxy - Multi-account management with token-based billing

## Features

- Multi-account pool management: Support for Free and Rikka (Team) account pools
- Token-based billing: Track usage by OpenAI official pricing
- Rate limit monitoring: Real-time 5h/week limit tracking with auto-recovery
- Web dashboard: Beautiful status page with pool switching
- API key management: Multiple API keys with quota control

## Architecture

- proxy.py - Main proxy server (port 8765)
- rikka_accounts.json - Team account pool (priority)
- free_accounts.json - Free account pool (fallback)
- keys.json - API key configuration
- usage.json - Usage statistics

## Usage

python3 proxy.py

Access dashboard at: http://localhost:8765/status
