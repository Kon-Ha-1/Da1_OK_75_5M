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

SYMBOL = "DOGE/USDT"
TIMEFRAME = "5m"
RSI_PERIOD = 14
EMA_FAST = 9
EMA_SLOW = 21
TP_PERCENT = 0.08
SL_PERCENT = 0.04
MIN_NOTIONAL = 1.0
ORDER_FILE = "swing_orders.json"

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

def compute_rsi(series, period):
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def is_bullish_pattern(df):
    c1, o1, c2, o2 = df['close'].iloc[-2], df['open'].iloc[-2], df['close'].iloc[-1], df['open'].iloc[-1]
    is_engulfing = c2 > o2 and o1 > c1 and c2 > o1 and o2 < c1
    is_pinbar = abs(o2 - c2) / (df['high'].iloc[-1] - df['low'].iloc[-1]) < 0.3
    is_doji = abs(c2 - o2) <= 0.001
    return is_engulfing or is_pinbar or is_doji

def compute_macd(df):
    ema12 = df['close'].ewm(span=12, adjust=False).mean()
    ema26 = df['close'].ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    df['macd'] = macd
    df['signal'] = signal
    return df

def fetch_ohlcv(exchange):
    try:
        data = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=100)
        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['ema_fast'] = df['close'].ewm(span=EMA_FAST, adjust=False).mean()
        df['ema_slow'] = df['close'].ewm(span=EMA_SLOW, adjust=False).mean()
        df['rsi'] = compute_rsi(df['close'], RSI_PERIOD)
        df = compute_macd(df)
        return df
    except Exception as e:
        print(f"[OHLCV Error] {e}")
        return None

def load_orders():
    if os.path.exists(ORDER_FILE):
        with open(ORDER_FILE, 'r') as f:
            return json.load(f)
    return []

def save_orders(data):
    with open(ORDER_FILE, 'w') as f:
        json.dump(data, f)

def get_price(exchange):
    try:
        return float(exchange.fetch_ticker(SYMBOL)['last'])
    except:
        return None

async def check_existing_holdings():
    ex = create_exchange()
    balance = ex.fetch_balance()
    coin = SYMBOL.split('/')[0]
    price = get_price(ex)
    if not price:
        return

    coin_amt = float(balance.get(coin, {}).get('free', 0))
    if coin_amt * price < MIN_NOTIONAL:
        return

    buy_price = load_orders()[0]['buy_price'] if load_orders() else price

    if price >= buy_price * (1 + TP_PERCENT):
        ex.create_market_sell_order(SYMBOL, coin_amt)
        await send_telegram(f"💰 TP SELL {coin_amt} {coin} tại {price:.4f} (Giá mua: {buy_price:.4f})")
        save_orders([])
    elif price <= buy_price * (1 - SL_PERCENT):
        ex.create_market_sell_order(SYMBOL, coin_amt)
        await send_telegram(f"🔻 SL SELL {coin_amt} {coin} tại {price:.4f} (Giá mua: {buy_price:.4f})")
        save_orders([])

async def strategy():
    ex = create_exchange()
    df = fetch_ohlcv(ex)
    if df is None:
        return

    last_row = df.iloc[-1]
    rsi = last_row['rsi']
    ema_fast = last_row['ema_fast']
    ema_slow = last_row['ema_slow']
    macd = last_row['macd']
    signal = last_row['signal']
    price = float(last_row['close'])

    pattern_ok = is_bullish_pattern(df)
    trend_ok = ema_fast > ema_slow or (df['ema_fast'].iloc[-2] < df['ema_slow'].iloc[-2] and ema_fast > ema_slow)
    rsi_ok = 45 < rsi < 75
    macd_ok = macd > signal or abs(macd - signal) < 0.002

    open_orders = load_orders()
    balance = ex.fetch_balance()
    usdt = float(balance.get('USDT', {}).get('free', 0))

    msg_debug = (
        f"📊 Giá hiện tại: ${price:.4f}\n"
        f"🎯 Trend={'✅' if trend_ok else '❌'}, RSI={rsi:.2f} ({'✅' if rsi_ok else '❌'}), MACD={'✅' if macd_ok else '❌'}, Nến={'✅' if pattern_ok else '❌'}"
    )
    await send_telegram(msg_debug)

    valid_signals = sum([trend_ok, rsi_ok, macd_ok, pattern_ok])

    if not open_orders and valid_signals >= 3 and usdt > 10:
        amount = round(usdt / price, 2)
        order = ex.create_market_buy_order(SYMBOL, amount)
        buy_price = order['average'] or price
        save_orders([{
            'buy_price': buy_price,
            'amount': amount,
            'timestamp': str(datetime.utcnow())
        }])
        await send_telegram(f"🚀 BUY {amount} DOGE tại {buy_price:.4f} (Tín hiệu đạt {valid_signals}/4)")
    elif not open_orders:
        await send_telegram(f"🤖 Tín hiệu chưa đủ mạnh ({valid_signals}/4). Đang theo dõi...")

async def runner():
    keep_alive()
    await send_telegram("🤖 Bot Swing DOGE + AI phân tích kỹ thuật đã khởi động!")
    schedule.every(5).minutes.do(lambda: asyncio.ensure_future(strategy()))
    schedule.every(2).minutes.do(lambda: asyncio.ensure_future(check_existing_holdings()))
    while True:
        schedule.run_pending()
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(runner())
