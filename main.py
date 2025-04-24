import ccxt
import asyncio
import schedule
import nest_asyncio
import time
import json
import os
from datetime import datetime
from telegram import Bot
from keep_alive import keep_alive

# === CONFIG ===
API_KEY = "99d39d59-c05d-4e40-9f2a-3615eac315ea"
API_SECRET = "4B1D25C8F05E12717AD561584B2853E6"
PASSPHRASE = "Mmoarb2025@"
TELEGRAM_TOKEN = "7817283052:AAF2fjxxZT8LP-gblBeTbpb0N0-a0C7GLQ8"
TELEGRAM_CHAT_ID = "5850622014"

SYMBOL = "ARB/USDT"
NUM_ORDERS = 6
SPREAD_PERCENT = 0.5
RESERVE = 15
TP_PERCENT = 0.5

bot = Bot(token=TELEGRAM_TOKEN)
nest_asyncio.apply()

last_total_balance = None
last_reset_price = None
FILLED_FILE = "filled_orders.json"

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

async def reset_grid():
    global last_reset_price
    ex = create_exchange()
    price = get_price(ex)
    if not price:
        return

    try:
        open_orders = ex.fetch_open_orders(symbol=SYMBOL)
        for order in open_orders:
            ex.cancel_order(order['id'], symbol=SYMBOL)
        await send_telegram("🧹 Huỷ toàn bộ lệnh cũ thành công.")
    except Exception as e:
        await send_telegram(f"⚠️ Huỷ lệnh lỗi: {str(e)}")

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

    for i in range(-NUM_ORDERS//2, NUM_ORDERS//2 + 1):
        level_price = price * (1 + i * SPREAD_PERCENT / 100)
        side = 'buy' if i < 0 else 'sell'
        amount = round(amount_per_order / level_price, 4)
        if side == 'sell' and coin_amt < amount:
            continue
        try:
            order = ex.create_limit_order(SYMBOL, side, amount, level_price)
            await send_telegram(f"✅ Đặt {side.upper()} {amount} tại {level_price:.4f}")
        except Exception as e:
            await send_telegram(f"❌ Lỗi đặt {side} tại {level_price:.4f}: {str(e)}")

async def log_portfolio():
    global last_total_balance
    ex = create_exchange()
    balance = ex.fetch_balance()
    price = get_price(ex)
    if not price:
        return

    usdt = float(balance.get('USDT', {}).get('total', 0))
    coin = SYMBOL.split('/')[0]
    coin_amt = float(balance.get(coin, {}).get('total', 0))
    total = usdt + coin_amt * price

    if last_total_balance is None or abs(total - last_total_balance) >= 0.01:
        last_total_balance = total
        await send_telegram(
            f"📊 USDT: {usdt:.2f}\n{coin}: {coin_amt:.4f} (~{coin_amt * price:.2f} USDT)\nTổng: {total:.2f} USDT")

async def check_filled_orders():
    ex = create_exchange()
    filled_orders = load_filled_orders()
    open_orders = ex.fetch_open_orders(symbol=SYMBOL)
    open_prices = [float(o['price']) for o in open_orders]
    price = get_price(ex)

    for order in filled_orders:
        target_price = order['buy_price'] * (1 + TP_PERCENT / 100)
        if target_price <= price and target_price not in open_prices:
            try:
                amount = float(order['amount'])
                ex.create_limit_order(SYMBOL, 'sell', amount, round(target_price, 4))
                await send_telegram(f"💰 Tạo SELL tại {target_price:.4f} từ BUY {order['buy_price']:.4f}")
            except Exception as e:
                await send_telegram(f"❌ Lỗi tạo SELL từ BUY {order['buy_price']:.4f}: {str(e)}")

async def detect_new_fills():
    ex = create_exchange()
    recent_trades = ex.fetch_my_trades(SYMBOL, limit=20)
    filled_orders = load_filled_orders()
    known_prices = [o['buy_price'] for o in filled_orders]

    for trade in recent_trades:
        if trade['side'] == 'buy':
            price = float(trade['price'])
            amount = float(trade['amount'])
            if price not in known_prices:
                filled_orders.append({"buy_price": price, "amount": amount})
                save_filled_orders(filled_orders)
                await send_telegram(f"📥 Khớp BUY {amount} tại {price:.4f}")

async def runner():
    keep_alive()
    await send_telegram("🤖 Bot MMO Grid Trailing đã khởi động!")
    await reset_grid()
    await log_portfolio()

    schedule.every(5).minutes.do(lambda: asyncio.ensure_future(log_portfolio()))
    schedule.every(2).minutes.do(lambda: asyncio.ensure_future(detect_new_fills()))
    schedule.every(2).minutes.do(lambda: asyncio.ensure_future(check_filled_orders()))

    while True:
        schedule.run_pending()
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(runner())
