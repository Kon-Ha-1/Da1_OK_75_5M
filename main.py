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
DAILY_PROFIT_TARGET = 0.03  # Mục tiêu 3% mỗi ngày
MAX_DAILY_LOSS = 0.05  # Dừng bot nếu lỗ >5% trong ngày

bot = Bot(token=TELEGRAM_TOKEN)
nest_asyncio.apply()

can_trade_status = {symbol: None for symbol in SYMBOLS}  # Lưu trạng thái dự đoán
last_total_value = None  # Lưu tổng giá trị tài sản để tránh báo lặp
capital = 0.0  # Sẽ được khởi tạo từ tổng tài sản
daily_profit = 0.0
daily_start_capital = 0.0  # Sẽ được khởi tạo từ tổng tài sản
last_day = None  # Sẽ được khởi tạo khi bot chạy
is_first_run = True  # Đánh dấu lần chạy đầu tiên
coin_values_at_start = {}  # Lưu giá trị coin tại 21:00 để tính lợi nhuận

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

async def initialize_capital(exchange):
    global capital, daily_start_capital, last_day, is_first_run, coin_values_at_start, last_total_value
    try:
        balance = await exchange.fetch_balance()
        usdt = float(balance.get('USDT', {}).get('total', 0.0))
        total_value = usdt
        coin_values_at_start = {}

        # Kiểm tra cấu trúc dữ liệu từ API
        info = balance.get('info', {})
        data = info.get('data', [{}])
        if not data or not isinstance(data, list):
            await send_telegram("⚠️ API trả về dữ liệu không hợp lệ: 'data' rỗng hoặc không phải list.")
            return False
        
        details = data[0].get('details', {})
        if isinstance(details, dict):
            for currency, info in details.items():
                coin_balance = float(info.get('ccyBalance', 0))
                if coin_balance > 0 and currency != 'USDT':
                    try:
                        symbol = f"{currency}/USDT"
                        ticker = await exchange.fetch_ticker(symbol)
                        price = ticker['last']
                        coin_value = coin_balance * price
                        total_value += coin_value
                        coin_values_at_start[currency] = {'balance': coin_balance, 'value': coin_value}
                    except Exception:
                        continue
        else:
            await send_telegram("⚠️ Dữ liệu số dư không hợp lệ: 'details' không phải dictionary.")

        capital = total_value
        daily_start_capital = total_value
        last_day = datetime.now(timezone(timedelta(hours=7))).date()
        is_first_run = False
        last_total_value = total_value  # Khởi tạo giá trị tổng tài sản ban đầu
        
        if total_value == 0:
            await send_telegram("⚠️ Tài khoản không có số dư (0 USDT). Bot vẫn chạy để dự đoán giá.")
            return True
        
        portfolio_msg = f"🚀 Bot dự đoán khởi động - Vốn ban đầu: {capital:.2f} USDT\n💵 USDT: {usdt:.2f}\n"
        for currency, data in coin_values_at_start.items():
            portfolio_msg += f"🪙 {currency}: {data['balance']:.4f} | Giá trị: {data['value']:.2f} USDT\n"
        portfolio_msg += f"🎯 Mục tiêu lợi nhuận: 3% mỗi ngày (tính từ 21:00 VN)"
        await send_telegram(portfolio_msg)
        return True
    except Exception as e:
        await send_telegram(f"❌ Lỗi khởi tạo vốn: {str(e)}")
        return False

async def fetch_ohlcv(exchange, symbol, timeframe='5m'):
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
        await send_telegram(f"❌ [OHLCV Error] {symbol}: {e}")
        return None

async def fetch_ohlcv_15m(exchange, symbol):
    try:
        data = await exchange.fetch_ohlcv(symbol, timeframe='15m', limit=100)
        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert('Asia/Ho_Chi_Minh')
        df['ema_fast'] = df['close'].ewm(span=5, adjust=False).mean()
        df['ema_slow'] = df['close'].ewm(span=12, adjust=False).mean()
        return df
    except Exception as e:
        await send_telegram(f"❌ [OHLCV 15m Error] {symbol}: {e}")
        return None

async def fetch_ohlcv_1h(exchange, symbol):
    try:
        data = await exchange.fetch_ohlcv(symbol, timeframe='1h', limit=2)
        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert('Asia/Ho_Chi_Minh')
        return df
    except Exception as e:
        await send_telegram(f"❌ [OHLCV 1h Error] {symbol}: {e}")
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
    return last_candle['ema_fast'] > last_candle['ema_slow']

def is_strong_downtrend(df_15m):
    last_candle = df_15m.iloc[-1]
    return last_candle['ema_fast'] < last_candle['ema_slow']

def is_market_safe(df_1h):
    last_candle = df_1h.iloc[-1]
    prev_candle = df_1h.iloc[-2]
    price_change = (last_candle['close'] - prev_candle['close']) / prev_candle['close']
    return price_change > -0.05

def is_volatile_enough(df_5m):
    last_candle = df_5m.iloc[-1]
    atr_percent = last_candle['atr'] / last_candle['close']
    return atr_percent > 0.004

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

