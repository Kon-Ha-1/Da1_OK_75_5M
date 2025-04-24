import ccxt
import asyncio
import schedule
import nest_asyncio
import time
import json
import os
from datetime import datetime
import pandas as pd
from telegram import Bot
from keep_alive import keep_alive

# === CONFIG ===
API_KEY = "99d39d59-c05d-4e40-9f2a-3615eac315ea"
API_SECRET = "4B1D25C8F05E12717AD561584B2853E6"
PASSPHRASE = "Mmoarb2025@"
TELEGRAM_TOKEN = "7817283052:AAF2fjxxZT8LP-gblBeTbpb0N0-a0C7GLQ8"
TELEGRAM_CHAT_ID = "5850622014"

SYMBOL = "ARB/USDT"
TIMEFRAME = "5m"
NUM_ORDERS = 6
SPREAD_PERCENT = 0.5
RESERVE = 15
TP_PERCENT = 0.5
ADJUST_THRESHOLD = 20  # chênh lệch giá trị USDT vs coin quá lớn thì cân bằng

bot = Bot(token=TELEGRAM_TOKEN)
nest_asyncio.apply()

last_total_balance = None
last_reset_price = None
FILLED_FILE = "filled_orders.json"

# === Indicator helpers ===
def get_ohlcv(exchange):
    try:
        data = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=100)
        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['rsi'] = compute_rsi(df['close'], 14)
        df['ema_fast'] = df['close'].ewm(span=9, adjust=False).mean()
        df['ema_slow'] = df['close'].ewm(span=21, adjust=False).mean()
        return df
    except Exception as e:
        print(f"[OHLCV ERROR] {e}")
        return None

def compute_rsi(series, period):
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def load_filled_orders():
    if os.path.exists(FILLED_FILE):
        with open(FILLED_FILE, 'r') as f:
            return json.load(f)
    return []

def save_filled_orders(data):
    with open(FILLED_FILE, 'w') as f:
        json.dump(data, f)

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

def get_price(exchange):
    try:
        return float(exchange.fetch_ticker(SYMBOL)['last'])
    except:
        return None

def get_order_book_top(exchange):
    try:
        order_book = exchange.fetch_order_book(SYMBOL)
        top_bid = order_book['bids'][0][0] if order_book['bids'] else None
        top_ask = order_book['asks'][0][0] if order_book['asks'] else None
        return top_bid, top_ask
    except:
        return None, None

async def auto_balance(exchange):
    balance = exchange.fetch_balance()
    price = get_price(exchange)
    coin = SYMBOL.split('/')[0]
    usdt = float(balance.get('USDT', {}).get('total', 0))
    coin_amt = float(balance.get(coin, {}).get('total', 0))
    coin_val = coin_amt * price

    if abs(coin_val - usdt) > ADJUST_THRESHOLD:
        msg = f"⚖️ Cân bằng vốn: USDT={usdt:.2f}, {coin}={coin_amt:.2f} (~{coin_val:.2f})"
        await send_telegram(msg)

