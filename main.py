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

SYMBOLS = ["MEMEFI/USDT"]
TIMEFRAME = "1m"  # đổi sang 1 phút để realtime hơn
TP_PERCENT = 0.02  # lời 2% thì bán
SL_PERCENT = 0.02  # lỗ 2% thì cắt

bot = Bot(token=TELEGRAM_TOKEN)
nest_asyncio.apply()

trade_memory = {}  # Lưu giá mua tại runtime

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
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert('Asia/Ho_Chi_Minh')
        df['ema_fast'] = df['close'].ewm(span=9, adjust=False).mean()
        df['ema_slow'] = df['close'].ewm(span=21, adjust=False).mean()
        return df
    except Exception as e:
        print(f"[OHLCV Error] {symbol}: {e}")
        return None

async def analyze_and_trade():
    ex = create_exchange()
    summary = "\n📊 PHÂN TÍCH + QUẢN LÝ LỆNH :\n"
    
    for symbol in SYMBOLS:
        df = fetch_ohlcv(ex, symbol)
        if df is None:
            continue

        price = df['close'].iloc[-1]
        ema_fast = df['ema_fast'].iloc[-1]
        ema_slow = df['ema_slow'].iloc[-1]

        holding = trade_memory.get(symbol)

        # Nếu đang hold, kiểm tra TP/SL
        if holding:
            buy_price = holding['buy_price']
            amount = holding['amount']
            if price >= buy_price * (1 + TP_PERCENT):
                try:
                    ex.create_market_sell_order(symbol, amount)
                    await send_telegram(f"✅ TP BÁN {amount} {symbol} tại {price:.4f}")
                    trade_memory.pop(symbol)
                except Exception as e:
                    await send_telegram(f"❌ Lỗi khi TP SELL {symbol}: {e}")
            elif price <= buy_price * (1 - SL_PERCENT):
                try:
                    ex.create_market_sell_order(symbol, amount)
                    await send_telegram(f"🛑 SL CẮT LỖ {amount} {symbol} tại {price:.4f}")
                    trade_memory.pop(symbol)
                except Exception as e:
                    await send_telegram(f"❌ Lỗi khi SL SELL {symbol}: {e}")
        else:
            # Nếu chưa hold, tìm điểm mua
            if ema_fast > ema_slow:
                balance = ex.fetch_balance()
                usdt = float(balance.get('USDT', {}).get('free', 0))
                if usdt > 5:
                    amount = round(usdt * 0.2 / price, 2)
                    try:
                        order = ex.create_market_buy_order(symbol, amount)
                        avg_price = order['average'] or price
                        trade_memory[symbol] = {'buy_price': avg_price, 'amount': amount}
                        await send_telegram(f"🚀 MUA {amount} {symbol} tại {avg_price:.4f}")
                    except Exception as e:
                        await send_telegram(f"❌ Lỗi khi BUY {symbol}: {e}")

        summary += f"\n🪙 {symbol} - Giá: ${price:.4f} - EMA: {'Bullish ✅' if ema_fast > ema_slow else 'Bearish ❌'}"

    await send_telegram(summary)

async def runner():
    keep_alive()
    await send_telegram("🤖 Bot MEMEFI Auto Trading khởi động!")
    schedule.every(10).seconds.do(lambda: asyncio.ensure_future(analyze_and_trade()))
    while True:
        schedule.run_pending()
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(runner())
