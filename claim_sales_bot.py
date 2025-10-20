import os 
import logging 
import telebot 
from flask import Flask, request 
 
logging.basicConfig(level=logging.INFO) 
logger = logging.getLogger(__name__) 
 
app = Flask(__name__) 
bot = telebot.TeleBot(os.environ.get('BOT_TOKEN')) 
 
@app.route('/') 
def home(): 
    return '?? PokaiShop Bot is running!' 
 
@app.route('/webhook', methods=['POST']) 
def webhook(): 
    if request.headers.get('content-type') == 'application/json': 
        json_string = request.get_data().decode('utf-8') 
        update = telebot.types.Update.de_json(json_string) 
        bot.process_new_updates([update]) 
        return 'OK' 
 
@bot.message_handler(commands=['start']) 
def start_message(message): 
    bot.reply_to(message, "?? PokaiShop Bot is running with webhooks!") 
 
if __name__ == '__main__': 
    # Remove any existing webhook 
    bot.remove_webhook() 
    # Set webhook for production 
    bot.set_webhook(url='https://your-render-url.onrender.com/webhook') 
    logger.info("?? Bot started with webhooks") 
    app.run(host='0.0.0.0', port=5000) 
