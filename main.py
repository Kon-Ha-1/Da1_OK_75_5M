import ccxt
import asyncio
import schedule
import nest_asyncio
import time
from datetime import datetime
from telegram import Bot

# === CONFIG ===
API_KEY = "YOUR_OKX_API_KEY"
API_SECRET = "YOUR_OKX_API_SECRET"
PASSPHRASE = "YOUR_OKX_PASSPHRASE"
TELEGRAM_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID = "YOUR_CHAT_ID"
SYMBOL = "DOGE/USDT"
NUM_ORDERS = 6
SPREAD_PERCENT = 0.6
RESERVE = 20

bot = Bot(token=TELEGRAM_TOKEN)
nest_asyncio.apply()

last_total_balance = None
last_reset_price = None

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
    try:
        ex = create_exchange()
        ex.cancel_all_orders(SYMBOL)
        balance = ex.fetch_balance()
        usdt = float(balance.get('USDT', {}).get('free', 0))
        coin = SYMBOL.split('/')[0]
        coin_balance = float(balance.get(coin, {}).get('total', 0))

        if usdt <= RESERVE:
            await send_telegram("⚠️ Không đủ USDT để đặt lệnh.")
            return

        grid_usdt = usdt - RESERVE
        amount_per_order = grid_usdt / NUM_ORDERS
        base_price = get_price(ex)
        if not base_price:
            return

        last_reset_price = base_price
        buy, sell = 0, 0
        for i in range(-NUM_ORDERS//2, NUM_ORDERS//2+1):
            price = base_price * (1 + i * SPREAD_PERCENT / 100)
            side = 'buy' if i < 0 else 'sell'
            amount = amount_per_order / price

            if side == 'sell' and coin_balance < amount:
                continue

            ex.create_limit_order(SYMBOL, side, amount, price)
            if side == 'buy':
                buy += 1
            else:
                sell += 1
        await send_telegram(f"✅ Lệnh BUY: {buy}, SELL: {sell}")
    except Exception as e:
        await send_telegram(f"❌ Lỗi reset_grid: {str(e)}")

async def log_portfolio():
    global last_total_balance
    try:
        ex = create_exchange()
        balance = ex.fetch_balance()
        coin = SYMBOL.split('/')[0]
        usdt = float(balance.get('USDT', {}).get('total', 0))
        coin_amt = float(balance.get(coin, {}).get('total', 0))
        price = get_price(ex)
        if not price:
            return
        total = usdt + coin_amt * price
        if last_total_balance is None or abs(total - last_total_balance) > 0.01:
            last_total_balance = total
            await send_telegram(f"📊 USDT: {usdt:.2f}, {coin}: {coin_amt:.4f}, Tổng: {total:.2f} USDT")
    except Exception as e:
        await send_telegram(f"❌ Lỗi log_portfolio: {str(e)}")

async def check_price_and_reset():
    global last_reset_price
    ex = create_exchange()
    current_price = get_price(ex)
    if not current_price:
        return
    if last_reset_price is None:
        last_reset_price = current_price
        await reset_grid()
    elif abs(current_price - last_reset_price) / last_reset_price >= 0.005:
        await send_telegram("🔁 Biến động giá mạnh, reset lệnh.")
        await reset_grid()

async def runner():
    await send_telegram("🤖 Grid Bot khởi động!")
    schedule.every().day.at("00:00").do(lambda: asyncio.ensure_future(reset_grid()))
    schedule.every(5).minutes.do(lambda: asyncio.ensure_future(log_portfolio()))
    schedule.every(5).minutes.do(lambda: asyncio.ensure_future(check_price_and_reset()))
    while True:
        schedule.run_pending()
        await asyncio.sleep(1)

asyncio.get_event_loop().run_until_complete(runner())
