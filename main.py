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
API_KEY = "99d39d59-c05d-4e40-9f2a-3615eac315ea"
API_SECRET = "4B1D25C8F05E12717AD561584B2853E6"
PASSPHRASE = "Mmoarb2025@"
TELEGRAM_TOKEN = "7817283052:AAF2fjxxZT8LP-gblBeTbpb0N0-a0C7GLQ8"
TELEGRAM_CHAT_ID = "5850622014"

 SYMBOLS = ["DOGE/USDT"]
 TIMEFRAME = "1m"
 
 bot = Bot(token=TELEGRAM_TOKEN)
 nest_asyncio.apply()
 
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
 
 def fetch_ohlcv(exchange, symbol):
     try:
         data = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=100)
         df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
         df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert('Asia/Ho_Chi_Minh')
         df['ema_fast'] = df['close'].ewm(span=9, adjust=False).mean()
         df['ema_slow'] = df['close'].ewm(span=21, adjust=False).mean()
         delta = df['close'].diff()
         gain = delta.where(delta > 0, 0.0)
         loss = -delta.where(delta < 0, 0.0)
         avg_gain = gain.rolling(window=14).mean()
         avg_loss = loss.rolling(window=14).mean()
         rs = avg_gain / avg_loss
         df['rsi'] = 100 - (100 / (1 + rs))
         ema12 = df['close'].ewm(span=12, adjust=False).mean()
         ema26 = df['close'].ewm(span=26, adjust=False).mean()
         df['macd'] = ema12 - ema26
         df['signal'] = df['macd'].ewm(span=9, adjust=False).mean()
         return df
     except Exception as e:
         print(f"[OHLCV Error] {symbol}: {e}")
         return None
 
 async def analyze_all_symbols():
     ex = create_exchange()
     full_report = "\n📊 PHÂN TÍCH TỔNG HỢP:\n"
 
     for symbol in SYMBOLS:
         df = fetch_ohlcv(ex, symbol)
         if df is None:
             continue
 
         now = pd.Timestamp.now(tz='Asia/Ho_Chi_Minh')
         today = now.normalize()
 
         last_hours_df = df[df['timestamp'] > now - pd.Timedelta(hours=6)]
         today_df = df[df['timestamp'] > today]
 
         current_price = df['close'].iloc[-1]
         min_today = today_df['low'].min()
         max_today = today_df['high'].max()
         min_6h = last_hours_df['low'].min()
         max_6h = last_hours_df['high'].max()
         open_today = today_df['open'].iloc[0] if not today_df.empty else df['open'].iloc[0]
         change_today = (current_price - open_today) / open_today * 100 if open_today else 0
 
         near = ""
         if current_price <= min_today * 1.01:
             near = "🌑 Gần đáy ngày"
         elif current_price >= max_today * 0.99:
             near = "☀️ Gần đỉnh ngày"
 
         ema_fast = df['ema_fast'].iloc[-1]
         ema_slow = df['ema_slow'].iloc[-1]
         trend_ok = ema_fast > ema_slow
 
         rsi = df['rsi'].iloc[-1]
         rsi_ok = 45 <= rsi <= 75
 
         macd = df['macd'].iloc[-1]
         signal = df['signal'].iloc[-1]
         macd_cross_up = macd > signal and df['macd'].iloc[-2] < df['signal'].iloc[-2]
 
         recent_slopes = df['close'].diff().tail(6)
         avg_slope = recent_slopes.mean()
         if avg_slope > 0:
             predict = "🚀 Dự đoán: giá sắp tăng"
         elif avg_slope < 0:
             predict = "🔻 Dự đoán: giá sắp giảm"
         else:
             predict = "⏳ Dự đoán: đi ngang"
 
         score = 0
         if trend_ok: score += 1
         if rsi_ok: score += 1
         if macd_cross_up: score += 1
         if near == "🌑 Gần đáy ngày": score += 1
 
         if score == 4:
             probability = "🔵 Xác suất cao: 90-95% @hakutecucxuc"
             suggest = "✅ GỢI Ý MUA"
         elif score == 3:
             probability = "🟡 Xác suất vừa: 75-80% @hakutecucxuc"
             suggest = "🕒 CÂN NHẮC"
         else:
             probability = "🔴 Xác suất thấp: <60%"
             suggest = "❌ CHỜ"
 
         full_report += (
             f"\n🪙 {symbol}\n"
             f"- Giá: ${current_price:.4f}\n"
             f"- Biến động hôm nay: {change_today:.2f}%\n"
             f"- 6h: Min={min_6h:.4f}, Max={max_6h:.4f}\n"
             f"- EMA: {'Bullish ✅' if trend_ok else 'Bearish ❌'}\n"
             f"- RSI: {rsi:.2f} {'✅' if rsi_ok else '❌'}\n"
             f"- MACD: {'✅ Cắt lên' if macd_cross_up else '❌ Chưa cắt lên'}\n"
             f"- {near if near else 'Giá trung bình ngày'}\n"
             f"- {predict}\n"
             f"- {probability}\n"
             f"👉 {suggest}\n"
         )
 
     await send_telegram(full_report)
 
 async def runner():
     keep_alive()
     await send_telegram("🤖 Bot phân tích Doge coin đã khởi động!")
     schedule.every(1).minutes.do(lambda: asyncio.ensure_future(analyze_all_symbols()))
 
     while True:
         schedule.run_pending()
         await asyncio.sleep(1)
 
 if __name__ == "__main__":
     asyncio.run(runner())
