import os 
import logging 
import telebot 
from flask import Flask 
import threading 
 
logging.basicConfig(level=logging.INFO) 
logger = logging.getLogger(__name__) 
 
app = Flask(__name__) 
 
@app.route('/') 
def home(): 
    return '?? PokaiShop Bot is running!' 
 
def run_bot(): 
    token = os.environ.get('BOT_TOKEN') 
    if not token: 
        logger.error("? BOT_TOKEN not set") 
        return 
 
    bot = telebot.TeleBot(token) 
 
    @bot.message_handler(commands=['start']) 
    def start_message(message): 
        bot.reply_to(message, "?? PokaiShop Bot is running!") 
 
    logger.info("?? Starting Telegram Bot...") 
    bot.polling(non_stop=True) 
 
if __name__ == '__main__': 
    # Start bot in a separate thread 
    bot_thread = threading.Thread(target=run_bot) 
    bot_thread.daemon = True 
    bot_thread.start() 
 
    # Start Flask app (for Render port detection) 
    app.run(host='0.0.0.0', port=5000) 
