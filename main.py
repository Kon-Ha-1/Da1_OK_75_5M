import ccxt.async_support as ccxt
import asyncio
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
import schedule
import nest_asyncio
from telegram import Bot
import logging
import json
import os

# === CONFIG ===
API_KEY = "99d39d59-c05d-4e40-9f2a-3615eac315ea"  # Thay bằng API Key của bro
API_SECRET = "4B1D25C8F05E12717AD561584B2853E6"  # Thay bằng API Secret
PASSPHRASE = "Mmoarb2025@"  # Thay bằng Passphrase
TELEGRAM_TOKEN = "7817283052:AAF2fjxxZT8LP-gblBeTbpb0N0-a0C7GLQ8"
TELEGRAM_CHAT_ID = "5850622014"
SYMBOL = "DOGE/USDT:USDT"  # Futures vĩnh cửu
TOTAL_CAPITAL = 50.0  # Vốn 50 USDT
LONG_CAPITAL = 25.0  # Vốn cho LONG
SHORT_CAPITAL = 25.0  # Vốn cho SHORT
LEVERAGE = 5  # Đòn bẩy 5x
DAILY_PROFIT_TARGET = 20.0  # Target 20% (10 USDT/ngày)
RISK_PER_TRADE = 0.4  # Rủi ro 40% vốn mỗi lệnh
DCA_STEP = -0.015  # Mua thêm khi giá giảm 1.5%
STOP_LOSS_PERCENT = -10.0  # Cắt lỗ -10%
TAKE_PROFIT_PERCENT = 3.0  # Chốt lời 3%
RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
VOLATILITY_THRESHOLD = 0.10  # Tạm dừng nếu biến động >10%
CHECK_INTERVAL = 30  # Kiểm tra mỗi 30s
STATE_FILE = "state.json"

# Khởi tạo Telegram và nest_asyncio
bot = Bot(token=TELEGRAM_TOKEN)
nest_asyncio.apply()

# Biến trạng thái
last_total_value_usd = None
daily_start_capital_usd = None
last_day = None
long_position = {'size': 0, 'avg_price': 0, 'orders': [], 'usdt': 0}
short_position = {'size': 0, 'avg_price': 0, 'orders': [], 'usdt': 0}
total_profit = 0.0

# Cấu hình logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def create_exchange():
    return ccxt.okx({
        'apiKey': API_KEY,
        'secret': API_SECRET,
        'password': PASSPHRASE,
        'enableRateLimit': True,
        'options': {'defaultType': 'swap'}  # Futures
    })

async def send_telegram(msg):
    vn_time = datetime.now(timezone(timedelta(hours=7))).strftime('%H:%M:%S %d/%m/%Y')
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"{msg}\n⏰ Giờ VN: {vn_time}")

def load_state():
    global daily_start_capital_usd, last_day, long_position, short_position, total_profit
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r') as f:
                state = json.load(f)
                daily_start_capital_usd = float(state.get('daily_start_capital_usd', TOTAL_CAPITAL))
                last_day_str = state.get('last_day')
                last_day = datetime.strptime(last_day_str, '%Y-%m-%d').date() if last_day_str else None
                long_position = state.get('long_position', {'size': 0, 'avg_price': 0, 'orders': [], 'usdt': 0})
                short_position = state.get('short_position', {'size': 0, 'avg_price': 0, 'orders': [], 'usdt': 0})
                total_profit = float(state.get('total_profit', 0.0))
                logger.info(f"Đã load state: capital={daily_start_capital_usd}, last_day={last_day}")
    except Exception as e:
        logger.error(f"Lỗi load state: {e}")

def save_state():
    try:
        state = {
            'daily_start_capital_usd': daily_start_capital_usd,
            'last_day': last_day.strftime('%Y-%m-%d') if last_day else None,
            'long_position': long_position,
            'short_position': short_position,
            'total_profit': total_profit
        }
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)
        logger.info("Đã save state")
    except Exception as e:
        logger.error(f"Lỗi save state: {e}")

