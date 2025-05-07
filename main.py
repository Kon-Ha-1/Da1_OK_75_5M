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
active_orders = {}
last_signal_check = {}
btc_dump_until = None
trade_history = []

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
    except Exception:
        return None

def compute_rsi(series, period):
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def is_strong_uptrend(df): return df.iloc[-1]['ema_fast'] > df.iloc[-1]['ema_slow']
def is_market_safe(df): return (df.iloc[-1]['close'] - df.iloc[-2]['close']) / df.iloc[-2]['close'] > -0.05
def is_volatile_enough(df, threshold=0.002): return df.iloc[-1]['atr'] / df.iloc[-1]['close'] > threshold

def should_increase(df_5m, df_1h):
    last_5m, prev_5m = df_5m.iloc[-1], df_5m.iloc[-2]
    last_1h = df_1h.iloc[-1]
    return (
        last_5m['ema_fast'] > last_5m['ema_slow'] and
        last_1h['ema_fast'] > last_1h['ema_slow'] and
        last_5m['macd'] > last_5m['signal'] and
        prev_5m['macd'] <= prev_5m['signal']
    ) or (
        last_5m['close'] > prev_5m['resistance'] and
        last_5m['volume'] > last_5m['volume_ma']
    )

async def is_btc_crashing(exchange):
    global btc_dump_until
    now = datetime.now(timezone(timedelta(hours=7)))
    if btc_dump_until and now < btc_dump_until:
        return True
    df_btc = await fetch_ohlcv(exchange, "BTC/USDT", "1h", limit=2)
    if df_btc is None: return False
    drop = (df_btc.iloc[-1]['close'] - df_btc.iloc[-2]['close']) / df_btc.iloc[-2]['close']
    if drop <= -0.02:
        btc_dump_until = now + timedelta(hours=1)
        await send_telegram(f"🚨 BTC dump mạnh ({drop*100:.2f}%), tạm dừng giao dịch đến {btc_dump_until.strftime('%H:%M')}")
        return True
    return False

async def trade_all_coins(exchange):
    if await is_btc_crashing(exchange): return
    for symbol in SYMBOLS:
        await trade_coin(exchange, symbol)

async def trade_coin(exchange, symbol):
    global active_orders, last_signal_check, trade_history
    try:
        now = datetime.now(timezone(timedelta(hours=7)))
        if symbol in last_signal_check and (now - last_signal_check[symbol]).total_seconds() < 10:
            return
        df_5m = await fetch_ohlcv(exchange, symbol, '5m', 100)
        df_1h = await fetch_ohlcv(exchange, symbol, '1h', 100)
        if not all([df_5m is not None, df_1h is not None]): return
        can_trade = is_strong_uptrend(df_5m) and is_market_safe(df_1h) and is_volatile_enough(df_5m) and should_increase(df_5m, df_1h)
        if symbol not in active_orders and can_trade and len(active_orders) < 3:
            balance = await exchange.fetch_balance()
            usdt = float(balance['total'].get('USDT', 0.0))
            recent_win = [p for p in trade_history[-2:] if p >= 0.5]
            usdt_per_trade = usdt * (0.3 if len(recent_win) == 2 else 0.2)
            if usdt_per_trade < 1: return
            ticker = await exchange.fetch_ticker(symbol)
            current_price = ticker['last']
            amount = usdt_per_trade / current_price
            await exchange.create_market_buy_order(symbol, amount)
            coin = symbol.split('/')[0]
            actual_amount = float((await exchange.fetch_balance())['total'].get(coin, 0.0))
            await send_telegram(f"🟢 Mua {symbol}: {actual_amount:.4f} | Giá: {current_price:.4f} | Tổng: {usdt_per_trade:.2f} USDT")
            active_orders[symbol] = {'buy_price': current_price, 'amount': actual_amount, 'usdt': usdt_per_trade, 'peak_price': current_price}
            last_signal_check[symbol] = now
        elif symbol in active_orders:
            o = active_orders[symbol]
            ticker = await exchange.fetch_ticker(symbol)
            current_price = ticker['last']
            buy_price, amount = o['buy_price'], o['amount']
            if current_price > o['peak_price']: o['peak_price'] = current_price
            trailing = ((o['peak_price'] - current_price) / o['peak_price']) * 100
            price_change = ((current_price - buy_price) / buy_price) * 100
            stop_loss = ((buy_price - current_price) / buy_price) * 100
            sell_reason = None
            if price_change >= 0.5 and trailing >= 0.2: sell_reason = "Trailing stop"
            elif price_change >= 1.5: sell_reason = "Lợi nhuận cao"
            elif stop_loss >= 0.3: sell_reason = "Cắt lỗ"
            if sell_reason:
                await send_telegram(f"📤 Bán {symbol}: {amount:.4f} coin | Giá: {current_price:.4f} | Lý do: {sell_reason}")
                await exchange.create_market_sell_order(symbol, amount)
                profit_usdt = (current_price - buy_price) * amount
                await send_telegram(f"🔴 Lợi nhuận: {price_change:.2f}% ({profit_usdt:.2f} USDT)")
                trade_history.append(price_change)
                if len(trade_history) > 10: trade_history.pop(0)
                del active_orders[symbol]
                last_signal_check[symbol] = now
    except Exception as e:
        await send_telegram(f"❌ Lỗi giao dịch {symbol}: {str(e)}")
        last_signal_check[symbol] = now

