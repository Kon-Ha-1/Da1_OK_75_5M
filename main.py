import ccxt.async_support as ccxt
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

SYMBOLS = ["DOGE/USDT", "SHIB/USDT", "SOL/USDT", "BNB/USDT", "NEAR/USDT"]
TIMEFRAME = "5m"
TP_PERCENT = 0.01  # Take Profit 1%
SL_PERCENT = 0.005  # Stop Loss 0.5%
RISK_PER_TRADE = 0.02  # Rủi ro 2% vốn
MIN_BALANCE_PER_TRADE = 3  # Tối thiểu $3 mỗi lệnh
MAX_LOSSES_PER_DAY = 2  # Tạm dừng coin nếu thua 2 lệnh/ngày
DAILY_PROFIT_TARGET = 0.03  # Mục tiêu 3% mỗi ngày
MAX_DAILY_LOSS = 0.05  # Dừng bot nếu lỗ >5% trong ngày

bot = Bot(token=TELEGRAM_TOKEN)
nest_asyncio.apply()

trade_memory = {}
loss_tracker = {symbol: {'count': 0, 'date': None} for symbol in SYMBOLS}
capital = 75.0  # Vốn ban đầu
daily_profit = 0.0
daily_start_capital = capital
last_day = (datetime.now(timezone(timedelta(hours=7))).date())  # Múi giờ Việt Nam

async def send_telegram(msg):
    try:
        vn_time = datetime.now(timezone(timedelta(hours=7))).strftime('%H:%M:%S %d/%m/%Y')
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"{msg}\n⏰ Giờ VN: {vn_time}")
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

async def fetch_ohlcv(exchange, symbol, timeframe='5m'):
    try:
        data = await exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=100)
        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert('Asia/Ho_Chi_Minh')
        
        df['ema_fast'] = df['close'].ewm(span=5, adjust=False).mean()
        df['ema_slow'] = df['close'].ewm(span=12, adjust=False).mean()
        df['ema_big'] = df['close'].ewm(span=30, adjust=False).mean()
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
        print(f"[OHLCV Error] {symbol}: {e}")
        return None

async def fetch_ohlcv_15m(exchange, symbol):
    try:
        data = await exchange.fetch_ohlcv(symbol, timeframe='15m', limit=100)
        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert('Asia/Ho_Chi_Minh')
        df['ema_fast'] = df['close'].ewm(span=5, adjust=False).mean()
        df['ema_slow'] = df['close'].ewm(span=12, adjust=False).mean()
        df['ema_big'] = df['close'].ewm(span=30, adjust=False).mean()
        return df
    except Exception as e:
        print(f"[OHLCV 15m Error] {symbol}: {e}")
        return None

async def fetch_ohlcv_1h(exchange, symbol):
    try:
        data = await exchange.fetch_ohlcv(symbol, timeframe='1h', limit=2)
        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert('Asia/Ho_Chi_Minh')
        return df
    except Exception as e:
        print(f"[OHLCV 1h Error] {symbol}: {e}")
        return None

def compute_rsi(series, period):
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def is_strong_uptrend(df_15m):
    last_candle = df_15m.iloc[-1]
    return (last_candle['ema_fast'] > last_candle['ema_slow'] and 
            last_candle['ema_slow'] > last_candle['ema_big'])

def is_market_safe(df_1h):
    last_candle = df_1h.iloc[-1]
    prev_candle = df_1h.iloc[-2]
    price_change = (last_candle['close'] - prev_candle['close']) / prev_candle['close']
    return price_change > -0.03

def is_volatile_enough(df_5m):
    last_candle = df_5m.iloc[-1]
    atr_percent = last_candle['atr'] / last_candle['close']
    return atr_percent > 0.007

def should_buy(df):
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

def can_trade(symbol):
    tracker = loss_tracker[symbol]
    today = datetime.now(timezone(timedelta(hours=7))).date()
    if tracker['date'] != today:
        tracker['count'] = 0
        tracker['date'] = today
    return tracker['count'] < MAX_LOSSES_PER_DAY

async def update_capital():
    global capital, daily_profit, daily_start_capital, last_day
    try:
        ex = create_exchange()
        balance = await ex.fetch_balance()
        usdt = float(balance['USDT']['total'])
        total_value = usdt
        
        for symbol in SYMBOLS:
            coin = symbol.split('/')[0]
            coin_balance = float(balance.get(coin, {}).get('total', 0))
            if coin_balance > 0:
                ticker = await ex.fetch_ticker(symbol)
                price = ticker['last']
                total_value += coin_balance * price
        
        today = datetime.now(timezone(timedelta(hours=7))).date()
        if today != last_day:
            daily_profit = (total_value - daily_start_capital) / daily_start_capital
            if daily_profit < DAILY_PROFIT_TARGET:
                await send_telegram(
                    f"⚠️ Lợi nhuận ngày {last_day} không đạt 3%: {daily_profit*100:.2f}%\n"
                    f"Bot tạm dừng đến ngày mai."
                )
                return False
            
            capital = total_value
            daily_start_capital = total_value
            last_day = today
            await send_telegram(
                f"📈 Cập nhật vốn ngày {today}: {capital:.2f} USDT\n"
                f"🎯 Lợi nhuận ngày trước: {daily_profit*100:.2f}%"
            )
        
        if (daily_start_capital - total_value) / daily_start_capital > MAX_DAILY_LOSS:
            await send_telegram(
                f"🛑 Lỗ vượt 5% trong ngày: {total_value:.2f} USDT\n"
                f"Bot tạm dừng đến ngày mai."
            )
            return False
        
        await ex.close()
        return True
    except Exception as e:
        await send_telegram(f"❌ Lỗi cập nhật vốn: {str(e)}")
        return True

