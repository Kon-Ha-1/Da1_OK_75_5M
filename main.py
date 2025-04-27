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

SYMBOLS = ["DOGE/USDT"]  # Chỉ giữ DOGE
DAILY_PROFIT_TARGET = 0.03  # Mục tiêu 3% mỗi ngày
MAX_DAILY_LOSS = 0.05  # Dừng bot nếu lỗ >5% trong ngày

bot = Bot(token=TELEGRAM_TOKEN)
nest_asyncio.apply()

can_trade_status = {symbol: None for symbol in SYMBOLS}  # Lưu trạng thái dự đoán
last_total_value_usd = None  # Lưu tổng tài sản USD để tránh báo lặp
capital_usd = 0.0  # Tổng tài sản USD
daily_start_capital_usd = 0.0  # Tổng tài sản USD tại 21:00
last_day = None  # Ngày cuối cùng cập nhật
is_first_run = True  # Đánh dấu lần chạy đầu tiên
coin_values_at_start = {}  # Lưu giá trị coin tại 21:00

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

async def fetch_usdt_usd_rate(exchange):
    try:
        ticker = await exchange.fetch_ticker("USDT/USD")
        return float(ticker['last'])
    except Exception as e:
        await send_telegram(f"⚠️ Lỗi lấy tỷ giá USDT/USD: {str(e)}. Dùng tỷ giá mặc định 1:1.")
        return 1.0  # Mặc định 1:1 nếu lỗi

async def initialize_capital(exchange):
    global capital_usd, daily_start_capital_usd, last_day, is_first_run, coin_values_at_start, last_total_value_usd
    try:
        balance = await exchange.fetch_balance()
        usdt = float(balance.get('USDT', {}).get('total', 0.0))
        total_value_usdt = usdt
        coin_values_at_start = {}

        # Lấy tỷ giá USDT/USD
        usdt_usd_rate = await fetch_usdt_usd_rate(exchange)

        # Tính giá trị coin bằng USDT
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
                        total_value_usdt += coin_value
                        coin_values_at_start[currency] = {'balance': coin_balance, 'value': coin_value}
                    except Exception:
                        continue
        else:
            await send_telegram("⚠️ Dữ liệu số dư không hợp lệ: 'details' không phải dictionary.")

        # Quy đổi sang USD
        total_value_usd = total_value_usdt * usdt_usd_rate
        capital_usd = total_value_usd
        daily_start_capital_usd = total_value_usd
        last_day = datetime.now(timezone(timedelta(hours=7))).date()
        is_first_run = False
        last_total_value_usd = total_value_usd
        
        if total_value_usd == 0:
            await send_telegram("⚠️ Tài khoản không có số dư (0 USD). Bot vẫn chạy để dự đoán giá.")
            return True
        
        portfolio_msg = f"🚀 Bot dự đoán khởi động - Vốn ban đầu: {capital_usd:.2f} USD\n💵 USDT: {usdt:.2f} (Tỷ giá USDT/USD: {usdt_usd_rate:.4f})\n"
        for currency, data in coin_values_at_start.items():
            coin_value_usd = data['value'] * usdt_usd_rate
            portfolio_msg += f"🪙 {currency}: {data['balance']:.4f} | Giá trị: {coin_value_usd:.2f} USD\n"
        portfolio_msg += f"🎯 Mục tiêu lợi nhuận: 3% mỗi ngày (tính từ 21:00 VN)"
        await send_telegram(portfolio_msg)
        return True
    except Exception as e:
        await send_telegram(f"❌ Lỗi khởi tạo vốn: {str(e)}")
        return False

async def fetch_ohlcv(exchange, symbol, timeframe, limit=100):
    try:
        data = await exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
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

def is_volatile_enough(df, threshold=0.004):
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

