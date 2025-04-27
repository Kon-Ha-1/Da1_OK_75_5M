import ccxt.async_support as ccxt
import asyncio
import pandas as pd
import os
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

SYMBOL = "DOGE/USDT"
TIMEFRAME = "5m"
TP_PERCENT = 0.03
SL_PERCENT = 0.015
RISK_PER_TRADE = 0.05

bot = Bot(token=TELEGRAM_TOKEN)
nest_asyncio.apply()

trade_memory = {}

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

async def fetch_ohlcv(exchange):
    try:
        data = await exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=100)
        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert('Asia/Ho_Chi_Minh')
        
        df['ema_fast'] = df['close'].ewm(span=5, adjust=False).mean()
        df['ema_slow'] = df['close'].ewm(span=12, adjust=False).mean()
        df['ema_big'] = df['close'].ewm(span=30, adjust=False).mean()
        df['rsi14'] = compute_rsi(df['close'], 14)
        df['volume_ma'] = df['volume'].rolling(10).mean()  # Sửa dòng này
        
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
    df = await fetch_ohlcv(ex)
    if df is None:
        await ex.close()
        return

    price = df['close'].iloc[-1]
    holding = trade_memory.get(SYMBOL)

    if holding:
        buy_price = holding['buy_price']
        amount = holding['amount']
        
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
    
    elif should_buy(df):
        try:
            balance = await ex.fetch_balance()
            usdt_balance = float(balance['USDT']['free'])
            if usdt_balance > 10:
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
    
    await ex.close()

async def log_portfolio():
    try:
        ex = create_exchange()
        balance = await ex.fetch_balance()
        usdt = float(balance['USDT']['total'])
        doge = float(balance['DOGE']['total'])
        ticker = await ex.fetch_ticker(SYMBOL)
        price = ticker['last']
        total_value = usdt + (doge * price)
        
        await send_telegram(
            f"📊 Báo cáo tài sản\n"
            f"🪙 DOGE: {doge:.0f} | Giá hiện tại: {price:.4f}\n"
            f"💵 USDT: {usdt:.2f}\n"
            f"💰 Tổng: {total_value:.2f} USDT"
        )
        await ex.close()
    except Exception as e:
        await send_telegram(f"❌ Lỗi log_portfolio: {str(e)}")

async def runner():
    keep_alive()
    await send_telegram("🤖 Bot DOGE/USDT đã khởi động!")
    schedule.every(1).minutes.do(lambda: asyncio.ensure_future(analyze_and_trade()))
    schedule.every(15).minutes.do(lambda: asyncio.ensure_future(log_portfolio()))
    while True:
        schedule.run_pending()
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(runner())