async def update_capital(exchange):
    global capital, daily_profit, daily_start_capital, last_day, is_first_run, coin_values_at_start
    try:
        balance = await exchange.fetch_balance()
        usdt = float(balance.get('USDT', {}).get('total', 0.0))
        total_value = usdt
        new_coin_values = {}

        info = balance.get('info', {})
        data = info.get('data', [{}])
        if not data or not isinstance(data, list):
            await send_telegram("⚠️ API trả về dữ liệu không hợp lệ: 'data' rỗng hoặc không phải list.")
            return True
        
        details = data[0].get('details', {})
        if isinstance(details, dict):
            for currency, info in details.items():
                coin_balance = float(info.get('ccyBalance', 0))
                if coin_balance > 0 and currency != 'USDT':
                    try:
                        symbol = f"{currency}/USDT"
                        ticker = await exchange.fetch_ticker(symbol)
                        price = ticker['last']
                        coin_value = coin_balance * price
                        total_value += coin_value
                        new_coin_values[currency] = {'balance': coin_balance, 'value': coin_value}
                    except Exception:
                        continue
        else:
            await send_telegram("⚠️ Dữ liệu số dư không hợp lệ: 'details' không phải dictionary.")
        
        now = datetime.now(timezone(timedelta(hours=7)))
        today = now.date()
        current_hour = now.hour
        
        if is_first_run:
            return True
        
        if today != last_day and current_hour >= 21:
            daily_profit = (total_value - daily_start_capital) / daily_start_capital if daily_start_capital > 0 else 0
            if daily_profit < DAILY_PROFIT_TARGET:
                await send_telegram(
                    f"⚠️ Lợi nhuận ngày {last_day} không đạt 3%: {daily_profit*100:.2f}%\n"
                    f"Bot tạm dừng dự đoán đến 21:00 ngày mai."
                )
                return False
            
            capital = total_value
            daily_start_capital = total_value
            coin_values_at_start = new_coin_values
            last_day = today
            portfolio_msg = f"📈 Ngày mới (21:00) - Cập nhật vốn: {capital:.2f} USDT\n💵 USDT: {usdt:.2f}\n"
            for currency, data in coin_values_at_start.items():
                portfolio_msg += f"🪙 {currency}: {data['balance']:.4f} | Giá trị: {data['value']:.2f} USDT\n"
            portfolio_msg += f"🎯 Lợi nhuận ngày trước: {daily_profit*100:.2f}%"
            await send_telegram(portfolio_msg)
        
        if daily_start_capital > 0 and (daily_start_capital - total_value) / daily_start_capital > MAX_DAILY_LOSS:
            await send_telegram(
                f"🛑 Lỗ vượt 5% trong ngày: {total_value:.2f} USDT\n"
                f"Bot tạm dừng dự đoán đến 21:00 ngày mai."
            )
            return False
        
        return True
    except Exception as e:
        await send_telegram(f"❌ Lỗi cập nhật vốn: {str(e)}")
        return True

async def analyze_and_predict(exchange):
    global can_trade_status, last_total_value
    if not await update_capital(exchange):
        return

    # Tính tổng giá trị tài sản (bao gồm USDT và tất cả coin)
    total_value = 0.0
    try:
        balance = await exchange.fetch_balance()
        usdt = float(balance.get('USDT', {}).get('total', 0.0))
        total_value = usdt

        info = balance.get('info', {})
        data = info.get('data', [{}])
        if not data or not isinstance(data, list):
            await send_telegram("⚠️ API trả về dữ liệu không hợp lệ: 'data' rỗng hoặc không phải list.")
            return
        
        details = data[0].get('details', {})
        if isinstance(details, dict):
            for currency, info in details.items():
                coin_balance = float(info.get('ccyBalance', 0))
                if coin_balance > 0 and currency != 'USDT':
                    try:
                        symbol = f"{currency}/USDT"
                        ticker = await exchange.fetch_ticker(symbol)
                        price = ticker['last']
                        coin_value = coin_balance * price
                        total_value += coin_value
                    except Exception:
                        continue
        else:
            await send_telegram("⚠️ Dữ liệu số dư không hợp lệ: 'details' không phải dictionary.")
        
        # Kiểm tra và gửi thông báo nếu tổng tài sản thay đổi > $0.01
        if last_total_value is None or abs(total_value - last_total_value) > 0.01:
            await send_telegram(f"💰 Tổng tài sản: {total_value:.2f} USDT")
            last_total_value = total_value
    except Exception as e:
        await send_telegram(f"❌ Lỗi kiểm tra tổng tài sản: {str(e)}")
        return

    for symbol in SYMBOLS:
        df_5m = await fetch_ohlcv(exchange, symbol, TIMEFRAME)
        df_15m = await fetch_ohlcv_15m(exchange, symbol)
        df_1h = await fetch_ohlcv_1h(exchange, symbol)
        if df_5m is None or df_15m is None or df_1h is None:
            continue

        price = df_5m['close'].iloc[-1]
        atr_percent = (df_5m['atr'].iloc[-1] / price) * 100

        reasons = []
        can_predict = True
        trend = None
        predicted_change = 0.0

        if not is_strong_uptrend(df_15m) and not is_strong_downtrend(df_15m):
            reasons.append("Không có xu hướng rõ ràng (EMA5 ≈ EMA12)")
            can_predict = False
        elif not is_market_safe(df_1h):
            reasons.append("Thị trường không an toàn (giá giảm >5%)")
            can_predict = False
        elif not is_volatile_enough(df_5m):
            reasons.append("Biến động thấp (ATR < 0.4%)")
            can_predict = False
        else:
            if should_increase(df_5m):
                trend = "increase"
                predicted_change = min(atr_percent, 1.0)
            elif should_decrease(df_5m):
                trend = "decrease"
                predicted_change = min(atr_percent, 0.5)
            else:
                reasons.append("Không thỏa tín hiệu tăng/giảm (EMA/MACD/breakout)")
                can_predict = False

        if can_predict and can_trade_status[symbol] != True:
            await send_telegram(f"✅ {symbol}: Có thể dự đoán, đang phân tích xu hướng")
            can_trade_status[symbol] = True
        elif not can_predict and can_trade_status[symbol] != False:
            await send_telegram(f"⏳ {symbol}: Không dự đoán. Lý do: {', '.join(reasons)}")
            can_trade_status[symbol] = False

        if can_predict and trend:
            if trend == "increase":
                await send_telegram(
                    f"📈 {symbol}: Dự đoán TĂNG {predicted_change:.2f}% "
                    f"(dựa trên ATR: {atr_percent:.2f}%)"
                )
            else:
                await send_telegram(
                    f"📉 {symbol}: Dự đoán GIẢM {predicted_change:.2f}% "
                    f"(dựa trên ATR: {atr_percent:.2f}%)"
                )
            can_trade_status[symbol] = None

