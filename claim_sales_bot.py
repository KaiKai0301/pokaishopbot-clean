import os 
import logging 
import telebot 
 
logging.basicConfig(level=logging.INFO) 
logger = logging.getLogger(__name__) 
 
token = os.environ.get('BOT_TOKEN') 
if not token: 
    logger.error("? ERROR: BOT_TOKEN environment variable not set") 
    logger.error("?? Set BOT_TOKEN in Render environment variables") 
    exit(1) 
 
bot = telebot.TeleBot(token) 
 
@bot.message_handler(commands=['start', 'help']) 
def send_welcome(message): 
    bot.reply_to(message, "?? PokaiShop Bot is running!") 
 
@bot.message_handler(func=lambda message: True) 
def echo_all(message): 
    bot.reply_to(message, "I received: " + message.text) 
 
if __name__ == '__main__': 
    logger.info("?? Starting PokaiShop Bot...") 
    bot.polling(non_stop=True) 
