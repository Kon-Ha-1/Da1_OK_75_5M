import ccxt
import asyncio
import schedule
import nest_asyncio
import time
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

SYMBOL = os.environ.get("SYMBOL", "ARB/USDT")
NUM_ORDERS = int(os.environ.get("NUM_ORDERS", 6))
SPREAD_PERCENT = float(os.environ.get("SPREAD_PERCENT", 0.5))
RESERVE = float(os.environ.get("RESERVE", 20))

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
    except Exception as e:
        print(f"[Lỗi lấy giá] {e}")
        return None

async def reset_grid():
    global last_reset_price
    try:
        print("[RESET] Bắt đầu reset lưới")
        ex = create_exchange()

        # Huỷ từng lệnh cũ nếu có
        try:
            open_orders = ex.fetch_open_orders(symbol=SYMBOL)
            for order in open_orders:
                try:
                    ex.cancel_order(order['id'], symbol=SYMBOL)
                except Exception as cancel_err:
                    await send_telegram(f"⚠️ Huỷ lệnh {order['id']} thất bại: {cancel_err}")
            await send_telegram("🧹 Đã huỷ toàn bộ lệnh cũ (thủ công).")
        except Exception as e:
            await send_telegram(f"⚠️ Lỗi huỷ lệnh cũ: {str(e)}")

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
            await send_telegram("⚠️ Không lấy được giá thị trường.")
            return

        last_reset_price = base_price
        buy, sell = 0, 0

        for i in range(-NUM_ORDERS // 2, NUM_ORDERS // 2 + 1):
            price = base_price * (1 + i * SPREAD_PERCENT / 100)
            side = 'buy' if i < 0 else 'sell'
            amount = round(amount_per_order / price, 4)

            if side == 'sell': 
                if coin_balance < amount:
                    await send_telegram(f"⚠️ Bỏ SELL tại {price:.4f}: không đủ {coin}.")
                    continue
                coin_balance -= amount
            try:
                ex.create_limit_order(SYMBOL, side, amount, price)
                if side == 'buy':
                    buy += 1
                else:
                    sell += 1
                await send_telegram(f"✅ Đặt {side.upper()} tại {price:.4f}")
            except Exception as e:
                await send_telegram(f"❌ Lỗi đặt {side.upper()} tại {price:.4f}: {str(e)}")

        await send_telegram(f"⏳ Tổng BUY: {buy} | SELL: {sell}. Đang chờ khớp...")
    except Exception as e:
        await send_telegram(f"❌ Lỗi reset_grid: {str(e)}")

async def log_portfolio():
    global last_total_balance
    try:
        print("[LOG] Kiểm tra tài sản")
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
            await send_telegram(
                f"📊 USDT: {usdt:.2f}\n{coin}: {coin_amt:.4f} (~{coin_amt * price:.2f} USDT)\nTổng: {total:.2f} USDT"
            )
        else:
            print("✅ Tài sản không thay đổi.")
    except Exception as e:
        await send_telegram(f"❌ Lỗi log_portfolio: {str(e)}")

async def check_price_and_reset():
    global last_reset_price
    print("[CHECK] Kiểm tra biến động giá")
    ex = create_exchange()
    current_price = get_price(ex)
    if not current_price:
        print("⚠️ Không lấy được giá.")
        return

    if last_reset_price is None:
        last_reset_price = current_price
        await reset_grid()
    elif abs(current_price - last_reset_price) / last_reset_price >= 0.005:
        await send_telegram("📈 Biến động giá > 0.5%, reset lại lưới.")
        await reset_grid()

async def runner():
    keep_alive()
    print("🚀 Bot khởi động")
    await send_telegram("🤖 Grid Bot khởi động!")
    await log_portfolio()
    await reset_grid()

    schedule.every().day.at("00:00").do(lambda: asyncio.ensure_future(reset_grid()))
    schedule.every(5).minutes.do(lambda: asyncio.ensure_future(log_portfolio()))
    schedule.every(5).minutes.do(lambda: asyncio.ensure_future(check_price_and_reset()))

    while True:
        print("[LOOP] Tick schedule...")
        schedule.run_pending()
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(runner())