async def fetch_usdt_usd_rate(exchange):
    try:
        ticker = await exchange.fetch_ticker("USDT/USD")
        return float(ticker['last'])
    except Exception:
        return 1.0

async def log_assets(exchange):
    global daily_start_capital_usd, last_day, last_total_value_usd
    try:
        balance = await exchange.fetch_balance()
        total_value_usdt = float(balance['total'].get('USDT', 0.0))
        usdt_usd_rate = await fetch_usdt_usd_rate(exchange)
        coins = {}
        for currency in balance['total']:
            if currency != 'USDT':
                coin_balance = float(balance['total'].get(currency, 0.0))
                if coin_balance > 0:
                    try:
                        ticker = await exchange.fetch_ticker(f"{currency}/USDT")
                        price = ticker['last']
                        value = coin_balance * price
                        total_value_usdt += value
                        coins[currency] = {'balance': coin_balance, 'price': price, 'value_usd': value * usdt_usd_rate}
                    except: pass
        total_value_usd = total_value_usdt * usdt_usd_rate
        now = datetime.now(timezone(timedelta(hours=7)))
        today = now.date()
        if last_day is None or (today != last_day and now.hour >= 21):
            daily_start_capital_usd = total_value_usd
            last_day = today
        profit_percent = ((total_value_usd - daily_start_capital_usd) / daily_start_capital_usd * 100) if daily_start_capital_usd > 0 else 0
        if last_total_value_usd is None or abs(total_value_usd - last_total_value_usd) > 0.01:
            msg = f"💰 Tổng tài sản: {total_value_usd:.2f} USD\n💵 USDT: {balance['total'].get('USDT', 0.0):.2f}\n"
            for c, d in coins.items():
                if d['value_usd'] > 0.1:
                    msg += f"🪙 {c}: {d['balance']:.4f} | Giá: {d['price']:.4f} | Giá trị: {d['value_usd']:.2f} USD\n"
            msg += f"📈 Lợi nhuận hôm nay: {profit_percent:.2f}%"
            await send_telegram(msg)
            if now.hour == 21 and now.minute < 10:
                await send_telegram(f"📊 Báo cáo cuối ngày\n📅 Ngày: {today}\n💼 Vốn đầu ngày: {daily_start_capital_usd:.2f} USD\n💰 Tổng hiện tại: {total_value_usd:.2f} USD\n📈 Lợi nhuận hôm nay: {profit_percent:.2f}%")
            last_total_value_usd = total_value_usd
    except Exception as e:
        await send_telegram(f"❌ Lỗi log tài sản: {str(e)}")

async def runner():
    keep_alive()
    exchange = create_exchange()
    await send_telegram("🤖 Bot giao dịch tự động đã khởi động! Mục tiêu: 2%/ngày")
    schedule.every(10).seconds.do(lambda: asyncio.ensure_future(trade_all_coins(exchange)))
    schedule.every(10).minutes.do(lambda: asyncio.ensure_future(log_assets(exchange)))
    while True:
        schedule.run_pending()
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(runner())