async def analyze_and_trade():
    if not await update_capital():
        return

    ex = create_exchange()
    for symbol in SYMBOLS:
        if not can_trade(symbol):
            continue

        df_5m = await fetch_ohlcv(ex, symbol, TIMEFRAME)
        df_15m = await fetch_ohlcv_15m(ex, symbol)
        df_1h = await fetch_ohlcv_1h(ex, symbol)
        if df_5m is None or df_15m is None or df_1h is None:
            continue

        price = df_5m['close'].iloc[-1]
        holding = trade_memory.get(symbol)

        if holding:
            buy_price = holding['buy_price']
            amount = holding['amount']
            sl_price = holding.get('sl_price', buy_price * (1 - SL_PERCENT))
            
            if price >= buy_price * 1.003:
                sl_price = max(sl_price, price * (1 - SL_PERCENT))
                trade_memory[symbol]['sl_price'] = sl_price
            
            if price >= buy_price * (1 + TP_PERCENT):
                try:
                    await ex.create_market_sell_order(symbol, amount)
                    profit_usdt = (price - buy_price) * amount
                    global daily_profit
                    daily_profit += profit_usdt / daily_start_capital
                    await send_telegram(
                        f"✅ TP BÁN {amount:.0f} {symbol.split('/')[0]}\n"
                        f"💰 Lợi nhuận: +{profit_usdt:.2f} USDT ({TP_PERCENT*100}%)\n"
                    )
                    trade_memory.pop(symbol)
                except Exception as e:
                    await send_telegram(f"❌ Lỗi khi TP SELL {symbol}: {e}")
            
            elif price <= sl_price:
                try:
                    await ex.create_market_sell_order(symbol, amount)
                    loss_usdt = (buy_price - price) * amount
                    loss_tracker[symbol]['count'] += 1
                    global daily_profit
                    daily_profit -= loss_usdt / daily_start_capital
                    await send_telegram(
                        f"🛑 SL CẮT LỖ {amount:.0f} {symbol.split('/')[0]}\n"
                        f"💸 Lỗ: -{loss_usdt:.2f} USDT ({SL_PERCENT*100}%)\n"
                        f"📉 Lỗ hôm nay: {loss_tracker[symbol]['count']}/{MAX_LOSSES_PER_DAY}"
                    )
                    trade_memory.pop(symbol)
                except Exception as e:
                    await send_telegram(f"❌ Lỗi khi SL SELL {symbol}: {e}")
        
        elif (should_buy(df_5m) and 
              is_strong_uptrend(df_15m) and 
              is_market_safe(df_1h) and 
              is_volatile_enough(df_5m)):
            try:
                balance = await ex.fetch_balance()
                usdt_balance = float(balance['USDT']['free'])
                if usdt_balance >= MIN_BALANCE_PER_TRADE:
                    amount = round((usdt_balance * RISK_PER_TRADE) / price, 0)
                    if amount * price >= MIN_BALANCE_PER_TRADE:
                        order = await ex.create_market_buy_order(symbol, amount)
                        avg_price = order['average'] or price
                        trade_memory[symbol] = {
                            'buy_price': avg_price,
                            'amount': amount,
                            'sl_price': avg_price * (1 - SL_PERCENT),
                            'timestamp': datetime.now(timezone(timedelta(hours=7))).isoformat()
                        }
                        await send_telegram(
                            f"🚀 MUA {amount:.0f} {symbol.split('/')[0]} tại {avg_price:.4f}\n"
                            f"🎯 TP: {avg_price * (1 + TP_PERCENT):.4f} (+{TP_PERCENT*100}%)\n"
                            f"🔪 SL: {avg_price * (1 - SL_PERCENT):.4f} (-{SL_PERCENT*100}%)"
                        )
            except Exception as e:
                await send_telegram(f"❌ Lỗi khi BUY {symbol}: {str(e)}")
    
    await ex.close()

async def log_portfolio():
    try:
        ex = create_exchange()
        balance = await ex.fetch_balance()
        usdt = float(balance['USDT']['total'])
        total_value = usdt
        portfolio_msg = f"📊 Báo cáo tài sản\n💵 USDT: {usdt:.2f}\n"
        
        for symbol in SYMBOLS:
            coin = symbol.split('/')[0]
            coin_balance = float(balance.get(coin, {}).get('total', 0))
            if coin_balance > 0:
                ticker = await ex.fetch_ticker(symbol)
                price = ticker['last']
                coin_value = coin_balance * price
                total_value += coin_value
                portfolio_msg += f"🪙 {coin}: {coin_balance:.0f} | Giá: {price:.4f} | Giá trị: {coin_value:.2f} USDT\n"
        
        portfolio_msg += f"💰 Tổng: {total_value:.2f} USDT\n📈 Lợi nhuận ngày: {(total_value - daily_start_capital)/daily_start_capital*100:.2f}%"
        await send_telegram(portfolio_msg)
        await ex.close()
    except Exception as e:
        await send_telegram(f"❌ Lỗi log_portfolio: {str(e)}")

async def runner():
    keep_alive()
    await send_telegram("🤖 Bot giao dịch đã khởi động! Chạy 24/7")
    schedule.every(15).seconds.do(lambda: asyncio.ensure_future(analyze_and_trade()))
    schedule.every(15).minutes.do(lambda: asyncio.ensure_future(log_portfolio()))
    while True:
        schedule.run_pending()
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(runner())
