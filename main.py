import ccxt.async_support as ccxt
import asyncio
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
import schedule
import nest_asyncio
from telegram import Bot
from sklearn.preprocessing import MinMaxScaler
from tensorflow.keras.models import Sequential
from tensorflow.keras.layers import LSTM, Dense
import logging
import json
import os
from keep_alive import keep_alive

# === CONFIG ===
API_KEY = "99d39d59-c05d-4e40-9f2a-3615eac315ea"
API_SECRET = "4B1D25C8F05E12717AD561584B2853E6"
PASSPHRASE = "Mmoarb2025@"
TELEGRAM_TOKEN = "7817283052:AAF2fjxxZT8LP-gblBeTbpb0N0-a0C7GLQ8"
TELEGRAM_CHAT_ID = "5850622014"
SYMBOLS = ["DOGE/USDT", "PEPE/USDT"]

# Bot configuration
DAILY_PROFIT_TARGET = 5.0  # Mục tiêu 5% mỗi ngày
RISK_PER_TRADE = 0.5  # Giảm rủi ro giao dịch xuống 50%
STOP_LOSS_PERCENT = -3.0  # Stop-loss -3%
TAKE_PROFIT_PERCENT = 2.5  # Take-profit 2.5%
STATE_FILE = "state.json"  # File lưu trạng thái

bot = Bot(token=TELEGRAM_TOKEN)
nest_asyncio.apply()

# State variables
last_total_value_usd = None
daily_start_capital_usd = None  # Sẽ lấy từ ví hoặc state.json
last_day = None
active_orders = {}
lowest_prices = {}
market_conditions = {}

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def create_exchange():
    return ccxt.okx({
        'apiKey': API_KEY,
        'secret': API_SECRET,
        'password': PASSPHRASE,
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'}
    })

async def send_telegram(msg):
    vn_time = datetime.now(timezone(timedelta(hours=7))).strftime('%H:%M:%S %d/%m/%Y')
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"{msg}\n⏰ Giờ VN: {vn_time}")

def load_state():
    global daily_start_capital_usd, last_day
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r') as f:
                state = json.load(f)
                daily_start_capital_usd = float(state.get('daily_start_capital_usd', 20.0))
                last_day_str = state.get('last_day')
                last_day = datetime.strptime(last_day_str, '%Y-%m-%d').date() if last_day_str else None
                logger.info(f"Loaded state: capital={daily_start_capital_usd}, last_day={last_day}")
    except Exception as e:
        logger.error(f"Error loading state: {e}")

def save_state():
    try:
        state = {
            'daily_start_capital_usd': daily_start_capital_usd,
            'last_day': last_day.strftime('%Y-%m-%d') if last_day else None
        }
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)
        logger.info("Saved state")
    except Exception as e:
        logger.error(f"Error saving state: {e}")

async def fetch_usdt_usd_rate(exchange):
    try:
        ticker = await exchange.fetch_ticker("USDT/USD")
        return float(ticker['last'])
    except Exception:
        return 1.0

async def fetch_wallet_balance(exchange):
    try:
        balance = await exchange.fetch_balance()
        total_value_usdt = 0.0
        usdt_usd_rate = await fetch_usdt_usd_rate(exchange)

        usdt = float(balance['total'].get('USDT', 0.0))
        total_value_usdt = usdt

        for currency in balance['total']:
            coin_balance = float(balance['total'].get(currency, 0.0))
            if coin_balance > 0 and currency != 'USDT':
                try:
                    symbol = f"{currency}/USDT"
                    ticker = await exchange.fetch_ticker(symbol)
                    price = ticker['last']
                    coin_value = coin_balance * price
                    total_value_usdt += coin_value
                except Exception:
                    continue

        return total_value_usdt * usdt_usd_rate
    except Exception as e:
        logger.error(f"Error fetching wallet balance: {e}")
        return None

def compute_rsi(data, periods=14):
    delta = data.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=periods).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=periods).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def compute_atr(df, periods=14):
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(window=periods).mean()

async def fetch_ohlcv(exchange, symbol, timeframe, limit=50):
    try:
        data = await exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert('Asia/Ho_Chi_Minh')
        
        df['rsi14'] = compute_rsi(df['close'], 14)
        df['atr'] = compute_atr(df, 14)
        df['ema_fast'] = df['close'].ewm(span=5, adjust=False).mean()
        df['ema_slow'] = df['close'].ewm(span=12, adjust=False).mean()
        df['macd'] = df['ema_fast'] - df['ema_slow']
        df['signal'] = df['macd'].ewm(span=9, adjust=False).mean()
        
        return df
    except Exception as e:
        logger.error(f"Error fetching OHLCV for {symbol}: {e}")
        return None

async def get_lowest_price_7d(exchange, symbol):
    try:
        df = await fetch_ohlcv(exchange, symbol, '1d', limit=7)
        if df is None or len(df) < 7:
            return None
        return df['low'].min()
    except Exception as e:
        logger.error(f"Error getting lowest price for {symbol}: {e}")
        return None

