import ccxt.async_support as ccxt
import asyncio
import pandas as pd
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

SYMBOLS = ["DOGE/USDT"]
bot = Bot(token=TELEGRAM_TOKEN)
nest_asyncio.apply()

last_total_value_usd = None  # Lưu tổng tài sản USD để tránh báo lặp
daily_start_capital_usd = 0.0  # Tổng tài sản USD tại 21:00
last_day = None  # Ngày cuối cùng cập nhật

async def send_telegram(msg):
    vn_time = datetime.now(timezone(timedelta(hours=7))).strftime('%H:%M:%S %d/%m/%Y')
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"{msg}\n⏰ Giờ VN: {vn_time}")

def create_exchange():
    return ccxt.okx({
        'apiKey': API_KEY,
        'secret': API_SECRET,
        'password': PASSPHRASE,
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'}
    })

async def fetch_usdt_usd_rate(exchange):
    try:
        ticker = await exchange.fetch_ticker("USDT/USD")
        return float(ticker['last'])
    except Exception:
        return 1.0  # Mặc định 1:1 nếu lỗi

async def fetch_ohlcv(exchange, symbol, timeframe, limit=100):
    try:
        data = await exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=100)
        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert('Asia/Ho_Chi_Minh')
        
        df['ema_fast'] = df['close'].ewm(span=5, adjust=False).mean()
        df['ema_slow'] = df['close'].ewm(span=12, adjust=False).mean()
        df['rsi14'] = compute_rsi(df['close'], 14)
        df['resistance'] = df['high'].rolling(20).max()
        df['volume_ma'] = df['volume'].rolling(10).mean()
        
        ema12 = df['close'].ewm(span=12, adjust=False).mean()
        ema26 = df['close'].ewm(span=26, adjust=False).mean()
        df['macd'] = ema12 - ema26
        df['signal'] = df['macd'].ewm(span=9, adjust=False).mean()
        
        df['tr'] = pd.concat([df['high'] - df['low'], 
                              (df['high'] - df['close'].shift()).abs(), 
                              (df['low'] - df['close'].shift()).abs()], axis=1).max(axis=1)
        df['atr'] = df['tr'].rolling(14).mean()
        
        return df
    except Exception as e:
        await send_telegram(f"❌ [OHLCV Error] {symbol} ({timeframe}): {e}")
        return None

def compute_rsi(series, period):
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def is_strong_uptrend(df):
    last_candle = df.iloc[-1]
    return last_candle['ema_fast'] > last_candle['ema_slow']

def is_strong_downtrend(df):
    last_candle = df.iloc[-1]
    return last_candle['ema_fast'] < last_candle['ema_slow']

def is_market_safe(df_1h):
    last_candle = df_1h.iloc[-1]
    prev_candle = df_1h.iloc[-2]
    price_change = (last_candle['close'] - prev_candle['close']) / prev_candle['close']
    return price_change > -0.05

def is_volatile_enough(df, threshold=0.003):
    last_candle = df.iloc[-1]
    atr_percent = last_candle['atr'] / last_candle['close']
    return atr_percent > threshold

def should_increase(df):
    last_candle = df.iloc[-1]
    prev_candle = df.iloc[-2]
    trend_strategy = (
        last_candle['ema_fast'] > last_candle['ema_slow'] and
        last_candle['rsi14'] < 70 and
        last_candle['macd'] > last_candle['signal'] and
        prev_candle['macd'] <= prev_candle['signal']
    )
    breakout_strategy = (
        last_candle['close'] > prev_candle['resistance'] and
        last_candle['volume'] > last_candle['volume_ma']
    )
    return trend_strategy or breakout_strategy

def should_decrease(df):
    last_candle = df.iloc[-1]
    prev_candle = df.iloc[-2]
    trend_strategy = (
        last_candle['ema_fast'] < last_candle['ema_slow'] and
        last_candle['rsi14'] > 30 and
        last_candle['macd'] < last_candle['signal'] and
        prev_candle['macd'] >= prev_candle['signal']
    )
    breakdown_strategy = (
        last_candle['close'] < prev_candle['low'].rolling(20).min() and
        last_candle['volume'] > last_candle['volume_ma']
    )
    return trend_strategy or breakdown_strategy

async def log_assets(exchange):
    global daily_start_capital_usd, last_day, last_total_value_usd
    try:
        balance = await exchange.fetch_balance()
        total_value_usdt = 0.0
        usdt_usd_rate = await fetch_usdt_usd_rate(exchange)

        # Lấy số dư từ balance['total']
        usdt = float(balance['total'].get('USDT', 0.0))
        total_value_usdt = usdt

        coins = {}
        for currency in balance['total']:
            coin_balance = float(balance['total'].get(currency, 0.0))
            if coin_balance > 0 and currency != 'USDT':
                try:
                    symbol = f"{currency}/USDT"
                    ticker = await exchange.fetch_ticker(symbol)
                    price = ticker['last']
                    coin_value = coin_balance * price
                    total_value_usdt += coin_value
                    coins[currency] = {'balance': coin_balance, 'price': price, 'value_usd': coin_value * usdt_usd_rate}
                except Exception:
                    continue

        total_value_usd = total_value_usdt * usdt_usd_rate

        # Cập nhật vốn khởi đầu ngày (21:00)
        now = datetime.now(timezone(timedelta(hours=7)))
        today = now.date()
        if last_day is None or (today != last_day and now.hour >= 21):
            daily_start_capital_usd = total_value_usd
            last_day = today

        # Tính lợi nhuận
        profit_percent = ((total_value_usd - daily_start_capital_usd) / daily_start_capital_usd * 100) if daily_start_capital_usd > 0 else 0

        # Log tài sản
        if last_total_value_usd > 0.01 or abs(total_value_usd - last_total_value_usd) > 0.01:
            msg = f"💰 Tổng tài sản: {total_value_usd:.2f} USD\n💵 USDT: {usdt:.2f}\n"
            for currency, data in coins.items():
                msg += f"🪙 {currency}: {data['balance']:.4f} | Giá: {data['price']:.4f} | Giá trị: {data['value_usd']:.2f} USD\n"
            msg += f"📈 Lợi nhuận 10).minutes.do(lambda: asyncio.ensure_future(log_assets(exchange)))
    while True:
        schedule.run_pending()
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(runner())