async def fetch_wallet_balance(exchange):
    try:
        balance = await exchange.fetch_balance()
        usdt = float(balance['total'].get('USDT', 0.0))
        return usdt
    except Exception as e:
        logger.error(f"Lỗi lấy số dư: {e}")
        return None

async def fetch_ohlcv(exchange, timeframe='5m', limit=100):
    try:
        data = await exchange.fetch_ohlcv(SYMBOL, timeframe, limit=limit)
        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert('Asia/Ho_Chi_Minh')
        df['rsi'] = df['close'].diff().apply(lambda x: x if x > 0 else 0).rolling(RSI_PERIOD).mean() / \
                    abs(df['close'].diff().apply(lambda x: x if x < 0 else 0)).rolling(RSI_PERIOD).mean()
        df['rsi'] = 100 - (100 / (1 + df['rsi']))
        return df
    except Exception as e:
        logger.error(f"Lỗi lấy OHLCV: {e}")
        return None

async def fetch_funding_rate(exchange):
    try:
        funding = await exchange.fetch_funding_rate(SYMBOL)
        return funding['fundingRate']
    except Exception as e:
        logger.error(f"Lỗi lấy funding rate: {e}")
        return 0.0

async def check_volatility(exchange):
    try:
        df = await fetch_ohlcv(exchange, '1h', limit=10)
        if df is None:
            return False
        recent_prices = df['close'][-10:]
        volatility = (recent_prices.max() - recent_prices.min()) / recent_prices.min()
        return volatility > VOLATILITY_THRESHOLD
    except Exception as e:
        logger.error(f"Lỗi kiểm tra biến động: {e}")
        return False

async def place_futures_order(exchange, side, amount, position_side):
    try:
        params = {
            'leverage': LEVERAGE,
            'posSide': position_side,
            'reduceOnly': False
        }
        order = await exchange.create_market_order(SYMBOL, side, amount, params=params)
        logger.info(f"Đặt lệnh {side} {position_side}: {amount} tại {order['price']}")
        return order
    except Exception as e:
        logger.error(f"Lỗi đặt lệnh: {e}")
        await send_telegram(f"❌ Lỗi đặt lệnh {position_side}: {str(e)}")
        return None

async def close_futures_position(exchange, side, amount, position_side):
    try:
        params = {
            'posSide': position_side,
            'reduceOnly': True
        }
        order = await exchange.create_market_order(SYMBOL, side, amount, params=params)
        logger.info(f"Đóng vị thế {position_side}: {amount} tại {order['price']}")
        return order
    except Exception as e:
        logger.error(f"Lỗi đóng vị thế: {e}")
        await send_telegram(f"❌ Lỗi đóng vị thế {position_side}: {str(e)}")
        return None