async def check_market_conditions(exchange, symbol):
    try:
        df_4h = await fetch_ohlcv(exchange, symbol, '4h', limit=2)
        if df_4h is None or len(df_4h) < 2:
            return False
        price_change = (df_4h['close'].iloc[-1] - df_4h['close'].iloc[-2]) / df_4h['close'].iloc[-2] * 100
        return price_change > -5
    except Exception as e:
        logger.error(f"Error checking market conditions for {symbol}: {e}")
        return False

async def check_liquidity(exchange, symbol, amount):
    try:
        order_book = await exchange.fetch_order_book(symbol)
        bid_volume = sum(bid[1] for bid in order_book['bids'][:5])
        return bid_volume >= amount
    except Exception as e:
        logger.error(f"Error checking liquidity for {symbol}: {e}")
        return False

def detect_pump_dump(df):
    try:
        avg_volume = df['volume'].rolling(10).mean().iloc[-1]
        return df['volume'].iloc[-1] > 2 * avg_volume
    except Exception:
        return False

def create_lstm_model():
    model = Sequential()
    model.add(LSTM(50, input_shape=(50, 1), return_sequences=True))
    model.add(LSTM(50))
    model.add(Dense(1))
    model.compile(optimizer='adam', loss='mse')
    return model

async def predict_price(df):
    try:
        scaler = MinMaxScaler()
        scaled_data = scaler.fit_transform(df['close'].values.reshape(-1, 1))
        X = np.array([scaled_data[i-50:i] for i in range(50, len(scaled_data))])
        y = scaled_data[50:]
        model = create_lstm_model()
        model.fit(X, y, epochs=5, batch_size=32, verbose=0)
        last_sequence = scaled_data[-50:].reshape(1, 50, 1)
        predicted_scaled = model.predict(last_sequence, verbose=0)
        predicted_price = scaler.inverse_transform(predicted_scaled)[0][0]
        return predicted_price
    except Exception as e:
        logger.error(f"Error predicting price: {e}")
        return df['close'].iloc[-1]

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
            save_state()

        profit_percent = ((total_value_usd - daily_start_capital_usd) / daily_start_capital_usd * 100) if daily_start_capital_usd > 0 else 0

        if last_total_value_usd is None or abs(total_value_usd - last_total_value_usd) > 0.01:
            msg = f"💰 Tổng tài sản: {total_value_usd:.2f} USD\n💵 USDT: {usdt:.2f}\n"
            for currency, data in coins.items():
                if data['value_usd'] > 0.1:
                    msg += f"🪙 {currency}: {data['balance']:.4f} | Giá: {data['price']:.4f} | Giá trị: {data['value_usd']:.2f} USD\n"
            msg += f"📈 Lợi nhuận hôm nay: {profit_percent:.2f}%"
            await send_telegram(msg)
            last_total_value_usd = total_value_usd

        return total_value_usd, profit_percent
    except Exception as e:
        logger.error(f"Error logging assets: {e}")
        await send_telegram(f"❌ Lỗi log tài sản: {str(e)}")
        return None, None

