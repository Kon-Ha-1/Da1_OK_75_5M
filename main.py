import ccxt
import asyncio
import pandas as pd
import os
from datetime import datetime, timezone, timedelta
import schedule
import nest_asyncio
from telegram import Bot
from keep_alive import keep_alive

# === CONFIG ===
API_KEY = "YOUR_API_KEY"  # Thay bằng API key thật
API_SECRET = "YOUR_API_SECRET"  # Thay bằng API secret thật
PASSPHRASE = "YOUR_PASSPHRASE"  # Nếu có
TELEGRAM_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID = "YOUR_CHAT_ID"

SYMBOL = "DOGE/USDT"
TIMEFRAME = "5m"  # Dùng khung 5 phút để giảm false signal
TP_PERCENT = 0.03  # Take Profit 3% (giảm từ 4% để an toàn)
SL_PERCENT = 0.015  # Stop Loss 1.5% (giảm từ 2%)
RISK_PER_TRADE = 0.05  # Chỉ rủi ro 5% vốn/lệnh (thay vì 15%)

bot = Bot(token=TELEGRAM_TOKEN)
nest_asyncio.apply()

trade_memory = {}  # Lưu trạng thái lệnh

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

def fetch_ohlcv(exchange):
    try:
        data = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=100)
        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert('Asia/Ho_Chi_Minh')
        
        # Tính chỉ báo
        df['ema_fast'] = df['close'].ewm(span=5, adjust=False).mean()
        df['ema_slow'] = df['close'].ewm(span=12, adjust=False).mean()
        df['ema_big'] = df['close'].ewm(span=30, adjust=False).mean()
        df['rsi14'] = compute_rsi(df['close'], 14)
        df['volume_ma'] = df['volume'].rolling(10).mean()
        
        # MACD
        ema12 = df['close'].ewm(span=12, adjust=False).mean()
        ema26 = df['close'].ewm(span=26, adjust=False).mean()
        df['macd'] = ema12 - ema26
        df['signal'] = df['macd'].ewm(span=9, adjust=False).mean()
        
        return df
    except Exception as e:
        print(f"[OHLCV Error] {e}")
        return None

def compute_rsi(series, period):
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def should_buy(df):
    last_candle = df.iloc[-1]
    prev_candle = df.iloc[-2]
    
    # Điều kiện mua:
    # 1. EMA5 > EMA12 > EMA30 (xu hướng tăng)
    # 2. RSI14 < 65 (tránh quá mua)
    # 3. Volume hiện tại > trung bình 10 nến
    # 4. MACD cắt lên Signal line
    return (
        last_candle['ema_fast'] > last_candle['ema_slow'] and
        last_candle['ema_slow'] > last_candle['ema_big'] and
        last_candle['rsi14'] < 65 and
        last_candle['volume'] > last_candle['volume_ma'] and
        last_candle['macd'] > last_candle['signal'] and
        prev_candle['macd'] <= prev_candle['signal']
    )

async def analyze_and_trade():
    ex = create_exchange()
    df = fetch_ohlcv(ex)
    if df is None:
        return

    price = df['close'].iloc[-1]
    holding = trade_memory.get(SYMBOL)

    if holding:
        buy_price = holding['buy_price']
        amount = holding['amount']
        
        # Check Take Profit
        if price >= buy_price * (1 + TP_PERCENT):
            try:
                await ex.create_market_sell_order(SYMBOL, amount)
                profit_usdt = (price - buy_price) * amount
                await send_telegram(
                    f"✅ TP BÁN {amount:.0f} DOGE\n"
                    f"💰 Lợi nhuận: +{profit_usdt:.2f} USDT ({TP_PERCENT*100}%)\n"
                    f"⏰ Giờ: {datetime.now().strftime('%H:%M:%S')}"
                )
                trade_memory.pop(SYMBOL)
            except Exception as e:
                await send_telegram(f"❌ Lỗi khi TP SELL: {e}")
        
        # Check Stop Loss
        elif price <= buy_price * (1 - SL_PERCENT):
            try:
                await ex.create_market_sell_order(SYMBOL, amount)
                loss_usdt = (buy_price - price) * amount
                await send_telegram(
                    f"🛑 SL CẮT LỖ {amount:.0f} DOGE\n"
                    f"💸 Lỗ: -{loss_usdt:.2f} USDT ({SL_PERCENT*100}%)\n"
                    f"⏰ Giờ: {datetime.now().strftime('%H:%M:%S')}"
                )
                trade_memory.pop(SYMBOL)
            except Exception as e:
                await send_telegram(f"❌ Lỗi khi SL SELL: {e}")
    
    # Tín hiệu mua
    elif should_buy(df):
        try:
            balance = ex.fetch_balance()
            usdt_balance = float(balance['USDT']['free'])
            if usdt_balance > 10:  # Ít nhất $10 để giao dịch
                amount = round((usdt_balance * RISK_PER_TRADE) / price, 0)
                if amount > 0:
                    order = await ex.create_market_buy_order(SYMBOL, amount)
                    avg_price = order['average'] or price
                    trade_memory[SYMBOL] = {
                        'buy_price': avg_price,
                        'amount': amount,
                        'timestamp': datetime.now().isoformat()
                    }
                    await send_telegram(
                        f"🚀 MUA {amount:.0f} DOGE tại {avg_price:.4f}\n"
                        f"🎯 TP: {avg_price * (1 + TP_PERCENT):.4f} (+{TP_PERCENT*100}%)\n"
                        f"🔪 SL: {avg_price * (1 - SL_PERCENT):.4f} (-{SL_PERCENT*100}%)"
                    )
        except Exception as e:
            await send_telegram(f"❌ Lỗi khi BUY: {str(e)}")

async def log_portfolio():
    try:
        ex = create_exchange()
        balance = ex.fetch_balance()
        usdt = float(balance['USDT']['total'])
        doge = float(balance['DOGE']['total'])
        price = (await ex.fetch_ticker(SYMBOL))['last']
        total_value = usdt + (doge * price)
        
        await send_telegram(
            f"📊 Báo cáo tài sản\n"
            f"🪙 DOGE: {doge:.0f} | Giá hiện tại: {price:.4f}\n"
            f"💵 USDT: {usdt:.2f}\n"
            f"💰 Tổng: {total_value:.2f} USDT"
        )
    except Exception as e:
        await send_telegram(f"❌ Lỗi log_portfolio: {str(e)}")

async def runner():
    keep_alive()
    await send_telegram("🤖 Bot DOGE/USDT đã khởi động!")
    schedule.every(1).minutes.do(lambda: asyncio.ensure_future(analyze_and_trade()))
    schedule.every(30).minutes.do(lambda: asyncio.ensure_future(log_portfolio()))
    while True:
        schedule.run_pending()
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(runner())