async def manage_long_position(exchange, current_price, rsi):
    global long_position, total_profit
    try:
        funding_rate = await fetch_funding_rate(exchange)
        if long_position['size'] == 0 and rsi < RSI_OVERSOLD and funding_rate < 0.01:
            # Mở vị thế LONG mới
            trade_usdt = LONG_CAPITAL * RISK_PER_TRADE
            amount = (trade_usdt * LEVERAGE) / current_price
            order = await place_futures_order(exchange, 'buy', amount, 'long')
            if order:
                long_position['size'] = amount
                long_position['avg_price'] = current_price
                long_position['usdt'] = trade_usdt
                long_position['orders'] = [{'price': current_price, 'amount': amount, 'usdt': trade_usdt}]
                await send_telegram(
                    f"🟢 LONG {SYMBOL}: {amount:.2f} DOGE | Giá: {current_price:.5f} | USDT: {trade_usdt:.2f}"
                )
        elif long_position['size'] > 0:
            # Quản lý vị thế LONG
            price_change = (current_price - long_position['avg_price']) / long_position['avg_price'] * 100
            if price_change <= DCA_STEP and len(long_position['orders']) < 3:
                # DCA: Mua thêm
                trade_usdt = LONG_CAPITAL * RISK_PER_TRADE * (2 ** len(long_position['orders']))
                if trade_usdt + long_position['usdt'] <= LONG_CAPITAL:
                    amount = (trade_usdt * LEVERAGE) / current_price
                    order = await place_futures_order(exchange, 'buy', amount, 'long')
                    if order:
                        long_position['size'] += amount
                        long_position['usdt'] += trade_usdt
                        long_position['orders'].append({'price': current_price, 'amount': amount, 'usdt': trade_usdt})
                        total_cost = sum(o['price'] * o['amount'] for o in long_position['orders'])
                        long_position['avg_price'] = total_cost / long_position['size']
                        await send_telegram(
                            f"🟢 LONG DCA {SYMBOL}: {amount:.2f} DOGE | Giá: {current_price:.5f} | USDT: {trade_usdt:.2f}"
                        )
            elif price_change >= TAKE_PROFIT_PERCENT:
                # Chốt lời
                order = await close_futures_position(exchange, 'sell', long_position['size'], 'long')
                if order:
                    profit = (current_price - long_position['avg_price']) * long_position['size']
                    total_profit += profit
                    await send_telegram(
                        f"🔒 LONG Lời {SYMBOL}: {profit:.2f} USDT | Tổng Lời: {total_profit:.2f}"
                    )
                    long_position = {'size': 0, 'avg_price': 0, 'orders': [], 'usdt': 0}
            elif price_change <= STOP_LOSS_PERCENT:
                # Cắt lỗ
                order = await close_futures_position(exchange, 'sell', long_position['size'], 'long')
                if order:
                    loss = (current_price - long_position['avg_price']) * long_position['size']
                    total_profit += loss
                    await send_telegram(
                        f"🛑 LONG Lỗ {SYMBOL}: {loss:.2f} USDT | Tổng Lời: {total_profit:.2f}"
                    )
                    long_position = {'size': 0, 'avg_price': 0, 'orders': [], 'usdt': 0}
    except Exception as e:
        logger.error(f"Lỗi quản lý LONG: {e}")
        await send_telegram(f"❌ Lỗi LONG: {str(e)}")

async def manage_short_position(exchange, current_price, rsi):
    global short_position, total_profit
    try:
        funding_rate = await fetch_funding_rate(exchange)
        if short_position['size'] == 0 and rsi > RSI_OVERBOUGHT and funding_rate > -0.01:
            # Mở vị thế SHORT mới
            trade_usdt = SHORT_CAPITAL * RISK_PER_TRADE
            amount = (trade_usdt * LEVERAGE) / current_price
            order = await place_futures_order(exchange, 'sell', amount, 'short')
            if order:
                short_position['size'] = amount
                short_position['avg_price'] = current_price
                short_position['usdt'] = trade_usdt
                short_position['orders'] = [{'price': current_price, 'amount': amount, 'usdt': trade_usdt}]
                await send_telegram(
                    f"🔴 SHORT {SYMBOL}: {amount:.2f} DOGE | Giá: {current_price:.5f} | USDT: {trade_usdt:.2f}"
                )
        elif short_position['size'] > 0:
            # Quản lý vị thế SHORT
            price_change = (short_position['avg_price'] - current_price) / short_position['avg_price'] * 100
            if price_change >= TAKE_PROFIT_PERCENT:
                # Chốt lời
                order = await close_futures_position(exchange, 'buy', short_position['size'], 'short')
                if order:
                    profit = (short_position['avg_price'] - current_price) * short_position['size']
                    total_profit += profit
                    await send_telegram(
                        f"🔒 SHORT Lời {SYMBOL}: {profit:.2f} USDT | Tổng Lời: {total_profit:.2f}"
                    )
                    short_position = {'size': 0, 'avg_price': 0, 'orders': [], 'usdt': 0}
            elif price_change <= STOP_LOSS_PERCENT:
                # Cắt lỗ
                order = await close_futures_position(exchange, 'buy', short_position['size'], 'short')
                if order:
                    loss = (short_position['avg_price'] - current_price) * short_position['size']
                    total_profit += loss
                    await send_telegram(
                        f"🛑 SHORT Lỗ {SYMBOL}: {loss:.2f} USDT | Tổng Lời: {total_profit:.2f}"
                    )
                    short_position = {'size': 0, 'avg_price': 0, 'orders': [], 'usdt': 0}
    except Exception as e:
        logger.error(f"Lỗi quản lý SHORT: {e}")
        await send_telegram(f"❌ Lỗi SHORT: {str(e)}")

