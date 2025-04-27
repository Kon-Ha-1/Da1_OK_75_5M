import ccxt
import asyncio
import pandas as pd
import os
from datetime import datetime, timezone, timedelta
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

SYMBOL = "DOGE/USDT"
TIMEFRAME = "1m"
TP_PERCENT = 0.05  # Take Profit 5%
SL_PERCENT = 0.03  # Stop Loss 3%

bot = Bot(token=TELEGRAM_TOKEN)
nest_asyncio.apply()

trade_memory = {}  # Lưu lệnh đang hold

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

def fetch_ohlcv(exchange):
    try:
        data = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=100)
        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert('Asia/Ho_Chi_Minh')
        df['ema_fast'] = df['close'].ewm(span=9, adjust=False).mean()
        df['ema_slow'] = df['close'].ewm(span=21, adjust=False).mean()
        df['rsi3'] = compute_rsi(df['close'], 3)
        df['rsi14'] = compute_rsi(df['close'], 14)
        ema12 = df['close'].ewm(span=12, adjust=False).mean()
        ema26 = df['close'].ewm(span=26, adjust=False).mean()
        df['macd'] = ema12 - ema26
        df['signal'] = df['macd'].ewm(span=9, adjust=False).mean()
        return df
    except Exception as e:
        print(f"[OHLCV Error] {e}")
        return None

def compute_rsi(series, period):
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

async def analyze_and_trade():
    ex = create_exchange()
    df = fetch_ohlcv(ex)
    if df is None:
        return

    price = df['close'].iloc[-1]
    ema_fast = df['ema_fast'].iloc[-1]
    ema_slow = df['ema_slow'].iloc[-1]
    rsi3 = df['rsi3'].iloc[-1]
    rsi14 = df['rsi14'].iloc[-1]
    macd = df['macd'].iloc[-1]
    signal = df['signal'].iloc[-1]

    holding = trade_memory.get(SYMBOL)

    if holding:
        buy_price = holding['buy_price']
        amount = holding['amount']
        if price >= buy_price * (1 + TP_PERCENT):
            try:
                ex.create_market_sell_order(SYMBOL, amount)
                await send_telegram(f"✅ TP BÁN {amount} DOGE tại {price:.4f}")
                trade_memory.pop(SYMBOL)
            except Exception as e:
                await send_telegram(f"❌ Lỗi khi TP SELL: {e}")
        elif price <= buy_price * (1 - SL_PERCENT):
            try:
                ex.create_market_sell_order(SYMBOL, amount)
                await send_telegram(f"🛑 SL CẮT LỖ {amount} DOGE tại {price:.4f}")
                trade_memory.pop(SYMBOL)
            except Exception as e:
                await send_telegram(f"❌ Lỗi khi SL SELL: {e}")
    else:
        if ema_fast > ema_slow and rsi3 < 35 and macd > signal:
            balance = ex.fetch_balance()
            usdt = float(balance.get('USDT', {}).get('free', 0))
            if usdt > 5:
                amount = round(usdt * 0.2 / price, 2)
                try:
                    order = ex.create_market_buy_order(SYMBOL, amount)
                    avg_price = order['average'] or price
                    trade_memory[SYMBOL] = {'buy_price': avg_price, 'amount': amount}
                    await send_telegram(f"🚀 MUA {amount} DOGE tại {avg_price:.4f}")
                except Exception as e:
                    await send_telegram(f"❌ Lỗi khi BUY: {e}")

async def runner():
    keep_alive()
    await send_telegram("🤖 Bot MEMEFI Real-Time Trading khởi động!")
    schedule.every(10).seconds.do(lambda: asyncio.ensure_future(analyze_and_trade()))
    while True:
        schedule.run_pending()
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(runner())
