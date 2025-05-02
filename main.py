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

SYMBOLS = ["DOGE/USDT", "BTC/USDT", "ETH/USDT", "XRP/USDT", "ARB/USDT", 
           "SOL/USDT", "TRUMP/USDT", "BNB/USDT", "TRX/USDT", "MAGIC/USDT",
           "PEPE/USDT", "SHIB/USDT"]
bot = Bot(token=TELEGRAM_TOKEN)
nest_asyncio.apply()

last_total_value_usd = None
daily_start_capital_usd = 0.0
last_day = None
active_orders = {}
last_signal_check = {}

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
        return 1.0

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

def is_market_safe(df):
    last_candle = df.iloc[-1]
    prev_candle = df.iloc[-2]
    price_change = (last_candle['close'] - prev_candle['close']) / prev_candle['close']
    return price_change > -0.05

def is_volatile_enough(df, threshold=0.002):
    last_candle = df.iloc[-1]
    atr_percent = last_candle['atr'] / last_candle['close']
    return atr_percent > threshold

def should_increase(df_5m):
    last_candle = df_5m.iloc[-1]
    prev_candle = df_5m.iloc[-2]
    return (
        last_candle['ema_fast'] > last_candle['ema_slow'] and
        30 < last_candle['rsi14'] < 70 and
        last_candle['macd'] > last_candle['signal'] and
        prev_candle['macd'] <= prev_candle['signal']
    ) or (
        last_candle['close'] > prev_candle['resistance'] and
        last_candle['volume'] > last_candle['volume_ma']
    )

def should_decrease(df):
    last_candle = df.iloc[-1]
    prev_candle = df.iloc[-2]
    return (
        last_candle['ema_fast'] < last_candle['ema_slow'] and
        last_candle['rsi14'] > 30 and
        last_candle['macd'] < last_candle['signal'] and
        prev_candle['macd'] >= prev_candle['signal']
    ) or (
        last_candle['close'] < prev_candle['low'].rolling(20).min() and
        last_candle['volume'] > last_candle['volume_ma']
    )

async def log_assets(exchange):
    global daily_start_capital_usd, last_day, last_total_value_usd
    try:
        balance = await exchange.fetch_balance()
        total_value_usdt = 0.0
        usdt_usd_rate = await fetch_usdt_usd_rate(exchange)

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

        now = datetime.now(timezone(timedelta(hours=7)))
        today = now.date()
        if last_day is None or (today != last_day and now.hour >= 21):
            daily_start_capital_usd = total_value_usd
            last_day = today

        profit_percent = ((total_value_usd - daily_start_capital_usd) / daily_start_capital_usd * 100) if daily_start_capital_usd > 0 else 0

        if last_total_value_usd is None or abs(total_value_usd - last_total_value_usd) > 0.01:
            msg = f"💰 Tổng tài sản: {total_value_usd:.2f} USD\n💵 USDT: {usdt:.2f}\n"
            for currency, data in coins.items():
                if data['value_usd'] > 0.1:
                    msg += f"🪙 {currency}: {data['balance']:.4f} | Giá: {data['price']:.4f} | Giá trị: {data['value_usd']:.2f} USD\n"
            msg += f"📈 Lợi nhuận hôm nay: {profit_percent:.2f}%"
            await send_telegram(msg)
            last_total_value_usd = total_value_usd
    except Exception as e:
        await send_telegram(f"❌ Lỗi log tài sản: {str(e)}")

async def sync_active_orders(exchange):
    global active_orders
    try:
        balance = await exchange.fetch_balance()
        to_remove = []
        for symbol in active_orders:
            coin = symbol.split('/')[0]
            coin_balance = float(balance['total'].get(coin, 0.0))
            required_amount = active_orders[symbol]['amount']
            
            if coin_balance < required_amount:
                await send_telegram(
                    f"⚠️ Đồng bộ active_orders: Xóa lệnh {symbol}. "
                    f"Số dư {coin}: {coin_balance:.4f}, nhưng cần {required_amount:.4f} để bán."
                )
                to_remove.append(symbol)
        
        for symbol in to_remove:
            del active_orders[symbol]
    except Exception as e:
        await send_telegram(f"❌ Lỗi đồng bộ active_orders: {str(e)}")