async def update_capital(exchange):
    global capital_usd, daily_profit, daily_start_capital_usd, last_day, is_first_run, coin_values_at_start
    try:
        balance = await exchange.fetch_balance()
        usdt = float(balance.get('USDT', {}).get('total', 0.0))
        total_value_usdt = usdt
        new_coin_values = {}

        usdt_usd_rate = await fetch_usdt_usd_rate(exchange)

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
                        total_value_usdt += coin_value
                        new_coin_values[currency] = {'balance': coin_balance, 'value': coin_value}
                    except Exception:
                        continue
        else:
            await send_telegram("⚠️ Dữ liệu số dư không hợp lệ: 'details' không phải dictionary.")
        
        total_value_usd = total_value_usdt * usdt_usd_rate
        now = datetime.now(timezone(timedelta(hours=7)))
        today = now.date()
        current_hour = now.hour
        
        if is_first_run:
            return True
        
        if today != last_day and current_hour >= 21:
            daily_profit = (total_value_usd - daily_start_capital_usd) / daily_start_capital_usd if daily_start_capital_usd > 0 else 0
            if daily_profit < DAILY_PROFIT_TARGET:
                await send_telegram(
                    f"⚠️ Lợi nhuận ngày {last_day} không đạt 3%: {daily_profit*100:.2f}%\n"
                    f"Bot tạm dừng dự đoán đến 21:00 ngày mai."
                )
                return False
            
            capital_usd = total_value_usd
            daily_start_capital_usd = total_value_usd
            coin_values_at_start = new_coin_values
            last_day = today
            portfolio_msg = f"📈 Ngày mới (21:00) - Cập nhật vốn: {capital_usd:.2f} USD\n💵 USDT: {usdt:.2f} (Tỷ giá USDT/USD: {usdt_usd_rate:.4f})\n"
            for currency, data in coin_values_at_start.items():
                coin_value_usd = data['value'] * usdt_usd_rate
                portfolio_msg += f"🪙 {currency}: {data['balance']:.4f} | Giá trị: {coin_value_usd:.2f} USD\n"
            portfolio_msg += f"🎯 Lợi nhuận ngày trước: {daily_profit*100:.2f}%"
            await send_telegram(portfolio_msg)
        
        if daily_start_capital_usd > 0 and (daily_start_capital_usd - total_value_usd) / daily_start_capital_usd > MAX_DAILY_LOSS:
            await send_telegram(
                f"🛑 Lỗ vượt 5% trong ngày: {total_value_usd:.2f} USD\n"
                f"Bot tạm dừng dự đoán đến 21:00 ngày mai."
            )
            return False
        
        return True
    except Exception as e:
        await send_telegram(f"❌ Lỗi cập nhật vốn: {str(e)}")
        return True

async def analyze_and_predict(exchange):
    global can_trade_status, last_total_value_usd
    if not await update_capital(exchange):
        return

    # Tính tổng tài sản USD
    total_value_usd = 0.0
    try:
        balance = await exchange.fetch_balance()
        usdt = float(balance.get('USDT', {}).get('total', 0.0))
        total_value_usdt = usdt

        usdt_usd_rate = await fetch_usdt_usd_rate(exchange)

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
                        total_value_usdt += coin_value
                    except Exception:
                        continue
        else:
            await send_telegram("⚠️ Dữ liệu số dư không hợp lệ: 'details' không phải dictionary.")
        
        total_value_usd = total_value_usdt * usdt_usd_rate
        if last_total_value_usd is None or abs(total_value_usd - last_total_value_usd) > 0.01:
            profit_percent = ((total_value_usd - daily_start_capital_usd) / daily_start_capital_usd * 100) if daily_start_capital_usd > 0 else 0
            await send_telegram(f"💰 Tổng tài sản: {total_value_usd:.2f} USD | Lợi nhuận: {profit_percent:.2f}%")
            last_total_value_usd = total_value_usd
    except Exception as e:
        await send_telegram(f"❌ Lỗi kiểm tra tổng tài sản: {str(e)}")
        return

    # Dự đoán DOGE
    symbol = SYMBOLS[0]  # Chỉ có DOGE/USDT
    df_5m = await fetch_ohlcv(exchange, symbol, '5m', limit=100)
    df_15m = await fetch_ohlcv(exchange, symbol, '15m', limit=100)
    df_1h = await fetch_ohlcv(exchange, symbol, '1h', limit=100)
    if df_5m is None or df_15m is None or df_1h is None:
        return

    current_price = df_5m['close'].iloc[-1]
    reasons = []
    can_predict = True
    trends = {}

    # Kiểm tra điều kiện trên các khung thời gian
    if not is_strong_uptrend(df_5m) and not is_strong_downtrend(df_5m):
        reasons.append("5m: Không có xu hướng rõ ràng (EMA5 ≈ EMA12)")
        can_predict = False
    if not is_strong_uptrend(df_15m) and not is_strong_downtrend(df_15m):
        reasons.append("15m: Không có xu hướng rõ ràng (EMA5 ≈ EMA12)")
        can_predict = False
    if not is_market_safe(df_1h):
        reasons.append("1h: Thị trường không an toàn (giá giảm >5%)")
        can_predict = False
    if not is_volatile_enough(df_5m, 0.003):  # Giảm ngưỡng để tăng tần suất dự đoán
        reasons.append("5m: Biến động thấp (ATR < 0.3%)")
        can_predict = False

    # Dự đoán cho 15 phút (dựa trên 5m), 30 phút (dựa trên 15m), 1 giờ (dựa trên 1h)
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
    if can_predict and can_trade_status[symbol] != True:
        await send_telegram(f"✅ {symbol}: Có thể dự đoán, đang phân tích xu hướng")
        can_trade_status[symbol] = True
    elif not can_predict and can_trade_status[symbol] != False:
        await send_telegram(f"⏳ {symbol}: Không dự đoán. Lý do: {', '.join(reasons)}")
        can_trade_status[symbol] = False

    if can_predict and trends:
        prediction_msg = f"🔮 Dự đoán giá {symbol}:\n"
        for timeframe, (trend, change) in trends.items():
            if trend == "increase":
                prediction_msg += f"📈 {timeframe}: TĂNG {change:.2f}% (dựa trên ATR: {locals()[f'atr_{timeframe.lower()}']:.2f}%)\n"
            else:
                prediction_msg += f"📉 {timeframe}: GIẢM {change:.2f}% (dựa trên ATR: {locals()[f'atr_{timeframe.lower()}']:.2f}%)\n"
        await send_telegram(prediction_msg)
        can_trade_status[symbol] = None

