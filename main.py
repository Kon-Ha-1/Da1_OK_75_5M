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

        # Log tài sản, chỉ hiển thị coin có giá trị > 0.1 USD
        if last_total_value_usd is None or abs(total_value_usd - last_total_value_usd) > 0.01:
            msg = f"💰 Tổng tài sản: {total_value_usd:.2f} USD\n💵 USDT: {usdt:.2f}\n"
            for currency, data in coins.items():
                if data['value_usd'] > 0.1:  # Chỉ hiển thị coin có giá trị > 0.1 USD
                    msg += f"🪙 {currency}: {data['balance']:.4f} | Giá: {data['price']:.4f} | Giá trị: {data['value_usd']:.2f} USD\n"
            msg += f"📈 Lợi nhuận hôm nay: {profit_percent:.2f}%"
            await send_telegram(msg)
            last_total_value_usd = total_value_usd
    except Exception as e:
        await send_telegram(f"❌ Lỗi log tài sản: {str(e)}")

async def predict_doge(exchange):
    symbol = SYMBOLS[0]
    df_5m = await fetch_ohlcv(exchange, symbol, '5m', limit=100)
    df_15m = await fetch_ohlcv(exchange, symbol, '15m', limit=100)
    df_1h = await fetch_ohlcv(exchange, symbol, '1h', limit=100)
    if df_5m is None or df_15m is None or df_1h is None:
        return

    current_price = df_5m['close'].iloc[-1]
    reasons = []
    can_predict = True
    trends = {}

    # Kiểm tra điều kiện
    if not is_strong_uptrend(df_5m) and not is_strong_downtrend(df_5m):
        reasons.append("5m: Không có xu hướng rõ ràng (EMA5 ≈ EMA12)")
        can_predict = False
    if not is_strong_uptrend(df_15m) and not is_strong_downtrend(df_15m):
        reasons.append("15m: Không có xu hướng rõ ràng (EMA5 ≈ EMA12)")
        can_predict = False
    if not is_market_safe(df_1h):
        reasons.append("1h: Thị trường không an toàn (giá giảm >5%)")
        can_predict = False
    if not is_volatile_enough(df_5m, 0.003):
        reasons.append("5m: Biến động thấp (ATR < 0.3%)")
        can_predict = False

    # Dự đoán
    if can_predict:
        # 15 phút (5m)
        atr_5m = df_5m['atr'].iloc[-1] / current_price * 100
        if should_increase(df_5m):
            trends['15m'] = ("increase", min(atr_5m, 1.0))
        elif should_decrease(df_5m):
            trends['15m'] = ("decrease", min(atr_5m, 0.5))
        
        # 30 phút (15m)
        atr_15m = df_15m['atr'].iloc[-1] / current_price * 100
        if should_increase(df_15m):
            trends['30m'] = ("increase", min(atr_15m, 1.5))
        elif should_decrease(df_15m):
            trends['30m'] = ("decrease", min(atr_15m, 0.75))
        
        # 1 giờ (1h)
        atr_1h = df_1h['atr'].iloc[-1] / current_price * 100
        if should_increase(df_1h):
            trends['1h'] = ("increase", min(atr_1h, 2.0))
        elif should_decrease(df_1h):
            trends['1h'] = ("decrease", min(atr_1h, 1.0))

    # Gửi thông báo
    if not can_predict:
        await send_telegram(f"⏳ {symbol}: Không dự đoán. Lý do: {', '.join(reasons)}")
    elif trends:
        prediction_msg = f"🔮 Dự đoán giá {symbol}:\n"
        for timeframe, (trend, change) in trends.items():
            if trend == "increase":
                prediction_msg += f"📈 {timeframe}: TĂNG {change:.2f}% (dựa trên ATR: {locals()[f'atr_{timeframe.lower()}']:.2f}%)\n"
            else:
                prediction_msg += f"📉 {timeframe}: GIẢM {change:.2f}% (dựa trên ATR: {locals()[f'atr_{timeframe.lower()}']:.2f}%)\n"
        await send_telegram(prediction_msg)

async def runner():
    keep_alive()
    exchange = create_exchange()
    await send_telegram("🤖 Bot dự đoán DOGE đã khởi động! Chạy 24/7")
    #schedule.every(15).seconds.do(lambda: asyncio.ensure_future(predict_doge(exchange)))
    schedule.every(10).minutes.do(lambda: asyncio.ensure_future(log_assets(exchange)))
    while True:
        schedule.run_pending()
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(runner())
