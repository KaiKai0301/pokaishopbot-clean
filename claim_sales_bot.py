import os 
import requests 
from flask import Flask 
from telegram.ext import Application, CommandHandler, MessageHandler, filters 
import logging 
 
logging.basicConfig(level=logging.INFO) 
logger = logging.getLogger(__name__) 
 
app = Flask(__name__) 
 
async def start(update, context): 
    await update.message.reply_text('?? PokaiShop Bot is running!') 
 
def main(): 
    token = os.environ.get('BOT_TOKEN') 
    if not token: 
        logger.error('No BOT_TOKEN found') 
        return 
 
    application = Application.builder().token(token).build() 
    application.add_handler(CommandHandler('start', start)) 
 
    # Start Flask web server 
    flask_thread = threading.Thread(target=lambda: app.run(host='0.0.0.0', port=5000)) 
    flask_thread.daemon = True 
    flask_thread.start() 
 
    # Start bot 
    application.run_polling() 
 
if __name__ == '__main__': 
    main() 
