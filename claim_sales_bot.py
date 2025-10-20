import os 
import logging 
from telegram.ext import Updater, CommandHandler 
from flask import Flask 
 
logging.basicConfig\(level=logging.INFO\) 
logger = logging.getLogger\(__name__\) 
 
app = Flask\(__name__\) 
 
@app.route\(\'/\'\) 
def home\(\): 
    return \'?? PokaiShop Bot is running!\' 
 
def start_command\(update, context\): 
    update.message.reply_text\(\'?? PokaiShop Bot is working!\'\) 
 
def main\(\): 
    token = os.environ.get\(\'BOT_TOKEN\'\) 
    if not token: 
        logger.error\(\'? BOT_TOKEN not set\'\) 
        return 
 
    updater = Updater\(token, use_context=True\) 
    dispatcher = updater.dispatcher 
    dispatcher.add_handler\(CommandHandler\(\'start\', start_command\)\) 
 
    logger.info\(\'?? Starting bot...\'\) 
    updater.start_polling\(\) 
    updater.idle\(\) 
 
if __name__ == \'__main__\': 
    main\(\) 
