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
TIMEFRAME = "5m"

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

def fetch_ohlcv(exchange):
    try:
        data = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=100)
        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert('Asia/Ho_Chi_Minh')
        df['ema_fast'] = df['close'].ewm(span=5, adjust=False).mean()
        df['ema_slow'] = df['close'].ewm(span=12, adjust=False).mean()
        df['ema_big'] = df['close'].ewm(span=30, adjust=False).mean()
        df['rsi14'] = compute_rsi(df['close'], 14)
        df['volume_ma'] = df['volume'].rolling(10).mean()
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
    
async def log_portfolio():
    try:
        ex = create_exchange()
        balance = ex.fetch_balance()
        usdt = float(balance['USDT']['total'])
        doge = float(balance['DOGE']['total'])
        price = (await ex.fetch_ticker(SYMBOL))['last']
        total_value = usdt + doge * price

        await send_telegram(
            f"📊 Báo cáo tài sản:\n"
            f"- USDT: {usdt:.2f}\n"
            f"- DOGE: {doge:.0f} (~{doge * price:.2f} USDT)\n"
            f"- Tổng tài sản: {total_value:.2f} USDT 💰"
        )
    except Exception as e:
        await send_telegram(f"❌ Lỗi log_portfolio: {str(e)}")

async def analyze_and_predict():
    ex = create_exchange()
    df = fetch_ohlcv(ex)
    if df is None:
        return

    price = df['close'].iloc[-1]
    ema_fast = df['ema_fast'].iloc[-1]
    ema_slow = df['ema_slow'].iloc[-1]
    ema_big = df['ema_big'].iloc[-1]
    rsi14 = df['rsi14'].iloc[-1]
    macd = df['macd'].iloc[-1]
    signal = df['signal'].iloc[-1]
    volume = df['volume'].iloc[-1]
    volume_ma = df['volume_ma'].iloc[-1]

    prediction = ""
    if ema_fast > ema_slow > ema_big and rsi14 < 65 and macd > signal and volume > volume_ma:
        prediction = "🚀 Dự đoán: Tín hiệu tăng giá mạnh"
    elif ema_fast < ema_slow and macd < signal:
        prediction = "🔻 Dự đoán: Tín hiệu giảm giá mạnh"
    else:
        prediction = "⏳ Dự đoán: Giá đi ngang hoặc chưa rõ xu hướng"

    await send_telegram(
"
        f"📈 Phân tích DOGE/USDT:
"
        f"- Giá hiện tại: {price:.4f}
"
        f"- EMA Fast: {ema_fast:.4f}
"
        f"- EMA Slow: {ema_slow:.4f}
"
        f"- EMA Big: {ema_big:.4f}
"
        f"- RSI14: {rsi14:.2f}
"
        f"- MACD: {macd:.4f} | Signal: {signal:.4f}
"
        f"- Volume hiện tại: {volume:.0f} | TB Volume: {volume_ma:.0f}
"
        f"{prediction}"
    )

async def runner():
    keep_alive()
    await send_telegram("🤖 Bot Dự đoán DOGE/USDT đã khởi động!")
    schedule.every(20).seconds.do(lambda: asyncio.ensure_future(analyze_and_predict()))
    schedule.every(5).minutes.do(lambda: asyncio.ensure_future(log_portfolio()))
    while True:
        schedule.run_pending()
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(runner())