async def trade_coin(exchange, symbol):
    global active_orders, last_signal_check
    try:
        now = datetime.now(timezone(timedelta(hours=7)))
        if symbol in last_signal_check:
            last_check = last_signal_check[symbol]
            if (now - last_check).total_seconds() < 10:
                return

        df_5m = await fetch_ohlcv(exchange, symbol, '5m', limit=100)
        df_1h = await fetch_ohlcv(exchange, symbol, '1h', limit=100)
        if df_5m is None or df_1h is None:
            return

        reasons = []
        can_trade = True

        if not is_strong_uptrend(df_5m):
            reasons.append("5m: Không có xu hướng tăng (EMA5 < EMA12)")
            can_trade = False
        if not is_market_safe(df_1h):
            reasons.append("1h: Thị trường không an toàn (giá giảm >5%)")
            can_trade = False
        if not is_volatile_enough(df_5m, 0.002):
            reasons.append("5m: Biến động thấp (ATR < 0.2%)")
            can_trade = False
        if not should_increase(df_5m):
            reasons.append("5m: Không thỏa mãn tín hiệu tăng (EMA, RSI, MACD, Breakout)")
            can_trade = False

        if symbol not in active_orders and can_trade:
            balance = await exchange.fetch_balance()
            usdt = float(balance['total'].get('USDT', 0.0))
            if usdt < 1.0:
                await send_telegram(f"⚠️ Không đủ USDT để giao dịch {symbol}")
                last_signal_check[symbol] = now
                return

            usdt_per_trade = usdt * 0.1
            if usdt_per_trade < 1.0:
                await send_telegram(f"⚠️ USDT quá thấp để chia lệnh: {usdt_per_trade:.2f}")
                last_signal_check[symbol] = now
                return

            ticker = await exchange.fetch_ticker(symbol)
            current_price = ticker['last']
            amount = usdt_per_trade / current_price

            order = await exchange.create_market_buy_order(symbol, amount)
            coin = symbol.split('/')[0]
            balance = await exchange.fetch_balance()
            actual_amount = float(balance['total'].get(coin, 0.0))
            
            await send_telegram(f"🟢 Mua {symbol}: {actual_amount:.4f} coin | Giá: {current_price:.4f} | Tổng: {usdt_per_trade:.2f} USDT")

            active_orders[symbol] = {
                'buy_price': current_price,
                'amount': actual_amount,
                'usdt': usdt_per_trade
            }
            last_signal_check[symbol] = now

        elif not can_trade:
            await send_telegram(f"⏳ {symbol}: Không mở lệnh. Lý do: {', '.join(reasons)}")
            last_signal_check[symbol] = now

        if symbol in active_orders:
            order_info = active_orders[symbol]
            buy_price = order_info['buy_price']
            amount = order_info['amount']

            coin = symbol.split('/')[0]
            balance = await exchange.fetch_balance()
            coin_balance = float(balance['total'].get(coin, 0.0))

            TOLERANCE = 0.001
            if coin_balance < amount:
                diff = amount - coin_balance
                diff_percent = (diff / amount) * 100
                if diff_percent <= TOLERANCE:
                    await send_telegram(
                        f"⚠️ Điều chỉnh bán {symbol}: Số dư {coin}: {coin_balance:.4f}, "
                        f"cần {amount:.4f}. Chênh lệch {diff:.4f} ({diff_percent:.2f}%). Bán theo số dư."
                    )
                    amount = coin_balance
                else:
                    await send_telegram(
                        f"⚠️ Điều chỉnh bán {symbol}: Số dư {coin}: {coin_balance:.4f}, "
                        f"nhưng cần {amount:.4f}. Bán với số dư hiện có."
                    )
                    amount = coin_balance

            ticker = await exchange.fetch_ticker(symbol)
            current_price = ticker['last']
            profit_percent = ((current_price - buy_price) / buy_price) * 100
            price_change = ((current_price - buy_price) / buy_price) * 100

            if price_change >= 0.3 or price_change <= -0.2:  # Bán khi tăng 0.3% hoặc giảm 0.2%
                await send_telegram(
                    f"📤 Chuẩn bị bán {symbol}: {amount:.4f} coin | "
                    f"Giá mua: {buy_price:.4f} | Giá hiện tại: {current_price:.4f}"
                )
                order = await exchange.create_market_sell_order(symbol, amount)
                profit_usdt = (current_price - buy_price) * amount
                await send_telegram(
                    f"🔴 Bán {symbol}: {amount:.4f} coin | Giá: {current_price:.4f} | "
                    f"Lợi nhuận: {profit_percent:.2f}% ({profit_usdt:.2f} USDT)"
                )
                del active_orders[symbol]
                last_signal_check[symbol] = now

    except Exception as e:
        error_msg = str(e)
        if "51008" in error_msg:
            await send_telegram(
                f"⚠️ Lỗi 51008 khi bán {symbol}: Số dư {coin} không đủ. Bán với số dư hiện có."
            )
            if symbol in active_orders:
                balance = await exchange.fetch_balance()
                coin_balance = float(balance['total'].get(coin, 0.0))
                order = await exchange.create_market_sell_order(symbol, coin_balance)
                del active_orders[symbol]
        else:
            await send_telegram(f"❌ Lỗi giao dịch {symbol}: {error_msg}")
        last_signal_check[symbol] = now

async def trade_all_coins(exchange):
    for symbol in SYMBOLS:
        await trade_coin(exchange, symbol)

async def runner():
    keep_alive()
    exchange = create_exchange()
    await send_telegram("🤖 Bot giao dịch tự động đã khởi động! Mục tiêu: 2%/ngày")
    
    await send_telegram("🔄 Đang đồng bộ active_orders...")
    await sync_active_orders(exchange)
    
    schedule.every(10).seconds.do(lambda: asyncio.ensure_future(trade_all_coins(exchange)))  # Kiểm tra mỗi 10 giây
    schedule.every(10).minutes.do(lambda: asyncio.ensure_future(log_assets(exchange)))
    while True:
        schedule.run_pending()
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(runner())