async def reset_grid():
    global last_reset_price
    ex = create_exchange()
    price = get_price(ex)
    if not price:
        return

    df = get_ohlcv(ex)
    if df is None:
        return

    rsi = df['rsi'].iloc[-1]
    ema_fast = df['ema_fast'].iloc[-1]
    ema_slow = df['ema_slow'].iloc[-1]

    if rsi > 70:
        await send_telegram(f"⛔ RSI = {rsi:.2f} quá cao. Hoãn reset lưới.")
        return
    elif rsi < 30:
        await send_telegram(f"⚠️ RSI = {rsi:.2f} đang quá thấp. Tạm dừng reset lưới để tránh bắt dao rơi.")
        return
    elif ema_fast < ema_slow:
        await send_telegram(f"⚠️ EMA cho tín hiệu giảm. Không nên đặt lệnh mới lúc này.")
        return

    try:
        open_orders = ex.fetch_open_orders(symbol=SYMBOL)
        for order in open_orders:
            ex.cancel_order(order['id'], symbol=SYMBOL)
        await send_telegram("🧹 Đã huỷ toàn bộ lệnh cũ thành công.")
    except Exception as e:
        await send_telegram(f"⚠️ Lỗi huỷ lệnh: {str(e)}")

    await auto_balance(ex)

    balance = ex.fetch_balance()
    usdt = float(balance.get('USDT', {}).get('free', 0))
    coin = SYMBOL.split('/')[0]
    coin_amt = float(balance.get(coin, {}).get('total', 0))

    if usdt <= RESERVE:
        await send_telegram("⚠️ Không đủ USDT để đặt lệnh.")
        return

    grid_usdt = usdt - RESERVE
    amount_per_order = grid_usdt / NUM_ORDERS
    last_reset_price = price

    sell_slots = sum(1 for i in range(-NUM_ORDERS//2, NUM_ORDERS//2 + 1) if i >= 0)
    max_sell_amount = coin_amt / sell_slots if sell_slots > 0 else 0

    for i in range(-NUM_ORDERS//2, NUM_ORDERS//2 + 1):
        level_price = price * (1 + i * SPREAD_PERCENT / 100)
        side = 'buy' if i < 0 else 'sell'
        amount = round(amount_per_order / level_price, 4)

        if side == 'sell':
            if max_sell_amount < amount:
                await send_telegram(f"⚠️ Không đủ {coin} để SELL {amount} tại {level_price:.4f}. Bỏ qua.")
                continue
            max_sell_amount -= amount

        try:
            ex.create_limit_order(SYMBOL, side, amount, level_price)
            await send_telegram(f"✅ Đặt {side.upper()} {amount} tại {level_price:.4f}")
        except Exception as e:
            await send_telegram(f"❌ Lỗi đặt {side.upper()} tại {level_price:.4f}: {str(e)}")

async def log_portfolio():
    ex = create_exchange()
    balance = ex.fetch_balance()
    price = get_price(ex)
    if not price:
        return

    usdt = float(balance.get('USDT', {}).get('total', 0))
    coin = SYMBOL.split('/')[0]
    coin_amt = float(balance.get(coin, {}).get('total', 0))
    total = usdt + coin_amt * price

    await send_telegram(f"📊 USDT: {usdt:.2f}\n{coin}: {coin_amt:.4f} (~{coin_amt * price:.2f} USDT)\nTổng: {total:.2f} USDT")

async def detect_new_fills():
    ex = create_exchange()
    recent_trades = ex.fetch_my_trades(SYMBOL, limit=50)
    filled_orders = load_filled_orders()
    known_ids = [o.get('trade_id') for o in filled_orders if 'trade_id' in o]

    for trade in recent_trades:
        if trade['side'] == 'buy' and trade['id'] not in known_ids:
            filled_orders.append({"buy_price": float(trade['price']), "amount": float(trade['amount']), "trade_id": trade['id']})
            save_filled_orders(filled_orders)
            await send_telegram(f"📥 Khớp BUY {trade['amount']} tại {trade['price']:.4f}")

async def check_filled_orders():
    ex = create_exchange()
    filled_orders = load_filled_orders()
    open_orders = ex.fetch_open_orders(symbol=SYMBOL)
    open_prices = [float(o['price']) for o in open_orders]
    price = get_price(ex)
    coin = SYMBOL.split('/')[0]
    balance = ex.fetch_balance()
    coin_amt = float(balance.get(coin, {}).get('total', 0))

    for order in filled_orders:
        target_price = order['buy_price'] * (1 + TP_PERCENT / 100)
        if target_price <= price and target_price not in open_prices:
            try:
                amount = float(order['amount'])
                if amount > coin_amt:
                    continue
                ex.create_limit_order(SYMBOL, 'sell', amount, round(target_price, 4))
                await send_telegram(f"💰 Tạo SELL tại {target_price:.4f} từ BUY {order['buy_price']:.4f}")
            except Exception as e:
                await send_telegram(f"❌ Lỗi tạo SELL từ BUY {order['buy_price']:.4f}: {str(e)}")

async def runner():
    keep_alive()
    await send_telegram("🤖 Bot MMO Grid Trailing đã khởi động!")
    await reset_grid()
    await log_portfolio()

    schedule.every(5).minutes.do(lambda: asyncio.ensure_future(log_portfolio()))
    schedule.every(2).minutes.do(lambda: asyncio.ensure_future(detect_new_fills()))
    schedule.every(2).minutes.do(lambda: asyncio.ensure_future(check_filled_orders()))
    schedule.every(5).minutes.do(lambda: asyncio.ensure_future(reset_grid()))

    while True:
        schedule.run_pending()
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(runner())