async def log_portfolio(exchange):
    global coin_values_at_start
    try:
        balance = await exchange.fetch_balance()
        usdt = float(balance.get('USDT', {}).get('total', 0.0))
        total_value_usdt = usdt
        usdt_usd_rate = await fetch_usdt_usd_rate(exchange)
        
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
                        total_value_usdt += coin_value
                    except Exception:
                        continue
        else:
            await send_telegram("⚠️ Dữ liệu số dư không hợp lệ: 'details' không phải dictionary.")
        
        total_value_usd = total_value_usdt * usdt_usd_rate
        daily_profit_percent = ((total_value_usd - daily_start_capital_usd) / daily_start_capital_usd * 100) if daily_start_capital_usd > 0 else 0
        portfolio_msg = f"📊 Báo cáo tài sản\n💵 USDT: {usdt:.2f} (Tỷ giá USDT/USD: {usdt_usd_rate:.4f})\n"
        for currency, data in coin_values_at_start.items():
            try:
                symbol = f"{currency}/USDT"
                ticker = await exchange.fetch_ticker(symbol)
                price = ticker['last']
                coin_balance = float(details.get(currency, {}).get('ccyBalance', 0))
                coin_value_usdt = coin_balance * price
                coin_value_usd = coin_value_usdt * usdt_usd_rate
                start_value_usd = data['value'] * usdt_usd_rate
                profit_percent = ((coin_value_usd - start_value_usd) / start_value_usd * 100) if start_value_usd > 0 else 0
                portfolio_msg += f"🪙 {currency}: {coin_balance:.4f} | Giá: {price:.4f} | Giá trị: {coin_value_usd:.2f} USD | Lợi nhuận: {profit_percent:.2f}%\n"
            except Exception:
                continue
        portfolio_msg += f"💰 Tổng: {total_value_usd:.2f} USD\n📈 Lợi nhuận ngày: {daily_profit_percent:.2f}%"
        await send_telegram(portfolio_msg)
    except Exception as e:
        await send_telegram(f"❌ Lỗi log_portfolio: {str(e)}")

async def runner():
    keep_alive()
    exchange = create_exchange()
    try:
        if not await initialize_capital(exchange):
            await send_telegram("🛑 Không thể khởi tạo vốn. Bot vẫn chạy để dự đoán giá.")
        
        await send_telegram("🤖 Bot dự đoán DOGE đã khởi động! Chạy 24/7")
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