async def log_assets(exchange):
    global daily_start_capital_usd, last_day, last_total_value_usd, total_profit
    try:
        balance = await fetch_wallet_balance(exchange)
        if balance is None:
            return None, None
        total_value_usd = balance
        now = datetime.now(timezone(timedelta(hours=7)))
        today = now.date()

        if last_day is None or (today != last_day and now.hour >= 21):
            daily_start_capital_usd = total_value_usd
            last_day = today
            total_profit = 0.0
            save_state()

        profit_percent = ((total_value_usd - daily_start_capital_usd) / daily_start_capital_usd * 100) if daily_start_capital_usd > 0 else 0

        if last_total_value_usd is None or abs(total_value_usd - last_total_value_usd) > 0.01:
            msg = f"💰 Tổng tài sản: {total_value_usd:.2f} USDT\n📈 Lợi nhuận ngày: {profit_percent:.2f}% ({total_profit:.2f} USDT)"
            await send_telegram(msg)
            last_total_value_usd = total_value_usd

        return total_value_usd, profit_percent
    except Exception as e:
        logger.error(f"Lỗi log tài sản: {e}")
        await send_telegram(f"❌ Lỗi log tài sản: {str(e)}")
        return None, None

async def trade_all(exchange):
    global total_profit
    total_value_usd, profit_percent = await log_assets(exchange)
    if profit_percent is not None and profit_percent >= DAILY_PROFIT_TARGET:
        await send_telegram("🎯 Đạt target 20% lợi nhuận ngày. Tạm dừng giao dịch.")
        return

    if await check_volatility(exchange):
        await send_telegram("⚠️ Biến động cao (>10%). Tạm dừng 1 giờ.")
        await asyncio.sleep(3600)
        return

    balance = await fetch_wallet_balance(exchange)
    if balance is None or balance < 10:
        await send_telegram("❌ Số dư dưới 10 USDT. Dừng bot.")
        return

    df = await fetch_ohlcv(exchange)
    if df is None:
        return
    current_price = df['close'].iloc[-1]
    rsi = df['rsi'].iloc[-1]

    await asyncio.gather(
        manage_long_position(exchange, current_price, rsi),
        manage_short_position(exchange, current_price, rsi)
    )

async def runner():
    global daily_start_capital_usd, last_day
    exchange = create_exchange()
    load_state()

    now = datetime.now(timezone(timedelta(hours=7)))
    today = now.date()
    if daily_start_capital_usd is None or last_day is None or today != last_day:
        total_value_usd = await fetch_wallet_balance(exchange)
        if total_value_usd is not None:
            daily_start_capital_usd = total_value_usd
            last_day = today
            save_state()
            await send_telegram(f"🤖 Bot khởi động! Vốn: {daily_start_capital_usd:.2f} USDT | Target: 20%/ngày")
        else:
            await send_telegram("❌ Lỗi lấy số dư. Dừng bot.")
            return
    else:
        await send_telegram(f"🤖 Bot khởi động! Vốn: {daily_start_capital_usd:.2f} USDT | Target: 20%/ngày")

    try:
        await exchange.set_leverage(LEVERAGE, SYMBOL)
    except Exception as e:
        await send_telegram(f"❌ Lỗi set đòn bẩy: {str(e)}")
        return

    schedule.every(CHECK_INTERVAL).seconds.do(lambda: asyncio.ensure_future(trade_all(exchange)))
    schedule.every(5).minutes.do(lambda: asyncio.ensure_future(log_assets(exchange)))
    while True:
        schedule.run_pending()
        await asyncio.sleep(1)

async def main():
    try:
        await runner()
    except Exception as e:
        logger.error(f"Lỗi nghiêm trọng: {e}")
        await send_telegram(f"❌ Bot crash: {str(e)}")
    finally:
        exchange = create_exchange()
        await exchange.close()

if __name__ == "__main__":
    asyncio.run(main())