async def trade_coin(exchange, symbol):
    global active_orders, lowest_prices, market_conditions
    try:
        # Check market conditions
        if symbol not in market_conditions:
            market_conditions[symbol] = await check_market_conditions(exchange, symbol)
        if not market_conditions[symbol]:
            return

        # Get lowest price in last 7 days
        if symbol not in lowest_prices:
            lowest_price = await get_lowest_price_7d(exchange, symbol)
            if lowest_price is None:
                return
            lowest_prices[symbol] = lowest_price

        # Fetch real-time data
        df_1m = await fetch_ohlcv(exchange, symbol, '1m', limit=50)
        if df_1m is None:
            return

        # Avoid pump-and-dump
        if detect_pump_dump(df_1m):
            await send_telegram(f"⚠️ Phát hiện pump-and-dump trên {symbol}. Bỏ qua giao dịch.")
            return

        ticker = await exchange.fetch_ticker(symbol)
        current_price = ticker['last']
        predicted_price = await predict_price(df_1m)

        # Dynamic take-profit
        now = datetime.now(timezone(timedelta(hours=7)))
        hours_since_midnight = now.hour + now.minute / 60
        take_profit = TAKE_PROFIT_PERCENT if hours_since_midnight < 12 else 2.0  # 2.5% trước 12h, 2% sau 12h

        # Buy logic
        if symbol not in active_orders:
            if is_near_lowest_price(current_price, lowest_prices[symbol], threshold=0.05) and \
               df_1m['rsi14'].iloc[-1] < 25 and \
               predicted_price > current_price * 1.025:  # Tăng >2.5%
                balance = await exchange.fetch_balance()
                usdt = float(balance['total'].get('USDT', 0.0))
                total_value_usd, _ = await log_assets(exchange)
                if total_value_usd is None or usdt < 1.0:
                    return

                trade_amount_usdt = usdt * RISK_PER_TRADE
                amount = trade_amount_usdt / current_price
                if not await check_liquidity(exchange, symbol, amount):
                    await send_telegram(f"⚠️ Thanh khoản thấp trên {symbol}. Bỏ qua giao dịch.")
                    return

                order = await exchange.create_market_buy_order(symbol, amount)
                coin = symbol.split('/')[0]
                balance = await exchange.fetch_balance()
                actual_amount = float(balance['total'].get(coin, 0.0))

                atr = df_1m['atr'].iloc[-1]
                stop_loss_price = current_price * (1 + STOP_LOSS_PERCENT / 100)
                take_profit_price = current_price * (1 + take_profit / 100)

                active_orders[symbol] = {
                    'buy_price': current_price,
                    'amount': actual_amount,
                    'usdt': trade_amount_usdt,
                    'stop_loss': stop_loss_price,
                    'take_profit': take_profit_price
                }

                await send_telegram(
                    f"🟢 Mua {symbol}: {actual_amount:.4f} coin | Giá: {current_price:.4f} | "
                    f"Tổng: {trade_amount_usdt:.2f} USDT | SL: {stop_loss_price:.4f} | TP: {take_profit_price:.4f}"
                )

        # Sell logic
        elif symbol in active_orders:
            order = active_orders[symbol]
            profit_percent = ((current_price - order['buy_price']) / order['buy_price']) * 100

            coin = symbol.split('/')[0]
            balance = await exchange.fetch_balance()
            coin_balance = float(balance['total'].get(coin, 0.0))
            amount = min(order['amount'], coin_balance)

            should_sell = (
                profit_percent >= take_profit or
                current_price <= order['stop_loss'] or
                predicted_price < current_price * 0.975  # Giảm >2.5%
            )

            if should_sell:
                if await check_liquidity(exchange, symbol, amount):
                    order = await exchange.create_market_sell_order(symbol, amount)
                    profit_usdt = (current_price - order['buy_price']) * amount
                    await send_telegram(
                        f"🔴 Bán {symbol}: {amount:.4f} coin | Giá: {current_price:.4f} | "
                        f"Lợi nhuận: {profit_percent:.2f}% ({profit_usdt:.2f} USDT)"
                    )
                    del active_orders[symbol]
                else:
                    await send_telegram(f"⚠️ Thanh khoản thấp khi bán {symbol}. Giữ lệnh.")

    except Exception as e:
        error_msg = str(e)
        if "51008" in error_msg and symbol in active_orders:
            balance = await exchange.fetch_balance()
            coin = symbol.split('/')[0]
            coin_balance = float(balance['total'].get(coin, 0.0))
            if coin_balance > 0:
                order = await exchange.create_market_sell_order(symbol, coin_balance)
                await send_telegram(
                    f"⚠️ Lỗi 51008 khi bán {symbol}: Số dư không đủ. Đã bán {coin_balance:.4f} coin."
                )
            del active_orders[symbol]
        else:
            await send_telegram(f"❌ Lỗi giao dịch {symbol}: {error_msg}")
            logger.error(f"Error trading {symbol}: {e}")

def is_near_lowest_price(current_price, lowest_price, threshold=0.05):
    return current_price <= lowest_price * (1 + threshold)

async def trade_all_coins(exchange):
    total_value_usd, profit_percent = await log_assets(exchange)
    if profit_percent is not None and profit_percent >= DAILY_PROFIT_TARGET:
        await send_telegram("🎯 Đã đạt mục tiêu lợi nhuận 5% hôm nay. Tạm dừng giao dịch.")
        return

    tasks = [trade_coin(exchange, symbol) for symbol in SYMBOLS]
    await asyncio.gather(*tasks)

async def runner():
    global daily_start_capital_usd
    keep_alive()
    exchange = create_exchange()

    # Load state from file
    load_state()

    # If no state or new day, fetch wallet balance
    now = datetime.now(timezone(timedelta(hours=7)))
    today = now.date()
    if daily_start_capital_usd is None or last_day is None or today != last_day:
        total_value_usd = await fetch_wallet_balance(exchange)
        if total_value_usd is not None:
            daily_start_capital_usd = total_value_usd
            last_day = today
            save_state()
            await send_telegram(f"🤖 Bot khởi động! Vốn ví: {daily_start_capital_usd:.2f} USDT | Mục tiêu: 5%/ngày")
        else:
            await send_telegram("❌ Lỗi lấy số dư ví. Dừng bot.")
            return
    else:
        await send_telegram(f"🤖 Bot khởi động! Vốn ví: {daily_start_capital_usd:.2f} USDT | Mục tiêu: 5%/ngày")
    
    schedule.every(30).seconds.do(lambda: asyncio.ensure_future(trade_all_coins(exchange)))
    schedule.every(5).minutes.do(lambda: asyncio.ensure_future(log_assets(exchange)))
    while True:
        schedule.run_pending()
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(runner())
