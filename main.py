import ccxt
import asyncio
import pandas as pd
import json
import os
from datetime import datetime
import schedule
import nest_asyncio
from telegram import Bot
from keep_alive import keep_alive

# === CONFIG ===
API_KEY = "99d39d59-c05d-4e40-9f2a-3615eac315ea"
API_SECRET = "4B1D25C8F05E12717AD561584B2853E6"
PASSPHRASE = "Mmoarb2025@"
TELEGRAM_TOKEN = "7817283052:AAF2fjxxZT8LP-gblBeTbpb0N0-a0C7GLQ8"
TELEGRAM_CHAT_ID = "5850622014"

SYMBOLS = ["DOGE/USDT", "ARB/USDT", "MAGIC/USDT"]
TIMEFRAME = "15m"

bot = Bot(token=TELEGRAM_TOKEN)
nest_asyncio.apply()

async def send_telegram(msg):
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
    except Exception as e:
        print(f"[Telegram Error] {e}")

def create_exchange():
    return ccxt.okx({
        'apiKey': API_KEY,
        'secret': API_SECRET,
        'password': PASSPHRASE,
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'}
    })

def fetch_ohlcv(exchange, symbol):
    try:
        data = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=100)
        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    except Exception as e:
        print(f"[OHLCV Error] {symbol}: {e}")
        return None

async def analyze_symbol(symbol):
    ex = create_exchange()
    df = fetch_ohlcv(ex, symbol)
    if df is None:
        return

    today = pd.Timestamp.utcnow().normalize()
    now = pd.Timestamp.utcnow()
    last_hours_df = df[df['timestamp'] > now - pd.Timedelta(hours=6)]
    today_df = df[df['timestamp'] > today]

    current_price = df['close'].iloc[-1]
    min_today = today_df['low'].min()
    max_today = today_df['high'].max()
    min_6h = last_hours_df['low'].min()
    max_6h = last_hours_df['high'].max()
    open_today = today_df['open'].iloc[0] if not today_df.empty else df['open'].iloc[0]
    change_today = (current_price - open_today) / open_today * 100 if open_today else 0

    # Xác định vùng giá hiện tại
    near = ""
    if current_price <= min_today * 1.01:
        near = "🌑 Gần đáy ngày"
    elif current_price >= max_today * 0.99:
        near = "☀️ Gần đỉnh ngày"

    msg = (
        f"📊 [{symbol}]
"
        f"Giá hiện tại: ${current_price:.4f}
"
        f"Biến động hôm nay: {change_today:.2f}%
"
        f"6h gần nhất: Min={min_6h:.4f}, Max={max_6h:.4f}
"
        f"{near}"
    )
    await send_telegram(msg)

async def runner():
    keep_alive()
    await send_telegram("🤖 Bot phân tích đa coin khởi động!")
    for sym in SYMBOLS:
        schedule.every(1).minutes.do(lambda s=sym: asyncio.ensure_future(analyze_symbol(s)))

    while True:
        schedule.run_pending()
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(runner())
