import os 
import logging 
import telebot 
from flask import Flask 
from threading import Thread 
 
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s') 
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
 
    logger.info("? Bot token found, creating bot...") 
    bot = telebot.TeleBot(token) 
 
    # DELETE ANY EXISTING WEBHOOK FIRST 
    logger.info("?? Deleting any existing webhook...") 
    try: 
        bot.delete_webhook() 
        logger.info("? Webhook deleted successfully") 
    except Exception as e: 
        logger.error(f"? Error deleting webhook: {e}") 
 
    @bot.message_handler(commands=['start']) 
    def start_message(message): 
        logger.info(f"?? Received /start from {message.from_user.id}") 
        bot.reply_to(message, "?? PokaiShop Bot is working! ??") 
 
    logger.info("?? Starting bot polling...") 
    bot.polling(non_stop=True) 
 
if __name__ == '__main__': 
    # Start bot in a separate thread 
    bot_thread = Thread(target=run_bot) 
    bot_thread.daemon = True 
    bot_thread.start() 
 
    # Start Flask server in main thread 
    logger.info("?? Starting Flask server...") 
    app.run(host='0.0.0.0', port=5000, debug=False) 