async def log_portfolio(exchange):
    global coin_values_at_start
    try:
        balance = await exchange.fetch_balance()
        usdt = float(balance.get('USDT', {}).get('total', 0.0))
        total_value = usdt
        portfolio_msg = f"📊 Báo cáo tài sản\n💵 USDT: {usdt:.2f}\n"
        
        info = balance.get('info', {})
        data = info.get('data', [{}])
        if not data or not isinstance(data, list):
            await send_telegram("⚠️ API trả về dữ liệu không hợp lệ: 'data' rỗng hoặc không phải list.")
            return
        
        details = data[0].get('details', {})
        if isinstance(details, dict):
            for currency, info in details.items():
                coin_balance = float(info.get('ccyBalance', 0))
                if coin_balance > 0 and currency != 'USDT':
                    try:
                        symbol = f"{currency}/USDT"
                        ticker = await exchange.fetch_ticker(symbol)
                        price = ticker['last']
                        coin_value = coin_balance * price
                        total_value += coin_value
                        
                        start_data = coin_values_at_start.get(currency, {'value': coin_value, 'balance': coin_balance})
                        start_value = start_data['value']
                        start_balance = start_data['balance']
                        if start_balance > 0 and start_value > 0:
                            profit_percent = ((coin_value - start_value) / start_value) * 100
                        else:
                            profit_percent = 0.0
                        
                        portfolio_msg += (
                            f"🪙 {currency}: {coin_balance:.4f} | Giá: {price:.4f} | "
                            f"Giá trị: {coin_value:.2f} USDT | Lợi nhuận: {profit_percent:.2f}%\n"
                        )
                    except Exception:
                        continue
        else:
            await send_telegram("⚠️ Dữ liệu số dư không hợp lệ: 'details' không phải dictionary.")
        
        daily_profit_percent = ((total_value - daily_start_capital) / daily_start_capital) * 100 if daily_start_capital > 0 else 0
        portfolio_msg += f"💰 Tổng: {total_value:.2f} USDT\n📈 Lợi nhuận ngày: {daily_profit_percent:.2f}%"
        await send_telegram(portfolio_msg)
    except Exception as e:
        await send_telegram(f"❌ Lỗi log_portfolio: {str(e)}")

async def runner():
    keep_alive()
    exchange = create_exchange()
    try:
        if not await initialize_capital(exchange):
            await send_telegram("🛑 Không thể khởi tạo vốn. Bot vẫn chạy để dự đoán giá.")
        
        await send_telegram("🤖 Bot dự đoán đã khởi động! Chạy 24/7")
        schedule.every(15).seconds.do(lambda: asyncio.ensure_future(analyze_and_predict(exchange)))
        schedule.every(15).minutes.do(lambda: asyncio.ensure_future(log_portfolio(exchange)))
        while True:
            schedule.run_pending()
            await asyncio.sleep(1)
    finally:
        try:
            await exchange.close()
            await send_telegram("🔌 Đã đóng kết nối tới OKX")
        except Exception as e:
            await send_telegram(f"❌ Lỗi khi đóng kết nối OKX: {str(e)}")

if __name__ == "__main__":
    asyncio.run(runner())
