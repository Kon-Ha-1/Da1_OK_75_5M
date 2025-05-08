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

SYMBOLS = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT"]
bot = Bot(token=TELEGRAM_TOKEN)
nest_asyncio.apply()

last_total_value_usd = None
daily_start_capital_usd = 0.0
last_day = None
active_order = None
lowest_prices = {}
safe_coins = {}

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
        
        return df
    except Exception as e:
        return None

async def get_lowest_price_7d(exchange, symbol):
    try:
        df = await fetch_ohlcv(exchange, symbol, '1d', limit=7)
        if df is None or len(df) < 7:
            return None
        return df['low'].min()
    except Exception as e:
        return None

async def check_recent_dump(exchange, symbol):
    try:
        df = await fetch_ohlcv(exchange, symbol, '1d', limit=3)
        if df is None or len(df) < 3:
            return False
        for i in range(-2, 0):
            price_change = (df['close'].iloc[i] - df['close'].iloc[i-1]) / df['close'].iloc[i-1] * 100
            if price_change <= -10:
                return False
        return True
    except Exception as e:
        return False

def is_near_lowest_price(current_price, lowest_price, threshold=0.05):  # Nới rộng ngưỡng từ 0.02 lên 0.05
    return current_price <= lowest_price * (1 + threshold)

def is_at_peak(df_5m):
    last_candle = df_5m.iloc[-1]
    prev_candle = df_5m.iloc[-2]
    return (
        last_candle['rsi14'] > 70 or
        (last_candle['macd'] < last_candle['signal'] and prev_candle['macd'] >= prev_candle['signal']) or
        last_candle['close'] >= last_candle['resistance']
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

async def trade_coin(exchange, symbol):
    global active_order, lowest_prices, safe_coins
    try:
        if symbol not in lowest_prices:
            lowest_price = await get_lowest_price_7d(exchange, symbol)
            if lowest_price is None:
                return
            lowest_prices[symbol] = lowest_price

        if symbol not in safe_coins:
            is_safe = await check_recent_dump(exchange, symbol)
            safe_coins[symbol] = is_safe
            if not is_safe:
                return

        if not safe_coins[symbol]:
            return

        lowest_price = lowest_prices[symbol]

        if active_order is None:
            ticker = await exchange.fetch_ticker(symbol)
            current_price = ticker['last']
            
            if is_near_lowest_price(current_price, lowest_price):
                balance = await exchange.fetch_balance()
                usdt = float(balance['total'].get('USDT', 0.0))
                if usdt < 1.0:
                    return

                amount = usdt / current_price
                order = await exchange.create_market_buy_order(symbol, amount)
                coin = symbol.split('/')[0]
                balance = await exchange.fetch_balance()
                actual_amount = float(balance['total'].get(coin, 0.0))
                
                await send_telegram(f"🟢 Mua {symbol}: {actual_amount:.4f} coin | Giá: {current_price:.4f} | Tổng: {usdt:.2f} USDT")

                active_order = {
                    'symbol': symbol,
                    'buy_price': current_price,
                    'amount': actual_amount,
                    'usdt': usdt
                }

        elif active_order['symbol'] == symbol:
            df_5m = await fetch_ohlcv(exchange, symbol, '5m', limit=100)
            if df_5m is None:
                return

            ticker = await exchange.fetch_ticker(symbol)
            current_price = ticker['last']
            profit_percent = ((current_price - active_order['buy_price']) / active_order['buy_price']) * 100

            coin = symbol.split('/')[0]
            balance = await exchange.fetch_balance()
            coin_balance = float(balance['total'].get(coin, 0.0))
            amount = active_order['amount']

            TOLERANCE = 0.001
            if coin_balance < amount:
                diff = amount - coin_balance
                diff_percent = (diff / amount) * 100
                if diff_percent <= TOLERANCE:
                    amount = coin_balance
                else:
                    amount = coin_balance

            should_sell = (
                profit_percent >= 2 or
                profit_percent <= -10 or
                is_at_peak(df_5m)
            )

            if should_sell:
                order = await exchange.create_market_sell_order(symbol, amount)
                profit_usdt = (current_price - active_order['buy_price']) * amount
                await send_telegram(
                    f"🔴 Bán {symbol}: {amount:.4f} coin | Giá: {current_price:.4f} | "
                    f"Lợi nhuận: {profit_percent:.2f}% ({profit_usdt:.2f} USDT)"
                )
                active_order = None

    except Exception as e:
        error_msg = str(e)
        if "51008" in error_msg and active_order:
            balance = await exchange.fetch_balance()
            coin = symbol.split('/')[0]
            coin_balance = float(balance['total'].get(coin, 0.0))
            order = await exchange.create_market_sell_order(symbol, coin_balance)
            await send_telegram(
                f"⚠️ Lỗi 51008 khi bán {symbol}: Số dư không đủ. Đã bán {coin_balance:.4f} coin."
            )
            active_order = None
        else:
            await send_telegram(f"❌ Lỗi giao dịch {symbol}: {error_msg}")

async def trade_all_coins(exchange):
    global active_order
    for symbol in SYMBOLS:
        if active_order is None or active_order['symbol'] == symbol:
            await trade_coin(exchange, symbol)

async def runner():
    keep_alive()
    exchange = create_exchange()
    await send_telegram("🤖 Bot giao dịch tự động đã khởi động! Chiến lược: Mua giá đáy, bán 2-5% hoặc đỉnh")
    
    schedule.every(1).minutes.do(lambda: asyncio.ensure_future(trade_all_coins(exchange)))
    schedule.every(10).minutes.do(lambda: asyncio.ensure_future(log_assets(exchange)))
    while True:
        schedule.run_pending()
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(runner())
