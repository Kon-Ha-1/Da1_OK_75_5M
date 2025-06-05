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
LONG_CAPITAL = 25.0  # Vốn LONG
SHORT_CAPITAL = 25.0  # Vốn SHORT
LEVERAGE = 5  # Đòn bẩy 5x
DAILY_PROFIT_TARGET = 20.0  # Target 20% (10 USDT)
RISK_PER_TRADE = 0.3  # Rủi ro 40%
DCA_STEP = -0.015  # Giảm 1.5% thì mua thêm
STOP_LOSS_PERCENT = -4.0  # Cắt lỗ -4%
MIN_TAKE_PROFIT = 0.8  # Tối thiểu 0.8%
RSI_PERIOD = 14
RSI_OVERSOLD = 40
RSI_OVERBOUGHT = 60
CHECK_INTERVAL = 10  # Kiểm tra mỗi 10s
STATE_FILE = "state.json"
MIN_BALANCE = 5.0  # Dừng nếu số dư < 5 USDT

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
atr_14 = 0.0
take_profit_percent = MIN_TAKE_PROFIT

# Cấu hình logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def create_exchange():
    return ccxt.okx({
        'apiKey': API_KEY,
        'secret': API_SECRET,
        'password': PASSPHRASE,
        'enableRateLimit': True,
        'options': {'defaultType': 'swap'}
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
        balance = await exchange.fetch_balance({'type': 'swap'})  # Fetch số dư ví futures
        usdt = float(balance['USDT']['free'])  # Số USDT khả dụng trong ví futures
        return usdt
    except Exception as e:
        logger.error(f"Lỗi lấy số dư futures: {e}")
        return None

async def fetch_ohlcv(exchange, timeframe='5m', limit=100):
    try:
        data = await exchange.fetch_ohlcv(SYMBOL, timeframe, limit=limit)
        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert('Asia/Ho_Chi_Minh')
        # Tính RSI
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=RSI_PERIOD).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=RSI_PERIOD).mean()
        rs = gain / loss
        df['rsi'] = 100 - (100 / (1 + rs))
        # Tính ATR_14
        high_low = df['high'] - df['low']
        high_close = np.abs(df['high'] - df['close'].shift())
        low_close = np.abs(df['low'] - df['close'].shift())
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        df['atr'] = tr.rolling(window=14).mean()
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

async def update_take_profit(exchange):
    global atr_14, take_profit_percent
    try:
        df = await fetch_ohlcv(exchange, '1h', limit=14)
        if df is not None:
            atr_14 = df['atr'].iloc[-1]
            current_price = df['close'].iloc[-1]
            atr_percent = (atr_14 / current_price) * 100
            take_profit_percent = max(MIN_TAKE_PROFIT, atr_percent * 0.5)
            logger.info(f"Cập nhật take_profit_percent: {take_profit_percent:.2f}%")
    except Exception as e:
        logger.error(f"Lỗi cập nhật take_profit: {e}")

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
    global long_position, total_profit, take_profit_percent
    try:
        funding_rate = await fetch_funding_rate(exchange)
        if long_position['size'] == 0 and rsi < RSI_OVERSOLD and funding_rate < 0.005:
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
            price_change = (current_price - long_position['avg_price']) / long_position['avg_price'] * 100
            if price_change <= DCA_STEP and len(long_position['orders']) < 2:
                trade_usdt = LONG_CAPITAL * RISK_PER_TRADE * 2
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
            elif price_change >= take_profit_percent:
                order = await close_futures_position(exchange, 'sell', long_position['size'], 'long')
                if order:
                    profit = (current_price - long_position['avg_price']) * long_position['size']
                    total_profit += profit
                    await send_telegram(
                        f"🔒 LONG Lời {SYMBOL}: {profit:.2f} USDT | Tổng Lời: {total_profit:.2f}"
                    )
                    long_position = {'size': 0, 'avg_price': 0, 'orders': [], 'usdt': 0}
            elif price_change <= STOP_LOSS_PERCENT:
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
    global short_position, total_profit, take_profit_percent
    try:
        funding_rate = await fetch_funding_rate(exchange)
        if short_position['size'] == 0 and rsi > RSI_OVERBOUGHT and funding_rate > -0.005:
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
            price_change = (short_position['avg_price'] - current_price) / short_position['avg_price'] * 100
            if price_change >= take_profit_percent:
                order = await close_futures_position(exchange, 'buy', short_position['size'], 'short')
                if order:
                    profit = (short_position['avg_price'] - current_price) * short_position['size']
                    total_profit += profit
                    await send_telegram(
                        f"🔒 SHORT Lời {SYMBOL}: {profit:.2f} USDT | Tổng Lời: {total_profit:.2f}"
                    )
                    short_position = {'size': 0, 'avg_price': 0, 'orders': [], 'usdt': 0}
            elif price_change <= STOP_LOSS_PERCENT:
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
    global take_profit_percent
    total_value_usd, profit_percent = await log_assets(exchange)
    if profit_percent is not None and profit_percent >= DAILY_PROFIT_TARGET:
        await send_telegram("🎯 Đạt target 20% lợi nhuận ngày. Tạm dừng giao dịch.")
        return

    balance = await fetch_wallet_balance(exchange)
    if balance is None or balance < MIN_BALANCE:
        await send_telegram(f"❌ Số dư dưới {MIN_BALANCE} USDT. Dừng bot.")
        return

    df = await fetch_ohlcv(exchange)
    if df is None:
        return
    current_price = df['close'].iloc[-1]
    rsi = df['rsi'].iloc[-1]
    atr_14 = df['atr'].iloc[-1]
    atr_percent = (atr_14 / current_price) * 100
    take_profit_percent = max(MIN_TAKE_PROFIT, atr_percent * 0.5)
    logger.info(f"Take-profit hiện tại: {take_profit_percent:.2f}%")

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
    schedule.every(60).minutes.do(lambda: asyncio.ensure_future(update_take_profit(exchange)))
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
