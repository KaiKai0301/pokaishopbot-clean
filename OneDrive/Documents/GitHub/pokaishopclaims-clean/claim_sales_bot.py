# Health check to prevent spinning down on Render
try:
    # Try to import Flask
    from flask import Flask
    import threading
    import os
    
    app = Flask(__name__)
    
    @app.route('/')
    def home():
        return "Bot is running!"
    
    @app.route('/health')
    def health():
        return "OK", 200
    
    def run_flask():
        port = int(os.environ.get('PORT', 5000))
        app.run(host='0.0.0.0', port=port, debug=False)
    
    def start_health_check():
        flask_thread = threading.Thread(target=run_flask)
        flask_thread.daemon = True
        flask_thread.start()
        print("✅ Health check server started")
    
    # Start health check only if not in main thread
    if __name__ != "__main__":
        start_health_check()

except ImportError:
    print("⚠️ Flask not installed - health check disabled")
except Exception as e:
    print(f"❌ Health check failed: {e}")

import logging
import re
from datetime import datetime, timedelta
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, 
    CommandHandler, 
    MessageHandler, 
    ContextTypes, 
    filters,
    ConversationHandler,
    CallbackContext,
    JobQueue
)
import asyncio
import threading

# === GOOGLE SHEETS IMPORTS ===
import os
import json
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# === DEEPSEEK IMPORTS ===
import httpx

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# === FIXED GOOGLE SHEETS FUNCTION ===
def get_sheets_service():
    """Get Google Sheets service using environment variables or local files"""
    try:
        # Try environment variables first (for Render deployment)
        creds_json = os.environ.get('GOOGLE_CREDENTIALS')
        token_json = os.environ.get('GOOGLE_TOKEN')
        
        if creds_json and token_json:
            # Use environment variables
            logger.info("Using Google credentials from environment variables")
            creds_dict = json.loads(creds_json)
            token_dict = json.loads(token_json)
            
            creds = Credentials.from_authorized_user_info(token_dict, SCOPES)
            
            # Refresh token if expired
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                
            return build('sheets', 'v4', credentials=creds)
        
        else:
            # Fall back to local files (for local development)
            logger.info("Using Google credentials from local files")
            creds = None
            
            # Check if token.json exists locally
            if os.path.exists('token.json'):
                creds = Credentials.from_authorized_user_file('token.json', SCOPES)
            
            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                else:
                    # This will only work locally with credentials.json
                    flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
                    creds = flow.run_local_server(port=0)
                
                # Save the credentials for the next run
                with open('token.json', 'w') as token:
                    token.write(creds.to_json())
            
            return build('sheets', 'v4', credentials=creds)
            
    except Exception as e:
        logger.error(f"Error getting sheets service: {e}")
        return None

# Thread-safe dictionaries to store data
class ThreadSafeDict:
    def __init__(self):
        self._data = {}
        self._lock = threading.Lock()
    
    def __getitem__(self, key):
        with self._lock:
            return self._data.get(key)
    
    def __setitem__(self, key, value):
        with self._lock:
            self._data[key] = value
    
    def __delitem__(self, key):
        with self._lock:
            if key in self._data:
                del self._data[key]
    
    def get(self, key, default=None):
        with self._lock:
            return self._data.get(key, default)
    
    def keys(self):
        with self._lock:
            return list(self._data.keys())
    
    def items(self):
        with self._lock:
            return list(self._data.items())
    
    def __contains__(self, key):
        with self._lock:
            return key in self._data

class ThreadSafeConversationMemory:
    def __init__(self):
        self._memories = ThreadSafeDict()
        self._max_history = 12  # Keep last 12 messages per user
    
    def add_message(self, user_id: int, role: str, content: str):
        """Add a message to user's conversation history"""
        if user_id not in self._memories:
            self._memories[user_id] = []
        
        history = self._memories[user_id]
        history.append({"role": role, "content": content})
        
        # Keep only the most recent messages
        if len(history) > self._max_history:
            self._memories[user_id] = history[-self._max_history:]
    
    def get_history(self, user_id: int) -> list:
        """Get user's conversation history"""
        return self._memories.get(user_id, [])
    
    def clear_history(self, user_id: int):
        """Clear user's conversation history"""
        if user_id in self._memories:
            del self._memories[user_id]

# Initialize conversation memory
conversation_memory = ThreadSafeConversationMemory()

async def ensure_headers(service):
    """Ensure headers exist in the sheet with simplified columns."""
    try:
        range_name = "Claims Data!A1:F1"
        result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range=range_name
        ).execute()
        values = result.get('values', [])
        
        if not values:
            headers = [
                'Timestamp', 
                'Item Name', 
                'Price', 
                'Available Quantity',
                'Sold Quantity', 
                'Post Type'
            ]
            
            body = {'values': [headers]}
            service.spreadsheets().values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=range_name,
                valueInputOption='RAW',
                body=body
            ).execute()
            logger.info("✅ Simplified headers added to Google Sheets")
            
    except Exception as e:
        logger.error(f"Error ensuring headers: {e}")

async def record_post_immediately(message_id, text, post_type):
    """Record a post immediately when it's posted - ONLY for claim/multi/auction posts."""
    try:
        # ✅ FIX: Double-check that we should record this post type
        if post_type not in ["claim", "multi", "auction"]:
            logger.info(f"⏭️ Skipping Google Sheets recording for {post_type} post {message_id}")
            return
            
        logger.info(f"🔍 STARTING Google Sheets recording for {post_type} post {message_id}")
        
        # Get service with error handling
        try:
            service = get_sheets_service()
            logger.info(f"✅ Google Sheets service initialized for message {message_id}")
        except Exception as e:
            logger.error(f"❌ FAILED to get Google Sheets service: {e}")
            return
            
        try:
            await ensure_headers(service)
            logger.info(f"✅ Headers ensured for message {message_id}")
        except Exception as e:
            logger.error(f"❌ FAILED to ensure headers: {e}")
            return
        
        # Extract item name with error handling
        try:
            item_name = extract_item_name(text, post_type)
            logger.info(f"📝 Extracted item name: '{item_name}' for message {message_id}")
        except Exception as e:
            logger.error(f"❌ FAILED to extract item name: {e}")
            item_name = "Unknown Item"
        
        # ✅ FIXED: Better price extraction that works for all post types
        price = 0.0
        price_match = None
        
        if post_type == "auction":
            # For auctions, look for numbers that could be starting bids
            # Common auction formats: "10", "Starting bid: 10", "SB: 10", "10 SGD"
            price_match = re.search(r'(?:starting\s*bid|sb|bid)[:\s]*(\d+(?:\.\d{1,2})?)', text, re.IGNORECASE)
            if not price_match:
                # Look for standalone numbers that could be starting bids
                price_match = re.search(r'\b(\d+(?:\.\d{1,2})?)\s*(?:sgd|usd|dollars?)?\b', text)
        else:
            # For claim/multi posts, look for $ prices
            price_match = re.search(r'\$(\d+(?:\.\d{1,2})?)', text)
        
        if price_match:
            price = float(price_match.group(1))
            logger.info(f"💰 Extracted price: ${price} for message {message_id}")
        else:
            # If no price found, use 0 but still record the post
            logger.warning(f"⚠️ No price found in {post_type} post {message_id}, using default 0")
            price = 0.0
        
        # Extract available quantity
        quantity_match = re.search(r'(?:qty|quantity)[:\s]*(\d+)', text, re.IGNORECASE)
        available_quantity = int(quantity_match.group(1)) if quantity_match else 1
        logger.info(f"📦 Extracted quantity: {available_quantity} for message {message_id}")
        
        # For multi posts, use the extracted quantity
        if post_type == "multi" and not quantity_match:
            available_quantity = 10
            logger.info(f"Multi post {message_id}: No quantity specified, using default 10")
        
        # For auctions, quantity is typically 1
        if post_type == "auction":
            available_quantity = 1
        
        # Create simplified row
        row = [
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            item_name[:100],  # Limit item name length
            str(price),  # Convert to string to avoid float issues
            str(available_quantity),
            "0",  # Sold quantity starts at 0
            post_type
        ]
        
        logger.info(f"📊 Row data prepared: {row}")
        
        # Store the row number for future updates
        if message_id not in sheets_tracking:
            sheets_tracking[message_id] = {
                "post_type": post_type,
                "row_number": None,
                "available_quantity": available_quantity,
                "sold_quantity": 0,
                "original_available_quantity": available_quantity
            }
        
        # Append to sheet and get the row number
        body = {'values': [row]}
        logger.info(f"📤 Attempting to append to Google Sheets...")
        
        try:
            result = service.spreadsheets().values().append(
                spreadsheetId=SPREADSHEET_ID,
                range="Claims Data!A:F",
                valueInputOption='RAW',
                body=body,
                insertDataOption="INSERT_ROWS"
            ).execute()
            
            logger.info(f"✅ Google Sheets API call SUCCESSFUL")
            
        except Exception as e:
            logger.error(f"❌ GOOGLE SHEETS API CALL FAILED: {e}")
            logger.error(f"Full error details:", exc_info=True)
            return
        
        # Extract the row number from the response
        updates = result.get('updates', {})
        updated_range = updates.get('updatedRange', '')
        updated_rows = updates.get('updatedRows', 0)
        
        logger.info(f"📈 Update details - Range: {updated_range}, Rows: {updated_rows}")
        
        if updated_rows > 0:
            logger.info(f"🎉 SUCCESS: {updated_rows} row(s) updated in Google Sheets")
            # Parse the row number from the range (e.g., "Claims Data!A123:F123")
            row_match = re.search(r'!A(\d+)(?::|$)', updated_range)
            if row_match:
                row_number = int(row_match.group(1))
                sheets_tracking[message_id]["row_number"] = row_number
                logger.info(f"📝 Recorded {post_type} post in row {row_number}: {item_name} (Qty: {available_quantity})")
        else:
            logger.warning(f"⚠️ No rows were updated in Google Sheets")
        
        logger.info(f"✅ COMPLETED recording {post_type} post {message_id} in Google Sheets")
        
    except Exception as e:
        logger.error(f"❌ CRITICAL ERROR recording {post_type} post {message_id}: {str(e)}")
        logger.error(f"❌ Full error details:", exc_info=True)

def extract_item_name(text, post_type):
    """Extract item name based on post type with intelligent parsing."""
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    
    if not lines:
        return "Unknown Item"
    
    if post_type == "claim":
        # For claim posts: first line is usually the item name
        first_line = lines[0]
        # Remove price and quantity information
        item_name = re.sub(r'\$.*', '', first_line).strip()
        item_name = re.sub(r'(?i)(qty|quantity).*', '', item_name).strip()
        # Remove common prefixes/suffixes
        item_name = re.sub(r'^(?:for sale|selling|claim)\s*[:\-\s]*', '', item_name, flags=re.IGNORECASE).strip()
        
        if item_name:
            return item_name[:100]
        else:
            return lines[0][:100]  # Fallback to first line
    
    elif post_type == "multi":
        # For multi posts: look for item name in lines 2-4 (skip first "Multiple" line)
        item_name = ""
        
        # Skip the first line (usually "Multiple" or "Multi")
        content_lines = lines[1:4] if len(lines) > 1 else lines
        
        for line in content_lines:
            clean_line = line.strip()
            # Skip lines that are just numbers, prices, or quantity info
            if (re.match(r'^\d+$', clean_line) or 
                re.search(r'\$', clean_line) or
                re.search(r'(?i)(qty|quantity|multiple|multi)', clean_line)):
                continue
                
            # Remove any price information that might be in the same line
            clean_line = re.sub(r'\$.*', '', clean_line).strip()
            clean_line = re.sub(r'(?i)(qty|quantity).*', '', clean_line).strip()
            
            if clean_line and len(clean_line) > 2:  # Avoid very short lines
                if item_name:
                    item_name += " " + clean_line
                else:
                    item_name = clean_line
        
        if item_name:
            return item_name[:100]
        else:
            # Fallback: use second line or first meaningful line
            for line in lines[1:]:
                if line and len(line) > 2 and not re.search(r'\$', line):
                    return line[:100]
            return "Multiple Items"[:100]
    
    elif post_type == "auction":
        # For auction posts: look for item name after "Auction" in first few lines
        item_name = ""
        
        for i, line in enumerate(lines[:3]):  # Check first 3 lines
            clean_line = line.strip()
            # Look for lines that contain item description, not just "Auction" or pricing
            if (re.search(r'auction', clean_line, re.IGNORECASE) and 
                not re.match(r'^(auction|Auction)\s*$', clean_line)):
                # Extract text after "Auction"
                match = re.search(r'(?:auction|Auction)\s+(.+)', clean_line, re.IGNORECASE)
                if match:
                    candidate = match.group(1).strip()
                    # Remove price/RP/end time info
                    candidate = re.sub(r'(?:\$|RP|ends?|end time).*', '', candidate, flags=re.IGNORECASE).strip()
                    if candidate and len(candidate) > 2:
                        item_name = candidate
                        break
            
            # If line doesn't start with "Auction" but looks like an item description
            elif (i > 0 and  # Not the first line
                  not re.search(r'(?i)^(auction|ends?|\$|RP|qty|quantity)', clean_line) and
                  len(clean_line) > 3):
                item_name = clean_line
                # Remove any price information
                item_name = re.sub(r'\$.*', '', item_name).strip()
                break
        
        if item_name:
            return item_name[:100]
        else:
            # Fallback: find first meaningful line after skipping auction header
            for line in lines[1:]:
                if (line and len(line) > 3 and 
                    not re.search(r'(?i)(ends?|\$|RP|qty|quantity|^auction$)', line)):
                    return line[:100]
            return "Auction Item"[:100]
    
    else:
        # Fallback for unknown post types
        first_line = lines[0]
        first_line = re.sub(r'\$.*', '', first_line).strip()
        first_line = re.sub(r'(?i)(qty|quantity).*', '', first_line).strip()
        return first_line[:100] if first_line else "Item"[:100]


async def update_sold_quantity(message_id, sold_change=1):
    """Update the sold quantity for a post when items are claimed or unclaimed."""
    try:
        logger.info(f"🔄 Updating sold quantity for message {message_id}: change = {sold_change}")
        
        if message_id not in sheets_tracking:
            logger.warning(f"Message {message_id} not in sheets_tracking")
            return
        
        if not sheets_tracking[message_id].get("row_number"):
            logger.error(f"No row number found for message {message_id}")
            return
        
        service = get_sheets_service()
        row_number = sheets_tracking[message_id]["row_number"]
        track_data = sheets_tracking[message_id]
        
        # Update sold quantity in tracking
        current_sold = track_data.get("sold_quantity", 0)
        current_available = track_data.get("available_quantity", 0)
        original_available = track_data.get("original_available_quantity", current_sold + current_available)
        
        # ✅ FIX: For multi posts, use the total quantity from multi_claims if available
        if track_data.get("post_type") == "multi" and message_id in multiple_claims:
            multi_data = multiple_claims[message_id]
            original_available = multi_data.get("total_quantity", original_available)
            track_data["original_available_quantity"] = original_available
            logger.info(f"📦 Multi post detected, using total quantity: {original_available}")
        
        new_sold = current_sold + sold_change
        new_available = original_available - new_sold  # Calculate based on original total
        
        # Validate that quantities don't go negative
        if new_sold < 0:
            logger.warning(f"❌ Sold quantity would go negative ({new_sold}), clamping to 0")
            new_sold = 0
        
        if new_available < 0:
            logger.warning(f"❌ Available quantity would go negative ({new_available}), clamping to 0")
            new_available = 0
            
        # Validate consistency
        if new_sold + new_available != original_available:
            logger.warning(f"⚠️ Quantity mismatch: recalculating to maintain total {original_available}")
            new_sold = min(new_sold, original_available)
            new_available = original_available - new_sold
        
        track_data["sold_quantity"] = new_sold
        track_data["available_quantity"] = new_available
        
        logger.info(f"📊 Message {message_id}: Sold {current_sold} -> {new_sold}, Available {current_available} -> {new_available}")
        
        # Update sold quantity in Google Sheets (column E)
        range_name = f"Claims Data!E{row_number}"
        body = {'values': [[new_sold]]}
        service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=range_name,
            valueInputOption='RAW',
            body=body
        ).execute()
        
        # Update available quantity in Google Sheets (column D)
        range_name = f"Claims Data!D{row_number}"
        body = {'values': [[new_available]]}
        service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=range_name,
            valueInputOption='RAW',
            body=body
        ).execute()
        
        logger.info(f"✅ Updated quantities for post {message_id}: {new_sold} sold, {new_available} available")
        
    except Exception as e:
        logger.error(f"Error updating sold quantity for message {message_id}: {e}")


async def update_post_price(message_id, new_price):
    """Update the price for a post when offers/counter offers are accepted or auction ends."""
    try:
        if message_id not in sheets_tracking or not sheets_tracking[message_id].get("row_number"):
            logger.error(f"No row tracking found for message {message_id}")
            return
        
        service = get_sheets_service()
        row_number = sheets_tracking[message_id]["row_number"]
        
        # Update price in Google Sheets (column C)
        range_name = f"Claims Data!C{row_number}"
        body = {'values': [[new_price]]}
        service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=range_name,
            valueInputOption='RAW',
            body=body
        ).execute()
        
        logger.info(f"💰 Updated price to ${new_price:.2f} for post {message_id}")
        
    except Exception as e:
        logger.error(f"Error updating post price: {e}")

async def record_or_update_claim(post_type, message_id, user_id=None, claim_data=None, action="claim"):
    """
    Smart recording system that updates existing rows instead of creating new ones.
    
    Actions: claim, unclaim, offer_accept, counter_accept, auction_win
    """
    try:
        service = get_sheets_service()
        await ensure_headers(service)
        
        if post_type == "claim":
            await handle_claim_sheets_update(service, message_id, user_id, claim_data, action)
        elif post_type == "multi":
            await handle_multi_sheets_update(service, message_id, user_id, claim_data, action)
        elif post_type == "auction":
            await handle_auction_sheets_update(service, message_id, user_id, claim_data, action)
            
    except Exception as e:
        logger.error(f"Error in smart sheets update: {e}")

async def handle_claim_sheets_update(service, message_id, user_id, claim_data, action):
    """Simplified claim post updates - only track post-level data."""
    if action == "claim":
        # Update sold quantity when item is claimed
        await update_sold_quantity(message_id, 1)
    elif action == "unclaim":
        # Decrease sold quantity when item is unclaimed
        await update_sold_quantity(message_id, -1)
    elif action in ["offer_accept", "counter_accept"]:
        # Update price when offer/counter offer is accepted
        await update_post_price(message_id, claim_data['price'])

async def handle_multi_sheets_update(service, message_id, user_id, claim_data, action):
    """Simplified multi post updates - only track post-level data."""
    if action == "claim":
        # Update sold quantity when a number is claimed
        await update_sold_quantity(message_id, 1)
    elif action in ["offer_accept", "counter_accept"]:
        # Update price when offer/counter offer is accepted for a specific number
        await update_post_price(message_id, claim_data['price'])

async def handle_auction_sheets_update(service, message_id, user_id, claim_data, action):
    """Simplified auction recording - only track post-level data."""
    if action == "auction_win":
        # Update sold quantity and price when auction is won
        await update_sold_quantity(message_id, 1)
        await update_post_price(message_id, claim_data['price'])




async def update_payment_status_in_sheet(user_id):
    """Update payment status for all of a user's rows."""
    try:
        service = get_sheets_service()
        
        # Find all rows for this user
        range_name = "Claims Data!A:N"
        result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range=range_name
        ).execute()
        values = result.get('values', [])
        
        if not values:
            return
        
        user_data = user_claims.get(user_id, {})
        payment_status = user_data.get('payment_status', 'unpaid')
        
        # Skip header row
        for i, row in enumerate(values[1:], start=2):
            if len(row) > 1 and row[1] == str(user_id):  # User ID matches
                # Update payment status (column J)
                range_name = f"Claims Data!J{i}"
                body = {'values': [[payment_status]]}
                service.spreadsheets().values().update(
                    spreadsheetId=SPREADSHEET_ID,
                    range=range_name,
                    valueInputOption='RAW',
                    body=body
                ).execute()
                
        logger.info(f"Updated payment status to {payment_status} for user {user_id}")
        
    except Exception as e:
        logger.error(f"Error updating payment status: {e}")



# Dictionary to store user claims: {user_id: {"items": [], "total": 0, "username": "", "full_name": ""}}
user_claims = ThreadSafeDict()

# Dictionary to track which posts have been claimed: {message_id: {"user_id": user_id, "quantity": quantity}}
claimed_posts = ThreadSafeDict()

# Dictionary for waitlisted users: {message_id: [user_id1, user_id2, ...]}
waitlists = ThreadSafeDict()

# Dictionary for auction posts: {message_id: {"bids": {user_id: amount}, "end_time": datetime, "extended": bool, "active": bool}}
auction_posts = ThreadSafeDict()

# Dictionary for offer posts: {message_id: {"offers": {user_id: amount}, "counter_offers": {user_id: amount}}}
offer_posts = ThreadSafeDict()

# Dictionary to store user payment status: {user_id: {"paid": bool, "payment_time": datetime}}
payment_status = ThreadSafeDict()

# Dictionary for multiple claims: {message_id: {"claims": {1: user_id, 2: user_id, ...}, "waitlist": {1: [user_id1, user_id2, ...]}}}
multiple_claims = ThreadSafeDict()

# Dictionary to track Google Sheets row IDs for smart updates
sheets_tracking = ThreadSafeDict()  # {message_id: {"claims": {user_id: row_id}, "numbers": {claim_number: row_id}, "post_data": {}}}


# === ADD THIS RIGHT AFTER YOUR ADMIN_IDS DEFINITION ===

def admin_only(func):
    """Decorator to restrict commands to admins only."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user.id not in ADMIN_IDS:
            await update.message.reply_text("❌ Command not found.")
            return
        return await func(update, context)
    return wrapper

# Your existing code continues...
ADMIN_IDS = [
    5094180912,  # Your personal user ID
    -1002742177157,  # Your channel ID (negative number)
    -1002840189860,  # Your channel ID (negative number)
    -1002730121603,
    -1002970517345
]

# Admin user IDs and channel IDs (replace with your actual IDs)
ADMIN_IDS = [
    5094180912,  # Your personal user ID
    -1002742177157,  # Your channel ID (negative number)
    -1002840189860,  # Your channel ID (negative number)
    -1002730121603,
    -1002970517345

]

# Bot token (replace with your actual token)
TOKEN = "8456466564:AAFtPxI6RX_lR0AWYuv_8Gp0M61cPduE0Ps"

# Google Sheets configuration
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
SPREADSHEET_ID = '1UHoUClr3BLcm4jmEUHSPgXT9--G3r0GqKN9n0SMWUUc'  # Your actual ID
SHEET_NAME = 'Claims Data'
CREDENTIALS_FILE = 'credentials.json'

# === ADD DEEPSEEK CONFIGURATION ===
DEEPSEEK_API_KEY = "sk-00f9d66134fe4e26a56b6b9d692c751f"  # Get from https://platform.deepseek.com/
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"


# Conversation states
SHIPPING_METHOD, PAYMENT_CONFIRMATION, UNDO_SHIPPING = range(3)


def private_chat_only(func):
    """Decorator to restrict commands to private chats only."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type != "private":
            await update.message.reply_text("❌ Please contact me privately to use this command. We can't let daddy know our convo here :(")
            return
        return await func(update, context)
    return wrapper

@private_chat_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a welcome message when the command /start is issued."""
    user = update.effective_user
    
    if user.id in ADMIN_IDS:
        message = """
👋 Admin Mode Activated! 🔧

📋 User Commands:
/list - Your claimed items
/invoice - Your invoice total
/pay - Confirm payment
/shipping - Choose shipping method
/reset_my_claims - Reset your claims
/undo - Change shipping method

🔧 Admin Commands (Hidden from users):
/summary - View all claims
/reminder - Remind unpaid users
/reset_all_claims - Reset everyone's claims
/reset_user - Reset specific user's claims
/reset_status - Check reset status
/find_user - Find user in database
/setup_sheet - Reset Google Sheet data
/tracking - Add tracking info

Contact @kykaikai03 for queries :)
"""
    else:
        message = """
👋 Hello! I'm PokaiShop AI Assistant Intern! 🤖

You can just chat with me naturally! Try asking me:

💬 "What items did I claim?"
💬 "How much do I need to pay?" 
💬 "What are my shipping options?"
💬 "Tell me about Pokémon cards!"
💬 "Tell me more secrets about Daddy!"

Or use these quick commands:
📋 /list - Your claimed items
🧾 /invoice - Your invoice total
💳 /pay - Confirm payment
🚚 /shipping - Choose shipping method

I understand natural language - just talk to me! 😊

💳 Payment: Transfer to 98168898 within 12 hours or Daddy will smack your ass!
📦 Shipping: TikTok Mailer, Self-collect, or Storage
📞 Contact: Daddy @kykaikai03 for urgent help

Follow our TikTok: https://www.tiktok.com/@pokaishop5
"""
    
    await update.message.reply_text(message)

@private_chat_only
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a help message when the command /help is issued."""
    await start(update, context)  # Reuse the same logic as start

async def handle_claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle claim messages including multiple claims."""
    user = update.effective_user
    message = update.effective_message
    reply_to = message.reply_to_message

    # Check if the message is a reply to another message
    if not reply_to:
        return
    
    # Check if this is a multiple claim post
    text_to_search = reply_to.text or reply_to.caption or ""
    first_line = text_to_search.split('\n')[0].strip() if text_to_search else ""
    
    # Check if this is a multiple claim post
    if "multiple" in first_line.lower():
        await handle_multiple_claim(update, context)
        return

        # Check if this post was sold through a counter offer (no quantity left)
    if reply_to.message_id in claimed_posts and not isinstance(claimed_posts[reply_to.message_id], dict):
        await message.reply_text("❌ This item has been sold through a counter offer and is no longer available. Don't worry, Daddy will release more cards in the future!")
        return
    
    # Check if the message is a reply to another message
    if not reply_to:
        return
    
    # Check if this is an auction post
    auction_data = auction_posts.get(reply_to.message_id)
    if auction_data and auction_data.get("active", False):
        await message.reply_text("⏳ This is an auction post. Please place your bid with a number instead of 'claim'.")
        return
    
    # Check if there are active offers for this post
    offer_data = offer_posts.get(reply_to.message_id)
    if offer_data and offer_data.get("offers"):
        # Notify all users who made offers that the item has been claimed
        for offer_user_id in list(offer_data["offers"].keys()):
            try:
                await context.bot.send_message(
                    chat_id=offer_user_id,
                    text=f"❌ The item you made an offer on has been claimed by another person at the original price."
                )
            except Exception as e:
                logger.error(f"Error notifying offer user {offer_user_id}: {e}")
        
        # Clear all offers for this post
        offer_posts[reply_to.message_id] = {"offers": {}, "counter_offers": {}}
    
    # Extract quantity from the replied message (if any)
    quantity = 1
    text_to_search = reply_to.text or reply_to.caption or ""
    quantity_match = re.search(r'(?:qty|quantity)[:\s]*(\d+)', text_to_search, re.IGNORECASE)
    if quantity_match:
        quantity = int(quantity_match.group(1))
    
        # Check if this post has already been claimed or sold
    claim_data = claimed_posts.get(reply_to.message_id)
    if claim_data:
        # Special case: If a counter offer was taken but quantities remain
        if "counter_offer_price" in claim_data:
            # Check if there's remaining quantity
            if "quantity" in claim_data and claim_data["quantity"] > 0:
                # Use the counter offer price for remaining quantities
                price = claim_data["counter_offer_price"]
            else:
                # No quantity left, add to waitlist
                current_waitlist = waitlists.get(reply_to.message_id, [])
                if user.id not in current_waitlist:
                    current_waitlist.append(user.id)
                    waitlists[reply_to.message_id] = current_waitlist
            
                await message.reply_text(
                    "⏳ Item is currently fully claimed. You've been added to the *waitlist*. "
                    "Daddy will notify you if the card is available!"
                )
                return
    
        # If it's a quantity-based claim, check if any quantity remains
        elif "quantity" in claim_data:
            if claim_data["quantity"] > 0:
                # Quantity still available, process the claim below
                # Use original price from the post
                price_match = re.search(r'\$(\d+(?:\.\d{1,2})?)', text_to_search)
                if price_match:
                    price = float(price_match.group(1))
                else:
                    await message.reply_text("❌ Could not determine item price. Please contact admin.")
                    return
            else:
                # No quantity left, add to waitlist
                current_waitlist = waitlists.get(reply_to.message_id, [])
                if user.id not in current_waitlist:
                    current_waitlist.append(user.id)
                    waitlists[reply_to.message_id] = current_waitlist
            
                await message.reply_text(
                    "⏳ Item is currently fully claimed. You've been added to the *waitlist*. "
                    "You'll be notified if the card is available!"
                )
                return
        else:
            # Single item already claimed, add to waitlist
            current_waitlist = waitlists.get(reply_to.message_id, [])
            if user.id not in current_waitlist:
                current_waitlist.append(user.id)
                waitlists[reply_to.message_id] = current_waitlist
        
            await message.reply_text(
                "⏳ Item is currently fully claimed. You've been added to the *waitlist*. "
                "You'll be notified if the card is available!"
            )
            return
    
          # Extract price from the replied message - use counter offer price if available
    claim_data = claimed_posts.get(reply_to.message_id)
    counter_offer_price = None

    if claim_data and "counter_offer_price" in claim_data:
        # Use the counter offer price for remaining quantities
        counter_offer_price = claim_data["counter_offer_price"]
        price = counter_offer_price
    else:
        # Use original price from the post
        price_match = re.search(r'\$(\d+(?:\.\d{1,2})?)', text_to_search)
        if not price_match:
            return
        price = float(price_match.group(1))
    
    # Extract only the item name (first line without price/quantity)
    text_to_search = reply_to.text or reply_to.caption or ""
    item_text = ""

    # Get the first line and remove any price/quantity information
    lines = text_to_search.split('\n')
    if lines:
        first_line = lines[0].strip()
        # Remove price information if present
        item_text = re.sub(r'\$.*', '', first_line).strip()
        # Remove quantity information if present
        item_text = re.sub(r'(?i)(qty|quantity).*', '', item_text).strip()

    # If we ended up with nothing, use a shortened version of first line
    if not item_text and lines:
        item_text = lines[0].strip()[:50]

    # Final fallback
    if not item_text:
        item_text = "Item"
    
    # Initialize user data if not exists
    if user.id not in user_claims:
        user_claims[user.id] = {
            "items": [], 
            "total": 0, 
            "username": f"@{user.username}" if user.username else "No username",
            "full_name": user.first_name + (f" {user.last_name}" if user.last_name else ""),
            "payment_status": "unpaid",
            "shipping_method": None,
            "tracking_number": None
        }
    
        # Add claim to user's list
    user_data = user_claims[user.id]
    claim_data = {
        "text": item_text,
        "price": price,
        "message_id": reply_to.message_id,
        "timestamp": datetime.now(),
        "quantity": 1  # Each claim is for 1 item
    }
    
    # Mark if this was claimed at counter offer price
    if counter_offer_price is not None:
        claim_data["is_counter_offer"] = True
        claim_data["original_price"] = float(re.search(r'\$(\d+(?:\.\d{1,2})?)', text_to_search).group(1)) if re.search(r'\$(\d+(?:\.\d{1,2})?)', text_to_search) else price
    
    user_data["items"].append(claim_data)
    user_data["total"] += price
    
    # Mark post as claimed
    if quantity > 1:
        # For quantity posts, track remaining quantity
        if reply_to.message_id in claimed_posts:
            claimed_posts[reply_to.message_id]["quantity"] -= 1
        else:
            claimed_posts[reply_to.message_id] = {
                "user_id": user.id,
                "quantity": quantity - 1  # Remaining quantity
            }
    else:
        # For single items
        claimed_posts[reply_to.message_id] = {"user_id": user.id}
    
    # Send confirmation message
    user_name = user.first_name
    user_username = f"@{user.username}" if user.username else "No username"
    
    await message.reply_text(
        f"Claim confirmed for {user_name} | {user_username}!\n"
        f"@{context.bot.username} will guide you through your claims 😊\n\n"
        f"Please PM me /start to see the commands my daddy set for me!"
    )

        # Update sold quantity when item is claimed
    asyncio.create_task(update_sold_quantity(reply_to.message_id, 1))

    
    # Schedule a reminder for payment (in 12 hours)
    context.job_queue.run_once(
        payment_reminder, 
        timedelta(hours=12), 
        data=user.id, 
        name=f"payment_reminder_{user.id}"
    )

@private_chat_only
@admin_only
async def setup_sheet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to setup Google Sheets with simplified headers."""
    user = update.effective_user
    
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("This command is for admins only.")
        return
    
    try:
        service = get_sheets_service()
        
        # Clear existing data
        range_name = "Claims Data!A:Z"
        service.spreadsheets().values().clear(
            spreadsheetId=SPREADSHEET_ID,
            range=range_name,
            body={}
        ).execute()
        
        # Add simplified headers (only 6 columns)
        headers = [
            'Timestamp', 
            'Item Name', 
            'Price', 
            'Available Quantity',
            'Sold Quantity', 
            'Post Type'
        ]
        
        body = {'values': [headers]}
        service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range="Claims Data!A1:F1",  # Only A-F columns now
            valueInputOption='RAW',
            body=body
        ).execute()
        
        await update.message.reply_text("✅ Google Sheets setup complete! Simplified headers added and old data cleared.")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error setting up sheet: {e}")

@private_chat_only
@admin_only
async def recover_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to manually record a missing post."""
    user = update.effective_user
    
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("This command is for admins only.")
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /recover_post <message_id>")
        return
    
    try:
        message_id = int(context.args[0])
        
        # Try to get the message
        try:
            message = await context.bot.get_message(
                chat_id=update.effective_chat.id,
                message_id=message_id
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Could not find message {message_id}: {e}")
            return
        
        text = message.text or message.caption or ''
        post_type = detect_post_type(text)
        
        # Record the post
        await record_post_immediately(message_id, text, post_type)
        
        await update.message.reply_text(f"✅ Successfully recorded post {message_id} as {post_type}")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error recovering post: {e}")


@private_chat_only
@admin_only
async def debug_claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Debug command to test claim detection on a specific post."""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /debug_claim <message_id>")
        return
    
    try:
        message_id = int(context.args[0])
        
        # Check all tracking dictionaries
        debug_info = f"🔍 DEBUG CLAIM FOR MESSAGE {message_id}\n\n"
        
        # Check sheets tracking
        if message_id in sheets_tracking:
            debug_info += f"📊 Sheets Tracking: YES\n"
            debug_info += f"   Data: {sheets_tracking[message_id]}\n"
        else:
            debug_info += f"📊 Sheets Tracking: NO\n"
        
        # Check claimed posts
        if message_id in claimed_posts:
            debug_info += f"✅ Claimed Posts: YES\n"
            debug_info += f"   Data: {claimed_posts[message_id]}\n"
        else:
            debug_info += f"✅ Claimed Posts: NO\n"
        
        # Check multiple claims  
        if message_id in multiple_claims:
            debug_info += f"🔢 Multiple Claims: YES\n"
            debug_info += f"   Data: {multiple_claims[message_id]}\n"
        else:
            debug_info += f"🔢 Multiple Claims: NO\n"
            
        # Check auction posts
        if message_id in auction_posts:
            debug_info += f"🏆 Auction Posts: YES\n"
            debug_info += f"   Data: {auction_posts[message_id]}\n"
        else:
            debug_info += f"🏆 Auction Posts: NO\n"
            
        # Check offer posts
        if message_id in offer_posts:
            debug_info += f"💵 Offer Posts: YES\n"
            debug_info += f"   Data: {offer_posts[message_id]}\n"
        else:
            debug_info += f"💵 Offer Posts: NO\n"
        
        await update.message.reply_text(debug_info)
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

@private_chat_only
@admin_only
async def test_post_detection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Test if post detection is working."""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return
    
    # Get recent messages to test
    try:
        # This will get the last 10 messages in the chat
        messages = await update.effective_chat.get_messages(limit=10)
        
        debug_info = "🔍 POST DETECTION TEST\n\n"
        debug_info += f"Recent messages in chat:\n"
        
        for msg in messages:
            has_text = bool(msg.text or msg.caption)
            is_reply = bool(msg.reply_to_message)
            is_command = msg.text and msg.text.startswith('/') if msg.text else False
            
            debug_info += f"📝 Msg {msg.message_id}: "
            debug_info += f"Text: {has_text}, "
            debug_info += f"Reply: {is_reply}, "
            debug_info += f"Command: {is_command}, "
            debug_info += f"Tracked: {msg.message_id in sheets_tracking}\n"
            
            if has_text and not is_reply and not is_command:
                debug_info += f"   Content: {msg.text or msg.caption[:50]}...\n"
        
        await update.message.reply_text(debug_info)
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error testing post detection: {e}")

@private_chat_only
@admin_only
async def record_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually record a post that wasn't detected automatically."""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /record_manual <message_id>")
        return
    
    try:
        message_id = int(context.args[0])
        
        # Try to get the message
        try:
            message = await context.bot.get_message(
                chat_id=update.effective_chat.id,
                message_id=message_id
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Could not find message {message_id}: {e}")
            return
        
        text = message.text or message.caption or ''
        
        if not text:
            await update.message.reply_text(f"❌ Message {message_id} has no text content.")
            return
        
        post_type = detect_post_type(text)
        
        # Record the post
        await record_post_immediately(message_id, text, post_type)
        
        await update.message.reply_text(f"✅ Manually recorded post {message_id} as {post_type}")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error manually recording post: {e}")


@private_chat_only
@admin_only
async def debug_sheets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Debug why posts aren't being recorded in sheets."""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return
    
    message = update.effective_message
    if not message.reply_to_message:
        await update.message.reply_text("Please reply to a post to debug it.")
        return
    
    target = message.reply_to_message
    text = target.text or target.caption or ''
    
    debug_info = f"🔍 DEBUG POST {target.message_id}:\n\n"
    debug_info += f"Has text: {bool(text.strip())}\n"
    debug_info += f"Is reply: {bool(target.reply_to_message)}\n"
    debug_info += f"In auction_posts: {target.message_id in auction_posts}\n"
    debug_info += f"In sheets_tracking: {target.message_id in sheets_tracking}\n"
    debug_info += f"In multiple_claims: {target.message_id in multiple_claims}\n"
    
    if target.message_id in sheets_tracking:
        debug_info += f"Sheets data: {sheets_tracking[target.message_id]}\n"
    
    # Try to record it now
    try:
        post_type = detect_post_type(text)
        debug_info += f"\nPost type detected: {post_type}\n"
        
        if target.message_id not in sheets_tracking:
            await record_post_immediately(target.message_id, text, post_type)
            debug_info += "✅ FORCE RECORDED IN SHEETS!\n"
        else:
            debug_info += "⚠️ Already in sheets_tracking\n"
            
    except Exception as e:
        debug_info += f"❌ Error recording: {e}\n"
    
    await update.message.reply_text(debug_info)

@private_chat_only
@admin_only
async def reset_all_claims(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to reset ALL users' claims for new sales session."""
    user = update.effective_user
    
    # Check if we're in confirmation state
    if context.user_data.get('awaiting_reset_confirmation'):
        user_response = update.message.text.lower()
        
        if user_response in ['yes', 'y']:
            # User confirmed - proceed with reset
            context.user_data['awaiting_reset_confirmation'] = False
            
            # Count users with claims before reset
            users_with_claims = 0
            total_items = 0
            total_value = 0
            
            for user_id, data in user_claims.items():
                if data["items"]:
                    users_with_claims += 1
                    total_items += len(data["items"])
                    total_value += data["total"]
            
            # Reset all users but preserve their basic info
            reset_count = 0
            for user_id, data in user_claims.items():
                if data["items"]:  # Only reset users who actually have claims
                    username = data.get("username", "No username")
                    full_name = data.get("full_name", "Unknown")
                    
                    user_claims[user_id] = {
                        "items": [], 
                        "total": 0, 
                        "username": username,
                        "full_name": full_name,
                        "payment_status": "unpaid",
                        "shipping_method": None,
                        "tracking_number": None
                    }
                    reset_count += 1
            
            # Also clear all tracking dictionaries for a fresh start
            claimed_posts.clear()
            waitlists.clear()
            multiple_claims.clear()
            auction_posts.clear()
            offer_posts.clear()
            
            await update.message.reply_text(
                f"✅ COMPLETE SYSTEM RESET COMPLETED!\n\n"
                f"📊 Reset statistics:\n"
                f"• Users reset: {reset_count}\n"
                f"• Total items: {total_items}\n"
                f"• Total value: ${total_value:.2f}\n\n"
                f"🗑️ Cleared all tracking data\n\n"
                f"Ready for new sales session! 🎉",
                reply_markup=ReplyKeyboardRemove()
            )
            logger.info(f"Admin {user.id} performed complete system reset: {reset_count} users, ${total_value:.2f} total value")
            
        elif user_response in ['no', 'n']:
            # User cancelled
            context.user_data['awaiting_reset_confirmation'] = False
            await update.message.reply_text(
                "❌ Reset cancelled. No changes were made.",
                reply_markup=ReplyKeyboardRemove()
            )
            logger.info(f"Admin {user.id} cancelled system reset")
        else:
            # Invalid response
            await update.message.reply_text(
                "❌ Please answer with 'yes' or 'no'.",
                reply_markup=ReplyKeyboardRemove()
            )
        return
    
    # Initial command - show confirmation with Yes/No buttons
    # Count current statistics
    users_with_claims = 0
    total_items = 0
    total_value = 0
    
    for user_id, data in user_claims.items():
        if data["items"]:
            users_with_claims += 1
            total_items += len(data["items"])
            total_value += data["total"]
    
    # Create Yes/No keyboard
    keyboard = [['Yes', 'No']]
    reply_markup = ReplyKeyboardMarkup(
        keyboard, 
        one_time_keyboard=True,
        resize_keyboard=True
    )
    
    confirmation_message = (
        f"⚠️ 🚨 FULL SYSTEM RESET CONFIRMATION 🚨 ⚠️\n\n"
        f"📊 CURRENT SYSTEM STATUS:\n"
        f"• Users with claims: {users_with_claims}\n"
        f"• Total items: {total_items}\n"
        f"• Total value: ${total_value:.2f}\n\n"
        f"🔄 THIS WILL RESET:\n"
        f"• ALL users' claims and invoices\n"
        f"• ALL waitlists\n"
        f"• ALL multiple claims data\n"
        f"• ALL auction data\n"
        f"• ALL offer data\n\n"
        f"❌ THIS ACTION CANNOT BE UNDONE!\n\n"
        f"Are you sure you want to reset everything?\n\n"
        f"Please confirm:"
    )
    
    # Set confirmation state
    context.user_data['awaiting_reset_confirmation'] = True
    
    await update.message.reply_text(
        confirmation_message,
        reply_markup=reply_markup
    )


@private_chat_only
@admin_only
async def reset_specific_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to reset a specific user's claims."""
    user = update.effective_user
    
    # Check if user is admin
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("This command is for Daddy only.")
        return
    
    if not context.args:
        await update.message.reply_text(
            "Usage: /reset_user @username\n\n"
            "Examples:\n"
            "/reset_user @john123\n"
            "/reset_user 123456789 (user ID)\n"
            "/reset_user @john123 confirm (skip confirmation)"
        )
        return
    
    # Parse arguments
    target_identifier = context.args[0]
    skip_confirmation = len(context.args) > 1 and context.args[1].lower() == 'confirm'
    
    # Find user by username or user ID
    target_user_id = None
    target_username = None
    target_data = None
    
    # Try to find by username
    if target_identifier.startswith('@'):
        username_search = target_identifier.lstrip('@').lower()
        for user_id, data in user_claims.items():
            if data['username'].lstrip('@').lower() == username_search:
                target_user_id = user_id
                target_username = data['username']
                target_data = data
                break
    else:
        # Try to find by user ID
        try:
            user_id_search = int(target_identifier)
            if user_id_search in user_claims:
                target_user_id = user_id_search
                target_data = user_claims[user_id_search]
                target_username = target_data['username']
        except ValueError:
            await update.message.reply_text("❌ Please provide a valid username (starting with @) or user ID.")
            return
    
    if not target_user_id:
        await update.message.reply_text(f"❌ User '{target_identifier}' not found in claims database.")
        return
    
    # Get user info for confirmation
    try:
        user_info = await context.bot.get_chat(target_user_id)
        full_name = f"{user_info.first_name} {user_info.last_name or ''}".strip()
    except:
        full_name = target_data.get('full_name', 'Unknown')
    
    items_count = len(target_data["items"])
    total_value = target_data["total"]
    payment_status = target_data.get('payment_status', 'unpaid')
    
    # Show confirmation unless skipped
    if not skip_confirmation:
        confirmation_message = (
            f"⚠️ RESET USER CONFIRMATION ⚠️\n\n"
            f"User: {target_username} ({full_name})\n"
            f"User ID: {target_user_id}\n"
            f"Items: {items_count}\n"
            f"Total: ${total_value:.2f}\n"
            f"Payment Status: {'✅ PAID' if payment_status == 'paid' else '❌ UNPAID'}\n\n"
            f"Are you sure you want to reset this user's claims?\n\n"
            f"To confirm, use:\n"
            f"/reset_user {target_identifier} confirm"
        )
        await update.message.reply_text(confirmation_message)
        return
    
    # Perform the reset
    username_display = target_data.get("username", "No username")
    full_name_display = target_data.get("full_name", "Unknown")
    
    # Reset the user but keep basic info
    user_claims[target_user_id] = {
        "items": [], 
        "total": 0, 
        "username": username_display,
        "full_name": full_name_display,
        "payment_status": "unpaid",
        "shipping_method": None,
        "tracking_number": None
    }
    
    # Also remove user from any waitlists
    waitlist_removed = 0
    for message_id, waitlist in list(waitlists.items()):
        if target_user_id in waitlist:
            waitlist.remove(target_user_id)
            waitlist_removed += 1
            if not waitlist:  # Remove empty waitlists
                del waitlists[message_id]
    
    # Remove user from multiple claims waitlists
    multi_waitlist_removed = 0
    for message_id, multi_data in list(multiple_claims.items()):
        for claim_num, waitlist in list(multi_data.get('waitlist', {}).items()):
            if target_user_id in waitlist:
                waitlist.remove(target_user_id)
                multi_waitlist_removed += 1
                if not waitlist:
                    del multi_data['waitlist'][claim_num]
    
    # Notify the user (if possible)
    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text="🔄 Your claims have been reset by admin for the next sales session.\n\n"
                 "You can now start claiming new items!\n\n"
                 "Use /start to see available commands."
        )
        user_notified = True
    except Exception as e:
        user_notified = False
        logger.warning(f"Could not notify user {target_user_id} about reset: {e}")
    
    # Send success message to admin
    success_message = (
        f"✅ USER RESET SUCCESSFUL!\n\n"
        f"👤 User: {target_username} ({full_name_display})\n"
        f"🆔 User ID: {target_user_id}\n"
        f"📦 Items cleared: {items_count}\n"
        f"💰 Value cleared: ${total_value:.2f}\n"
        f"📋 Payment status: {payment_status}\n\n"
        f"🗑️ Cleanup:\n"
        f"• Regular waitlists: {waitlist_removed}\n"
        f"• Multi waitlists: {multi_waitlist_removed}\n"
        f"• User notified: {'✅ Yes' if user_notified else '❌ No'}\n\n"
        f"The user is now ready for new claims! 🎉"
    )
    
    await update.message.reply_text(success_message)
    logger.info(f"Admin {user.id} reset user {target_user_id} ({target_username}): {items_count} items, ${total_value:.2f}")

@private_chat_only
@admin_only
async def reset_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current reset status and statistics."""
    user = update.effective_user
    
    if user.id not in ADMIN_IDS:
        # Regular users see their own status
        if user.id not in user_claims or not user_claims[user.id]["items"]:
            await update.message.reply_text("You have no active claims.")
            return
        
        user_data = user_claims[user.id]
        await update.message.reply_text(
            f"📊 Your Current Claims:\n"
            f"• Items: {len(user_data['items'])}\n"
            f"• Total: ${user_data['total']:.2f}\n"
            f"• Status: {'✅ Paid' if user_data.get('payment_status') == 'paid' else '❌ Unpaid'}\n\n"
            f"Use /reset_my_claims to clear your claims for next session."
        )
        return
    
    # Admin sees system-wide status
    users_with_claims = 0
    paid_users = 0
    total_items = 0
    total_value = 0
    unpaid_value = 0
    
    for user_id, data in user_claims.items():
        if data["items"]:
            users_with_claims += 1
            total_items += len(data["items"])
            total_value += data["total"]
            
            if data.get("payment_status") == "paid":
                paid_users += 1
            else:
                unpaid_value += data["total"]
    
    await update.message.reply_text(
        f"📊 SYSTEM RESET STATUS\n\n"
        f"👥 Users with claims: {users_with_claims}\n"
        f"💰 Paid users: {paid_users}\n"
        f"📦 Total items: {total_items}\n"
        f"💵 Total value: ${total_value:.2f}\n"
        f"⏳ Unpaid value: ${unpaid_value:.2f}\n\n"
        f"🔄 Reset Commands:\n"
        f"/reset_user @username - Reset specific user\n"
        f"/reset_all_claims confirm - FULL SYSTEM RESET\n\n"
        f"Use with caution! ⚠️"
    )

@private_chat_only
@admin_only
async def find_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to find a user in the claims database."""
    user = update.effective_user
    
    # Check if user is admin
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("This command is for Daddy only.")
        return
    
    if not context.args:
        await update.message.reply_text(
            "Usage: /find_user @username_or_partial_name\n\n"
            "Examples:\n"
            "/find_user john\n"
            "/find_user @john123\n"
            "/find_user 123456789"
        )
        return
    
    search_term = context.args[0].lower()
    results = []
    
    # Search through all users
    for user_id, data in user_claims.items():
        username = data.get('username', '').lower()
        full_name = data.get('full_name', '').lower()
        
        # Check if search term matches username, full name, or user ID
        if (search_term in username or 
            search_term in full_name or 
            str(user_id) == search_term):
            
            items_count = len(data["items"])
            total_value = data["total"]
            payment_status = data.get('payment_status', 'unpaid')
            
            results.append({
                'user_id': user_id,
                'username': data.get('username', 'No username'),
                'full_name': data.get('full_name', 'Unknown'),
                'items_count': items_count,
                'total_value': total_value,
                'payment_status': payment_status
            })
    
    if not results:
        await update.message.reply_text(f"❌ No users found matching '{search_term}'")
        return
    
    # Format results
    if len(results) == 1:
        user_data = results[0]
        result_message = (
            f"🔍 USER FOUND:\n\n"
            f"👤 Username: {user_data['username']}\n"
            f"📛 Name: {user_data['full_name']}\n"
            f"🆔 User ID: {user_data['user_id']}\n"
            f"📦 Items: {user_data['items_count']}\n"
            f"💰 Total: ${user_data['total_value']:.2f}\n"
            f"💳 Status: {'✅ PAID' if user_data['payment_status'] == 'paid' else '❌ UNPAID'}\n\n"
            f"To reset this user:\n"
            f"/reset_user {user_data['user_id']}\n"
            f"or\n"
            f"/reset_user {user_data['username']}"
        )
    else:
        result_message = f"🔍 FOUND {len(results)} USERS:\n\n"
        for user_data in results[:10]:  # Show first 10 results
            result_message += (
                f"👤 {user_data['username']} ({user_data['full_name']})\n"
                f"   🆔 {user_data['user_id']} | "
                f"📦 {user_data['items_count']} items | "
                f"💰 ${user_data['total_value']:.2f} | "
                f"💳 {'✅' if user_data['payment_status'] == 'paid' else '❌'}\n"
                f"   Reset: /reset_user {user_data['user_id']}\n\n"
            )
        
        if len(results) > 10:
            result_message += f"... and {len(results) - 10} more users\n"
    
    await update.message.reply_text(result_message)


async def handle_multiple_claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle multiple claim messages like 'claim 1', 'claim 2', etc."""
    user = update.effective_user
    message = update.effective_message
    reply_to = message.reply_to_message
    
    text_to_search = reply_to.text or reply_to.caption or ""
    lines = text_to_search.split('\n')
    
    # Extract item name from lines 2-4 (skip first line with "Multiple")
    item_name = ""
    for i in range(1, min(4, len(lines))):  # Lines 2, 3, 4
        if lines[i].strip():
            clean_line = lines[i].strip()
            clean_line = re.sub(r'\$.*', '', clean_line).strip()
            clean_line = re.sub(r'(?i)(qty|quantity).*', '', clean_line).strip()
            if clean_line:
                item_name += clean_line + " "
    item_name = item_name.strip()
    
    if not item_name:
        item_name = "Multiple Claim Item"
    
    # Extract price
    price_match = re.search(r'\$(\d+(?:\.\d{1,2})?)', text_to_search)
    if not price_match:
        await message.reply_text("❌ Could not determine item price. Please contact admin.")
        return
    
    price = float(price_match.group(1))
    
    # Parse claim number from message
    claim_match = re.search(r'claim\s*(\d+)', message.text, re.IGNORECASE)
    if not claim_match:
        await message.reply_text("❌ Please use format 'claim X' where X is a number.")
        return
    
    claim_number = int(claim_match.group(1))
    
    # ✅ FIX: Get total quantity from the post to validate claim number range
    total_quantity = 10  # Default fallback
    quantity_match = re.search(r'(?:qty|quantity)[:\s]*(\d+)', text_to_search, re.IGNORECASE)
    if quantity_match:
        total_quantity = int(quantity_match.group(1))
    
    # Validate claim number is within the available range
    if claim_number < 1 or claim_number > total_quantity:
        await message.reply_text(f"❌ Claim number must be between 1 and {total_quantity} for this multi post.")
        return
    
    # Initialize multiple claims data if not exists
    if reply_to.message_id not in multiple_claims:
        multiple_claims[reply_to.message_id] = {
            "claims": {},
            "waitlist": {},
            "offers": {},
            "counter_offers": {},
            "accepted_offers": {},
            "total_quantity": total_quantity  # Store the actual quantity from the post
        }
        logger.info(f"✅ Initialized multi post {reply_to.message_id} with quantity {total_quantity}")
    
    multi_data = multiple_claims[reply_to.message_id]
    
    # Check if this claim number has an accepted offer
    if claim_number in multi_data.get("accepted_offers", {}):
        # Add to waitlist for this specific number
        if claim_number not in multi_data["waitlist"]:
            multi_data["waitlist"][claim_number] = []
        
        if user.id not in multi_data["waitlist"][claim_number]:
            multi_data["waitlist"][claim_number].append(user.id)
            await message.reply_text(
                f"⏳ Claim #{claim_number} has an accepted offer. You've been added to the waitlist for this number. "
                f"Daddy will notify you if it becomes available!"
            )
        else:
            await message.reply_text(f"⏳ You're already on the waitlist for claim #{claim_number}.")
        return
    
    # Check if this claim number is already taken (regular claim)
    if claim_number in multi_data["claims"]:
        # Add to waitlist for this specific number
        if claim_number not in multi_data["waitlist"]:
            multi_data["waitlist"][claim_number] = []
        
        if user.id not in multi_data["waitlist"][claim_number]:
            multi_data["waitlist"][claim_number].append(user.id)
            await message.reply_text(
                f"⏳ Claim #{claim_number} is already taken. You've been added to the waitlist for this number. "
                f"Daddy will notify you if it becomes available!"
            )
        else:
            await message.reply_text(f"⏳ You're already on the waitlist for claim #{claim_number}.")
        return
    
    # Process the claim
    multi_data["claims"][claim_number] = user.id
    
    # Initialize user data if not exists
    if user.id not in user_claims:
        user_claims[user.id] = {
            "items": [], 
            "total": 0, 
            "username": f"@{user.username}" if user.username else "No username",
            "full_name": user.first_name + (f" {user.last_name}" if user.last_name else ""),
            "payment_status": "unpaid",
            "shipping_method": None,
            "tracking_number": None
        }
    
    # Add claim to user's list
    user_data = user_claims[user.id]
    claim_data = {
        "text": f"{item_name} (#{claim_number})",
        "price": price,
        "message_id": reply_to.message_id,
        "timestamp": datetime.now(),
        "quantity": 1,
        "claim_number": claim_number,
        "is_multiple_claim": True
    }
    
    user_data["items"].append(claim_data)
    user_data["total"] += price
    
    # Send confirmation message
    user_name = user.first_name
    user_username = f"@{user.username}" if user.username else "No username"
    
    await message.reply_text(
        f"Claim #{claim_number} confirmed for {user_name} | {user_username}!\n"
        f"@{context.bot.username} will guide you through your claims 😊\n\n"
        f"Please PM me /start to see the commands my daddy set for me!"
    )

    # Update sold quantity when multi claim is made
    asyncio.create_task(update_sold_quantity(reply_to.message_id, 1))
    
    # Schedule a reminder for payment (in 12 hours)
    context.job_queue.run_once(
        payment_reminder, 
        timedelta(hours=12), 
        data=user.id, 
        name=f"payment_reminder_{user.id}"
    )

async def handle_multiple_counter_offer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle counter offers for multiple claim posts (admin only)."""
    message = update.effective_message
    reply_to = message.reply_to_message
    
    # Check if the message is a reply to another message
    if not reply_to or reply_to.message_id not in multiple_claims:
        return
    
    # Allow admin counter offers from admin sources
    user = update.effective_user
    is_admin_user = user and user.id in ADMIN_IDS
    is_channel_message = hasattr(message, 'sender_chat') and message.sender_chat and message.sender_chat.id in ADMIN_IDS
    is_forward_from_channel = hasattr(message, 'forward_from_chat') and message.forward_from_chat and message.forward_from_chat.id in ADMIN_IDS
    
    if not (is_admin_user or is_channel_message or is_forward_from_channel):
        return
    
    # Check if this is a counter offer for multiple claims - format: "co X at Y" or "co X for Y"
    co_match = re.search(r'co\s*(\d+)\s*(?:at|for)\s*(\$?\d+(?:\.\d{1,2})?)', message.text, re.IGNORECASE)
    if not co_match:
        return
    
    claim_number = int(co_match.group(1))
    counter_offer_amount = float(co_match.group(2).replace('$', ''))
    
    # ✅ FIX: Validate claim number is within the available range
    multi_data = multiple_claims[reply_to.message_id]
    total_quantity = multi_data.get("total_quantity", 10)
    
    if claim_number < 1 or claim_number > total_quantity:
        await message.reply_text(f"❌ Claim number must be between 1 and {total_quantity} for this multi post.")
        return
    
    
    # Get the current highest offer and user for this claim number
    current_offers = multi_data["offers"][claim_number]
    offer_user_id = max(current_offers, key=current_offers.get)
    offer_amount = current_offers[offer_user_id]
    
    # Check if counter offer is reasonable (should be higher than current offer)
    if counter_offer_amount <= offer_amount:
        await message.reply_text(f"Counter offer must be higher than current highest offer of ${offer_amount:.2f} for claim #{claim_number}.")
        return
    
    # Initialize counter offers structure if not exists
    if "counter_offers" not in multi_data:
        multi_data["counter_offers"] = {}
    if claim_number not in multi_data["counter_offers"]:
        multi_data["counter_offers"][claim_number] = {}
    
    # Record the counter offer
    multi_data["counter_offers"][claim_number][offer_user_id] = counter_offer_amount
    
    # Post counter offer in comments
    try:
        await context.bot.send_message(
            chat_id=message.chat_id,
            reply_to_message_id=reply_to.message_id,
            text=f"💵 Counter offer for claim #{claim_number}: ${counter_offer_amount:.2f}\n"
                 f"Original offer: ${offer_amount:.2f}\n"
                 f"You can reply 'take {claim_number}' to accept this counter offer."
        )
    except Exception as e:
        logger.error(f"Error posting counter offer in comments: {e}")
    
    # Notify the user about the counter offer via PM
    try:
        await context.bot.send_message(
            chat_id=offer_user_id,
            text=f"💵 Counter offer from Daddy for claim #{claim_number}: ${counter_offer_amount:.2f}\n\n"
                 f"Your original offer: ${offer_amount:.2f}\n"
                 f"Reply with 'take {claim_number}' to the original post to accept this counter offer by Daddy."
        )
    except Exception as e:
        logger.error(f"Error notifying user {offer_user_id}: {e}")


async def handle_unclaim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle unclaim messages including multiple claims with numbers."""
    user = update.effective_user
    message = update.effective_message
    reply_to = message.reply_to_message
    
    # Check if the message is a reply to another message
    if not reply_to:
        await message.reply_text("Please reply to the item you want to unclaim.")
        return
    
    # Check if user has any claims
    if user.id not in user_claims or not user_claims[user.id]["items"]:
        await message.reply_text("You have no items to unclaim.")
        return
    
    # Check if this is a multiple unclaim with number (e.g., "unclaim 1")
    unclaim_match = re.search(r'unclaim\s*(\d+)', message.text, re.IGNORECASE)
    if unclaim_match:
        claim_number = int(unclaim_match.group(1))
        await handle_specific_unclaim(update, context, claim_number)
        return
    
    # Handle regular unclaim (most recent claim)
    await handle_most_recent_unclaim(update, context)

async def handle_specific_unclaim(update: Update, context: ContextTypes.DEFAULT_TYPE, claim_number: int):
    """Handle unclaim for a specific claim number."""
    user = update.effective_user
    message = update.effective_message
    reply_to = message.reply_to_message
    
    # ✅ FIX: Get total quantity from the post to validate claim number range
    total_quantity = 10  # Default fallback
    text_to_search = reply_to.text or reply_to.caption or ""
    quantity_match = re.search(r'(?:qty|quantity)[:\s]*(\d+)', text_to_search, re.IGNORECASE)
    if quantity_match:
        total_quantity = int(quantity_match.group(1))
    
    if claim_number < 1 or claim_number > total_quantity:
        await message.reply_text(f"❌ Claim number must be between 1 and {total_quantity} for this multi post.")
        return
    
    # Find the specific claim in user's claims
    user_data = user_claims[user.id]
    item_to_remove = None
    
    for item in user_data["items"]:
        if (item["message_id"] == reply_to.message_id and 
            item.get("is_multiple_claim") and 
            item.get("claim_number") == claim_number):
            item_to_remove = item
            break
    
    if not item_to_remove:
        await message.reply_text(f"You haven't claimed #{claim_number} for this item.")
        return
    
    # Check if this is a multiple claim item
    if item_to_remove.get("is_multiple_claim"):
        await handle_multiple_unclaim(update, context, item_to_remove)
    else:
        # Fallback to regular unclaim handling
        await handle_regular_unclaim(update, context, item_to_remove)

async def handle_most_recent_unclaim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle unclaim for the most recent claim when no number is specified."""
    user = update.effective_user
    message = update.effective_message
    reply_to = message.reply_to_message
    
    # Find the most recent claim for this post in user's claims
    user_data = user_claims[user.id]
    items_for_this_post = []
    
    for item in user_data["items"]:
        if item["message_id"] == reply_to.message_id:
            items_for_this_post.append(item)
    
    if not items_for_this_post:
        await message.reply_text("You haven't claimed any items from this post.")
        return
    
    # Sort by timestamp (most recent first) and take the first one
    items_for_this_post.sort(key=lambda x: x["timestamp"], reverse=True)
    most_recent_item = items_for_this_post[0]
    
    # Check if this is a multiple claim item
    if most_recent_item.get("is_multiple_claim"):
        await handle_multiple_unclaim(update, context, most_recent_item)
    else:
        await handle_regular_unclaim(update, context, most_recent_item)

async def handle_regular_unclaim(update: Update, context: ContextTypes.DEFAULT_TYPE, item_to_remove):
    """Handle regular (non-multiple) unclaim."""
    user = update.effective_user
    message = update.effective_message
    reply_to = message.reply_to_message
    
    # Store item details for waitlist assignment
    item_price = item_to_remove["price"]
    item_text = item_to_remove["text"]
    item_message_id = item_to_remove["message_id"]
    
    # Remove the item
    user_data = user_claims[user.id]
    user_data["items"].remove(item_to_remove)
    user_data["total"] -= item_to_remove["price"]
    
    # Handle waitlist if there is one - AUTO ASSIGN TO NEXT PERSON
    current_waitlist = waitlists.get(item_message_id, [])
    if current_waitlist:
        next_user_id = current_waitlist.pop(0)
        waitlists[item_message_id] = current_waitlist
        
        # Initialize next user's data if not exists
        if next_user_id not in user_claims:
            try:
                next_user_info = await context.bot.get_chat(next_user_id)
                username = f"@{next_user_info.username}" if next_user_info.username else "No username"
                full_name = next_user_info.first_name + (f" {next_user_info.last_name}" if next_user_info.last_name else "")
            except:
                username = "No username"
                full_name = "Unknown"
                
            user_claims[next_user_id] = {
                "items": [], 
                "total": 0, 
                "username": username,
                "full_name": full_name,
                "payment_status": "unpaid",
                "shipping_method": None,
                "tracking_number": None
            }
        
        # Add the item to next user's claims
        next_user_data = user_claims[next_user_id]
        next_user_data["items"].append({
            "text": item_text,
            "price": item_price,
            "message_id": item_message_id,
            "timestamp": datetime.now(),
            "quantity": 1
        })
        next_user_data["total"] += item_price
        
        # ✅ FIX: DO NOT update sold quantity here - it stays the same since we're just transferring ownership
        # The item is still sold, just to a different person
        logger.info(f"🔄 Transferring ownership of message {item_message_id} from {user.id} to {next_user_id} - sold quantity remains unchanged")
        
        # Notify next user in waitlist
        try:
            await context.bot.send_message(
                chat_id=next_user_id,
                text=f"🎉 Good news! An item you were waitlisted for is now available!\n\n"
                     f"Item: {item_text.split('\n')[0][:50]}\n\n"
                     f"The item has been automatically added to your claims.\n"
                     f"Please use /invoice to see your total and /pay to confirm payment."
            )
        except Exception as e:
            logger.error(f"Error notifying waitlisted user {next_user_id}: {e}")
        
        # Notify in comments
        try:
            next_user_username = user_claims[next_user_id]["username"]
            await context.bot.send_message(
                chat_id=message.chat_id,
                reply_to_message_id=reply_to.message_id,
                text=f"✅ Item automatically assigned to {next_user_username} from waitlist"
            )
        except Exception as e:
            logger.error(f"Error posting waitlist assignment in comments: {e}")
            
        # Update claimed_posts to reflect the new owner
        if item_message_id in claimed_posts:
            if "quantity" in claimed_posts[item_message_id]:
                # For quantity items, keep the same quantity but update user_id
                claimed_posts[item_message_id]["user_id"] = next_user_id
            else:
                # For single items, update the user_id
                claimed_posts[item_message_id]["user_id"] = next_user_id
    else:
        # ✅ FIX: Only update quantities when there's NO waitlist (actual unclaim with no replacement)
        logger.info(f"🔄 Actual unclaim with no waitlist for message {item_message_id}")
        asyncio.create_task(update_sold_quantity(item_message_id, -1))
        
        # Update claimed_posts only if no waitlist assignment
        if reply_to.message_id in claimed_posts:
            if "quantity" in claimed_posts[reply_to.message_id]:
                # For quantity items, increase available quantity
                claimed_posts[reply_to.message_id]["quantity"] += 1
            else:
                # For single items, remove the claim entirely
                del claimed_posts[reply_to.message_id]
    
    user_name = user.first_name
    user_username = f"@{user.username}" if user.username else "No username"

    # Extract only the item name (first line before any newline)
    item_name = item_to_remove['text'].split('\n')[0].strip()

    await message.reply_text(
        f"{user_username} unclaimed {item_name}"
    )


async def handle_multiple_unclaim(update: Update, context: ContextTypes.DEFAULT_TYPE, item_to_remove):
    """Handle unclaim for multiple claim items."""
    user = update.effective_user
    message = update.effective_message
    reply_to = message.reply_to_message
    
    claim_number = item_to_remove.get("claim_number")
    message_id = reply_to.message_id
    
    # Remove from user's claims
    user_data = user_claims[user.id]
    user_data["items"].remove(item_to_remove)
    user_data["total"] -= item_to_remove["price"]
    
    # Handle multiple claims data
    if message_id in multiple_claims:
        multi_data = multiple_claims[message_id]
        
        # Remove the claim
        if claim_number in multi_data["claims"] and multi_data["claims"][claim_number] == user.id:
            del multi_data["claims"][claim_number]
            
            # Check waitlist for this claim number
            if claim_number in multi_data.get("waitlist", {}) and multi_data["waitlist"][claim_number]:
                next_user_id = multi_data["waitlist"][claim_number].pop(0)
                
                # Initialize next user's data if not exists
                if next_user_id not in user_claims:
                    try:
                        next_user_info = await context.bot.get_chat(next_user_id)
                        username = f"@{next_user_info.username}" if next_user_info.username else "No username"
                        full_name = next_user_info.first_name + (f" {next_user_info.last_name}" if next_user_info.last_name else "")
                    except:
                        username = "No username"
                        full_name = "Unknown"
                        
                    user_claims[next_user_id] = {
                        "items": [], 
                        "total": 0, 
                        "username": username,
                        "full_name": full_name,
                        "payment_status": "unpaid",
                        "shipping_method": None,
                        "tracking_number": None
                    }
                
                # Add to next user's claims
                next_user_data = user_claims[next_user_id]
                next_user_data["items"].append({
                    "text": item_to_remove["text"],
                    "price": item_to_remove["price"],
                    "message_id": message_id,
                    "timestamp": datetime.now(),
                    "quantity": 1,
                    "claim_number": claim_number,
                    "is_multiple_claim": True
                })
                next_user_data["total"] += item_to_remove["price"]
                
                # ✅ FIX: DO NOT update sold quantity - just transfer ownership
                logger.info(f"🔄 Transferring multiple claim #{claim_number} from {user.id} to {next_user_id} - sold quantity unchanged")
                
                # Update multiple claims data
                multi_data["claims"][claim_number] = next_user_id
                
                # Notify next user
                try:
                    await context.bot.send_message(
                        chat_id=next_user_id,
                        text=f"🎉 Good news! Claim #{claim_number} you were waitlisted for is now available!\n\n"
                             f"The claim has been automatically added to your claims.\n"
                             f"Please use /invoice to see your total and /pay to confirm payment."
                    )
                except Exception as e:
                    logger.error(f"Error notifying waitlisted user {next_user_id}: {e}")
                
                # Notify in comments
                try:
                    next_user_username = user_claims[next_user_id]["username"]
                    await context.bot.send_message(
                        chat_id=message.chat_id,
                        reply_to_message_id=reply_to.message_id,
                        text=f"✅ Claim #{claim_number} automatically assigned to {next_user_username} from waitlist"
                    )
                except Exception as e:
                    logger.error(f"Error posting waitlist assignment in comments: {e}")
            else:
                # No waitlist, just post unclaim message
                user_name = user.first_name
                user_username = f"@{user.username}" if user.username else "No username"
                
                await message.reply_text(
                    f"{user_username} unclaimed {item_to_remove['text']}"
                )
                
                # ✅ FIX: Only update sold quantity when NOT reassigned (actual unclaim)
                logger.info(f"🔄 Actual multiple unclaim with no waitlist for claim #{claim_number}")
                asyncio.create_task(update_sold_quantity(message_id, -1))


async def handle_offer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle offer messages including multiple claim offers."""
    user = update.effective_user
    message = update.effective_message
    reply_to = message.reply_to_message
    
    # Check if the message is a reply to another message
    if not reply_to:
        return
    
    # Check if this is a multiple claim post
    text_to_search = reply_to.text or reply_to.caption or ""
    first_line = text_to_search.split('\n')[0].strip() if text_to_search else ""
    
    if "multiple" in first_line.lower():
        await handle_multiple_offer(update, context)
        return
    
      # CHECK IF ITEM IS ALREADY FULLY CLAIMED (no quantity left)
    claim_data = claimed_posts.get(reply_to.message_id)
    if claim_data:
        # If this is a counter offer item with remaining quantity, ALLOW offers/claims
        if "counter_offer_price" in claim_data and "quantity" in claim_data:
            if claim_data["quantity"] <= 0:
                await message.reply_text("❌ This item is fully claimed and no longer available for offers. Don't worry my Daddy will release more in the future!")
                return
            else:
                # THERE IS REMAINING QUANTITY - ALLOW THE OFFER/CLAIM TO PROCEED
                pass
        elif "quantity" in claim_data:
            if claim_data["quantity"] <= 0:
                await message.reply_text("❌ This item is fully claimed and no longer available for offers. Don't worry my Daddy will release more in the future!")
                return
            else:
                # THERE IS REMAINING QUANTITY - ALLOW THE OFFER/CLAIM TO PROCEED
                pass
        else:
            # Single item already claimed (no quantity field means it's a single item that's fully claimed)
            await message.reply_text("❌ This item has already been claimed and is no longer available for offers. Don't worry my Daddy will release more in the future!")
            return
    
    # Check if this is an offer post - IMPROVED REGEX TO HANDLE BOTH FORMATS
    offer_match = re.search(r'offer\s*(\$?\d+(?:\.\d{1,2})?)', message.text, re.IGNORECASE)
    if not offer_match:
        return
    
    # Extract the amount and remove any dollar sign
    offer_amount_str = offer_match.group(1)
    # Remove dollar sign if present
    offer_amount_str = offer_amount_str.replace('$', '')
    offer_amount = float(offer_amount_str)
    
    # Initialize offer post data if not exists
    if reply_to.message_id not in offer_posts:
        offer_posts[reply_to.message_id] = {
            "offers": {},
            "counter_offers": {}
        }
    
    # Check if this user already made an offer on this post
    current_offers = offer_posts[reply_to.message_id]["offers"]
    if user.id in current_offers:
        # User already has an offer, check if new offer is higher
        previous_offer = current_offers[user.id]
        if offer_amount <= previous_offer:
            await message.reply_text(
                f"❌ Your new offer of ${offer_amount:.2f} is not higher than your previous offer of ${previous_offer:.2f}."
            )
            return
    
    # Check if this is the highest offer so far
    if current_offers:
        current_highest = max(current_offers.values())
        current_highest_user = max(current_offers, key=current_offers.get)
        
        # If new offer is higher than current highest, notify the previous highest bidder
        if offer_amount > current_highest:
            try:
                await context.bot.send_message(
                    chat_id=current_highest_user,
                    text=f"⚠️ Your offer of ${current_highest:.2f} has been outbid.\n"
                         f"New highest offer: ${offer_amount:.2f}\n\n"
                         f"You can make a higher offer if interested."
                )
            except Exception as e:
                logger.error(f"Error notifying outbid user {current_highest_user}: {e}")
    
    # Record the new offer (overwrite previous offer if any)
    offer_posts[reply_to.message_id]["offers"][user.id] = offer_amount
    
    # Remove any existing counter offers for this user since they made a new offer
    if user.id in offer_posts[reply_to.message_id]["counter_offers"]:
        del offer_posts[reply_to.message_id]["counter_offers"][user.id]
    
    # Post offer update in comments
    try:
        current_highest = max(offer_posts[reply_to.message_id]["offers"].values()) if offer_posts[reply_to.message_id]["offers"] else offer_amount
        await context.bot.send_message(
            chat_id=message.chat_id,
            reply_to_message_id=reply_to.message_id,
            text=f"💵 New offer from @{user.username}: ${offer_amount:.2f}\n"
                 f"Current highest: ${current_highest:.2f}\n"
                 f"Daddy may respond with a counter offer."
        )
    except Exception as e:
        logger.error(f"Error posting offer update in comments: {e}")

async def handle_multiple_offer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle offer messages for multiple claim posts with specific numbers."""
    user = update.effective_user
    message = update.effective_message
    reply_to = message.reply_to_message
    
    logger.info(f"Multi offer detected: '{message.text}' from user {user.id}")

    text_to_search = reply_to.text or reply_to.caption or ""
    lines = text_to_search.split('\n')
    
    # Extract item name from lines 2-4 (skip first line with "Multiple")
    item_name = ""
    for i in range(1, min(4, len(lines))):  # Lines 2, 3, 4
        if lines[i].strip():
            clean_line = lines[i].strip()
            # Remove price information if present
            clean_line = re.sub(r'\$.*', '', clean_line).strip()
            # Remove quantity information if present  
            clean_line = re.sub(r'(?i)(qty|quantity).*', '', clean_line).strip()
            if clean_line:
                item_name += clean_line + " "
    item_name = item_name.strip()
    
    if not item_name:
        item_name = "Multiple Claim Item"
    
    # Extract original price from post
    price_match = re.search(r'\$(\d+(?:\.\d{1,2})?)', text_to_search)
    if not price_match:
        await message.reply_text("❌ Could not determine item price. Please contact admin.")
        return
    
    original_price = float(price_match.group(1))
    
    # Parse offer with number and price - format: "offer X at Y" or "offer X for Y"
    offer_match = re.search(r'offer\s*(\d+)\s*(?:at|for)\s*(\$?\d+(?:\.\d{1,2})?)', message.text, re.IGNORECASE)
    if not offer_match:
        await message.reply_text("❌ For multiple claim posts, please use format: 'offer X at Y' or 'offer X for Y' where X is claim number and Y is your offer price.")
        return
    
    claim_number = int(offer_match.group(1))
    offer_amount_str = offer_match.group(2).replace('$', '')
    offer_amount = float(offer_amount_str)
    
    # ✅ FIX: Get total quantity from the post to validate claim number range
    total_quantity = 10  # Default fallback
    quantity_match = re.search(r'(?:qty|quantity)[:\s]*(\d+)', text_to_search, re.IGNORECASE)
    if quantity_match:
        total_quantity = int(quantity_match.group(1))
    
    # Validate claim number is within the available range
    if claim_number < 1 or claim_number > total_quantity:
        await message.reply_text(f"❌ Claim number must be between 1 and {total_quantity} for this multi post.")
        return
    
    # Check if offer is reasonable (should be different from original price)
    if offer_amount == original_price:
        await message.reply_text(f"❌ Your offer of ${offer_amount:.2f} is the same as the original price. Please use 'claim {claim_number}' instead.")
        return
    
    # Initialize multiple claims data if not exists
    if reply_to.message_id not in multiple_claims:
        multiple_claims[reply_to.message_id] = {
            "claims": {},  # {claim_number: user_id}
            "waitlist": {},  # {claim_number: [user_ids]}
            "offers": {},  # {claim_number: {user_id: offer_amount}}
            "total_quantity": total_quantity
        }
    
    multi_data = multiple_claims[reply_to.message_id]
    
    # Initialize offers structure for this claim number if not exists
    if claim_number not in multi_data["offers"]:
        multi_data["offers"][claim_number] = {}
    
    # Check if this claim number is already claimed
    if claim_number in multi_data["claims"]:
        await message.reply_text(f"❌ Claim #{claim_number} is already taken. You can only make offers for available claim numbers.")
        return
    
    # [Rest of your existing offer handling code...]
    
    # Check if this user already made an offer for this claim number
    current_offers = multi_data["offers"][claim_number]
    if user.id in current_offers:
        # User already has an offer, check if new offer is different
        previous_offer = current_offers[user.id]
        if offer_amount == previous_offer:
            await message.reply_text(f"❌ You already made the same offer of ${offer_amount:.2f} for claim #{claim_number}.")
            return
    
    # Check if this is the highest offer for this claim number
    if current_offers:
        current_highest = max(current_offers.values())
        current_highest_user = max(current_offers, key=current_offers.get)
        
        # If new offer is higher than current highest, notify the previous highest bidder
        if offer_amount > current_highest and user.id != current_highest_user:
            try:
                await context.bot.send_message(
                    chat_id=current_highest_user,
                    text=f"⚠️ Your offer of ${current_highest:.2f} for claim #{claim_number} has been outbid.\n"
                         f"New highest offer: ${offer_amount:.2f}\n\n"
                         f"You can make a higher offer if interested."
                )
            except Exception as e:
                logger.error(f"Error notifying outbid user {current_highest_user}: {e}")
    
    # Record the new offer (overwrite previous offer if any)
    multi_data["offers"][claim_number][user.id] = offer_amount
    
    # Post offer update in comments
    try:
        current_highest = max(multi_data["offers"][claim_number].values()) if multi_data["offers"][claim_number] else offer_amount
        await context.bot.send_message(
            chat_id=message.chat_id,
            reply_to_message_id=reply_to.message_id,
            text=f"💵 New offer from @{user.username} for claim #{claim_number}: ${offer_amount:.2f}\n"
                 f"Current highest for #{claim_number}: ${current_highest:.2f}\n"
                 f"Original price: ${original_price:.2f}\n"
                 f"Daddy may respond with a counter offer."
        )
    except Exception as e:
        logger.error(f"Error posting offer update in comments: {e}")

async def handle_counter_offer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle counter offer messages (admin only)."""
    message = update.effective_message
    reply_to = message.reply_to_message
    
    # Check if the message is a reply to another message
    if not reply_to or reply_to.message_id not in offer_posts:
        return
    
       # CHECK IF ITEM IS ALREADY FULLY CLAIMED (no quantity left)
    if reply_to.message_id in claimed_posts:
        claim_data = claimed_posts[reply_to.message_id]
        if "quantity" in claim_data:
            if claim_data["quantity"] <= 0:
                await message.reply_text("❌ This item is fully claimed and counter offers are no longer accepted. Don't worry Daddy will release more in the future!")
                return
        else:
            # Single item already claimed
            await message.reply_text("❌ This item has already been claimed and counter offers are no longer accepted. Don't worry Daddy will release more in the future!")
            return
    
    # Check if this is a counter offer
    co_match = re.search(r'co\s*(\d+(?:\.\d{1,2})?)', message.text, re.IGNORECASE)
    if not co_match:
        return
    
    # Allow admin confirmation from:
    # 1. Admin users (by user ID)
    user = update.effective_user
    is_admin_user = user and user.id in ADMIN_IDS
    
    # 2. Channel messages (check if sender chat is in admin IDs)
    is_channel_message = False
    if hasattr(message, 'sender_chat') and message.sender_chat:
        is_channel_message = message.sender_chat.id in ADMIN_IDS
    
    # 3. Also check if the message is from a channel that forwarded the message
    is_forward_from_channel = False
    if hasattr(message, 'forward_from_chat') and message.forward_from_chat:
        is_forward_from_channel = message.forward_from_chat.id in ADMIN_IDS
    
    if not (is_admin_user or is_channel_message or is_forward_from_channel):
        return
    
    counter_offer_amount = float(co_match.group(1))
    
    # Check if there are still active offers (not outbid)
    if not offer_posts[reply_to.message_id]["offers"]:
        await message.reply_text("No active offers found for this post.")
        return
    
    # Get the current highest offer and user
    current_offers = offer_posts[reply_to.message_id]["offers"]
    offer_user_id = max(current_offers, key=current_offers.get)
    offer_amount = current_offers[offer_user_id]
    
    # Check if counter offer is reasonable (should be higher than current offer)
    if counter_offer_amount <= offer_amount:
        await message.reply_text(f"Counter offer must be higher than current highest offer of ${offer_amount:.2f}.")
        return
    
    # Record the counter offer
    offer_posts[reply_to.message_id]["counter_offers"][offer_user_id] = counter_offer_amount
    
    # Post counter offer in comments (reply to the original post)
    try:
        await context.bot.send_message(
            chat_id=message.chat_id,
            reply_to_message_id=reply_to.message_id,  # Reply to original post
            text=f"💵 Counter offer: ${counter_offer_amount:.2f}\n"
                 f"Original offer: ${offer_amount:.2f}\n"
                 f"You can reply 'take' to accept this counter offer."
        )
    except Exception as e:
        logger.error(f"Error posting counter offer in comments: {e}")
    
       # Notify the user about the counter offer via PM
    try:
        await context.bot.send_message(
            chat_id=offer_user_id,
            text=f"💵 Counter offer from Daddy: ${counter_offer_amount:.2f}\n\n"
                 f"Your original offer: ${offer_amount:.2f}\n"
                 f"Reply with 'take' to the original post to accept this counter offer by Daddy."
        )
    except Exception as e:
        logger.error(f"Error notifying user {offer_user_id}: {e}")
        # Don't show the error message if it's a bot-to-bot communication issue
        if "bots can't send messages to bots" not in str(e):
            await message.reply_text(f"Could not notify user: {e}")

async def handle_take(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle take messages to accept counter offers for both regular and multiple claims."""
    user = update.effective_user
    message = update.effective_message
    reply_to = message.reply_to_message
    
    logger.info(f"Take command received from user {user.id}: '{message.text}'")
    
    # Check if the message is a reply to the counter offer message
    if not reply_to:
        await message.reply_text("Please reply to the counter offer message with 'take'.")
        return
    
    # Parse if specific claim number is mentioned - format: "take X"
    take_match = re.search(r'take\s*(\d+)', message.text, re.IGNORECASE)
    claim_number = int(take_match.group(1)) if take_match else None
    
    logger.info(f"Parsed claim number: {claim_number}")
    
    # Handle multiple claim counter offers
    if claim_number:
        # Find if this user has a counter offer for the specified claim number
        text_to_search = reply_to.text or reply_to.caption or ""
        first_line = text_to_search.split('\n')[0].strip() if text_to_search else ""
        
        logger.info(f"Checking multiple claim for message {reply_to.message_id}, first line: '{first_line}'")
        
        if "multiple" in first_line.lower() and reply_to.message_id in multiple_claims:
            multi_data = multiple_claims[reply_to.message_id]
            logger.info(f"Multiple claims data found: {multi_data}")
            
            if (claim_number in multi_data.get("counter_offers", {}) and 
                user.id in multi_data["counter_offers"][claim_number]):
                
                counter_offer_amount = multi_data["counter_offers"][claim_number][user.id]
                logger.info(f"Counter offer found: ${counter_offer_amount} for claim #{claim_number}")
                
                await handle_multiple_take(update, context, claim_number, counter_offer_amount)
                return
            else:
                logger.info(f"No counter offer found for user {user.id} for claim #{claim_number}")
                logger.info(f"Available counter offers: {multi_data.get('counter_offers', {})}")
        
        await message.reply_text(f"No counter offer found for you for claim #{claim_number}.")
        return
    
    # Find if this user has any counter offers for ANY post
    counter_offer_post_id = None
    counter_offer_amount = None
    
    for post_id, post_data in offer_posts.items():
        if user.id in post_data.get("counter_offers", {}):
            counter_offer_post_id = post_id
            counter_offer_amount = post_data["counter_offers"][user.id]
            break
    
    if not counter_offer_post_id:
        await message.reply_text("No counter offer found for you.")
        return

    # Get item name from the original post that has the counter offer
    try:
        # Get the original post that was replied to with the counter offer
        original_post = await context.bot.get_message(
            chat_id=message.chat_id, 
            message_id=counter_offer_post_id
        )
    
        # Use the same extraction logic as regular claims
        text_to_search = original_post.text or original_post.caption or ""
        item_name = ""

        # Get the first line and remove any price/quantity information
        lines = text_to_search.split('\n')
        if lines:
            first_line = lines[0].strip()
            # Remove price information if present
            item_name = re.sub(r'\$.*', '', first_line).strip()
            # Remove quantity information if present
            item_name = re.sub(r'(?i)(qty|quantity).*', '', item_name).strip()

        # If we ended up with nothing, use a shortened version of first line
        if not item_name and lines:
            item_name = lines[0].strip()[:50]

        # Final fallback
        if not item_name:
            item_name = "Item"
        
        logger.info(f"Extracted item name for counter offer: '{item_name}'")

    except Exception as e:
        logger.error(f"Error getting original post for counter offer: {e}")
        # Try alternative approach - use the counter offer message text
        try:
            counter_text = reply_to.text or reply_to.caption or ""
            # Look for the item name in the counter offer message
            lines = counter_text.split('\n')
            for line in lines:
                if line.strip() and not any(x in line.lower() for x in ['counter offer', 'original offer', '$']):
                    item_name = line.strip()[:50]
                    break
            if not item_name:
                item_name = "Item"
        except:
            item_name = "Item"
    
    # Initialize user data if not exists
    if user.id not in user_claims:
        user_claims[user.id] = {
            "items": [], 
            "total": 0, 
            "username": f"@{user.username}" if user.username else "No username",
            "full_name": user.first_name + (f" {user.last_name}" if user.last_name else ""),
            "payment_status": "unpaid",
            "shipping_method": None,
            "tracking_number": None
        }
    
    # Add the counter offer to user's claims
    user_data = user_claims[user.id]
    user_data["items"].append({
        "text": item_name,
        "price": counter_offer_amount,
        "message_id": counter_offer_post_id,
        "timestamp": datetime.now(),
        "quantity": 1,
        "is_counter_offer": True
    })

    # EXTRA DEBUGGING
    logger.info("=== COUNTER OFFER DEBUG ===")
    logger.info(f"User: {user.id}")
    logger.info(f"Counter offer amount: ${counter_offer_amount}")
    logger.info(f"Item name stored: '{item_name}'")
    logger.info(f"All user items: {[item['text'] for item in user_data['items']]}")
    logger.info(f"User total: ${user_data['total']}")
    logger.info("===========================")

    user_data["total"] += counter_offer_amount


        # Handle quantity when counter offer is accepted
    try:
        original_post = await context.bot.get_message(
            chat_id=message.chat_id, 
            message_id=counter_offer_post_id
        )
        text_to_search = original_post.text or original_post.caption or ""
        quantity_match = re.search(r'(?:qty|quantity)[:\s]*(\d+)', text_to_search, re.IGNORECASE)
        original_quantity = int(quantity_match.group(1)) if quantity_match else 1
    except:
        original_quantity = 1

    # For multi-quantity items, store the counter offer price for remaining quantities
    if original_quantity > 1:
        if counter_offer_post_id in claimed_posts:
            # Item already has some claims - reduce available quantity
            if "quantity" in claimed_posts[counter_offer_post_id]:
                claimed_posts[counter_offer_post_id]["quantity"] -= 1
            else:
                # Convert from single claim to quantity format with counter offer price
                claimed_posts[counter_offer_post_id] = {
                    "quantity": original_quantity - 1,  # Remaining quantity
                    "counter_offer_price": counter_offer_amount  # Store the counter offer price
                }
        else:
            # First claim of this multi-quantity item via counter offer
            claimed_posts[counter_offer_post_id] = {
                "quantity": original_quantity - 1,  # Remaining quantity
                "counter_offer_price": counter_offer_amount  # Store the counter offer price
            }
    else:
        # Single item - mark as fully claimed at counter offer price
        claimed_posts[counter_offer_post_id] = {"user_id": user.id}
    
        # Notify waitlisted users for single items only
        if counter_offer_post_id in waitlists:
            waitlist_users = waitlists[counter_offer_post_id]
            for waitlist_user_id in waitlist_users:
                try:
                    await context.bot.send_message(
                        chat_id=waitlist_user_id,
                        text="❌ The item you were waitlisted for has been sold through a counter offer. Don't worry, Daddy will release more cards in the future!"
                    )
                except Exception as e:
                    logger.error(f"Error notifying waitlisted user {waitlist_user_id}: {e}")
    
            # Clear the waitlist for this single item
            del waitlists[counter_offer_post_id]

    # Notify in comments (reply to original post)
    try:
        await context.bot.send_message(
            chat_id=message.chat_id,
            reply_to_message_id=counter_offer_post_id,
            text=f"✅ Counter offer accepted! @{user.username if user.username else 'user'} accepted ${counter_offer_amount:.2f}"
        )
    except Exception as e:
        logger.error(f"Error posting acceptance in comments: {e}")

        # Update sold quantity and price for counter offer
    asyncio.create_task(update_sold_quantity(counter_offer_post_id, 1))
    asyncio.create_task(update_post_price(counter_offer_post_id, counter_offer_amount))


    # Schedule a reminder for payment (in 12 hours)
    context.job_queue.run_once(
        payment_reminder, 
        timedelta(hours=12), 
        data=user.id, 
        name=f"payment_reminder_{user.id}"
    )

async def handle_multiple_take(update: Update, context: ContextTypes.DEFAULT_TYPE, claim_number: int, counter_offer_amount: float):
    """Handle accepting counter offers for multiple claims."""
    user = update.effective_user
    message = update.effective_message
    reply_to = message.reply_to_message
    
    multi_data = multiple_claims[reply_to.message_id]
    
    # Get item details
    text_to_search = reply_to.text or reply_to.caption or ""
    lines = text_to_search.split('\n')
    item_name = ""
    for i in range(1, min(4, len(lines))):
        if lines[i].strip():
            clean_line = lines[i].strip()
            clean_line = re.sub(r'\$.*', '', clean_line).strip()
            clean_line = re.sub(r'(?i)(qty|quantity).*', '', clean_line).strip()
            if clean_line:
                item_name += clean_line + " "
    item_name = item_name.strip()
    
    if not item_name:
        item_name = "Multiple Claim Item"
    
    # Initialize user data if not exists
    if user.id not in user_claims:
        user_claims[user.id] = {
            "items": [], 
            "total": 0, 
            "username": f"@{user.username}" if user.username else "No username",
            "full_name": user.first_name + (f" {user.last_name}" if user.last_name else ""),
            "payment_status": "unpaid",
            "shipping_method": None,
            "tracking_number": None
        }
    
    # Add the counter offer to user's claims
    user_data = user_claims[user.id]
    user_data["items"].append({
        "text": f"{item_name} (#{claim_number})",
        "price": counter_offer_amount,
        "message_id": reply_to.message_id,
        "timestamp": datetime.now(),
        "quantity": 1,
        "claim_number": claim_number,
        "is_multiple_claim": True,
        "is_counter_offer": True
    })
    user_data["total"] += counter_offer_amount
    
    # Mark this claim number as accepted via counter offer
    if "accepted_offers" not in multi_data:
        multi_data["accepted_offers"] = {}
    multi_data["accepted_offers"][claim_number] = user.id
    
    # Clear offers and counter offers for this claim number
    if claim_number in multi_data.get("offers", {}):
        del multi_data["offers"][claim_number]
    if claim_number in multi_data.get("counter_offers", {}):
        del multi_data["counter_offers"][claim_number]
    
    # Notify in comments
    try:
        await context.bot.send_message(
            chat_id=message.chat_id,
            reply_to_message_id=reply_to.message_id,
            text=f"✅ Counter offer accepted! @{user.username if user.username else 'user'} accepts claim #{claim_number} for ${counter_offer_amount:.2f}"
        )
    except Exception as e:
        logger.error(f"Error posting acceptance in comments: {e}")

        # Update sold quantity and price for multiple counter offer
    asyncio.create_task(update_sold_quantity(reply_to.message_id, 1))
    asyncio.create_task(update_post_price(reply_to.message_id, counter_offer_amount))
    
    # Schedule payment reminder
    context.job_queue.run_once(
        payment_reminder, 
        timedelta(hours=12), 
        data=user.id, 
        name=f"payment_reminder_{user.id}"
    )

async def handle_auction_bid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle auction bid messages with reduced spam."""
    user = update.effective_user
    message = update.effective_message
    reply_to = message.reply_to_message
    
    # Check if the message is a reply to an auction post
    if not reply_to or reply_to.message_id not in auction_posts:
        return
    
    # Check if the auction is still active
    auction_data = auction_posts[reply_to.message_id]
    if not auction_data.get("active", False):
        await message.reply_text("This auction has ended.")
        return
    
    # Check if the message contains a valid bid
    bid_match = re.search(r'(\d+(?:\.\d{1,2})?)', message.text)
    if not bid_match:
        return
    
    bid_amount = float(bid_match.group(1))
    
    # Get current highest bid
    current_bids = auction_data["bids"]
    current_highest = max(current_bids.values()) if current_bids else 0
    
    # Check if the bid is higher than current highest
    if bid_amount <= current_highest:
        # Only send message for invalid bids (reduced spam)
        await message.reply_text(f"Your bid of ${bid_amount:.2f} is not higher than the current highest bid of ${current_highest:.2f}.")
        return
    
    # If there was a previous highest bidder, notify them privately
    if current_bids:
        previous_highest_bidder = max(current_bids, key=current_bids.get)
        previous_highest_bid = current_bids[previous_highest_bidder]
    
        if user.id != previous_highest_bidder:  # Don't notify if same user outbid themselves
            # Cooldown check to prevent spam
            current_time = datetime.now()
            should_notify = True
        
            # Check if we've notified this user recently
            last_notification = auction_data.get("last_outbid_notification", {}).get(previous_highest_bidder)
            if last_notification:
                time_since_last = current_time - last_notification
                if time_since_last < timedelta(minutes=2):  # 2-minute cooldown
                    should_notify = False
                    logger.info(f"Skipping outbid notification for {previous_highest_bidder} due to cooldown")
        
            if should_notify:
                try:
                    await context.bot.send_message(
                        chat_id=previous_highest_bidder,
                        text=f"🚨 You've been outbid! 🚨\n\n"
                             f"Your bid of ${previous_highest_bid:.2f} has been exceeded.\n"
                             f"New highest bid: ${bid_amount:.2f}\n\n"
                             f"Reply to the auction post with a higher bid if you want to reclaim the lead! ⚡"
                    )
                    logger.info(f"Notified user {previous_highest_bidder} about being outbid")
                
                    # Update notification tracking
                    if "last_outbid_notification" not in auction_data:
                        auction_data["last_outbid_notification"] = {}
                    auction_data["last_outbid_notification"][previous_highest_bidder] = current_time
                
                except Exception as e:
                    logger.error(f"Error notifying outbid user {previous_highest_bidder}: {e}")
                    # Post in comments if PM fails
                    try:
                        previous_user_info = await context.bot.get_chat(previous_highest_bidder)
                        username = f"@{previous_user_info.username}" if previous_user_info.username else f"User {previous_highest_bidder}"
                        await context.bot.send_message(
                            chat_id=message.chat_id,
                            reply_to_message_id=reply_to.message_id,
                            text=f"⚠️ Could not DM {username} about being outbid. Please check your privacy settings!"
                        )
                    except:
                        pass

    # Record the bid (THIS MUST COME AFTER THE NOTIFICATION CODE)

    # Record the bid
    auction_data["bids"][user.id] = bid_amount
    
    # Check if we need to extend the auction time (only if anti-snipe applies)
    if auction_data.get("anti_snipe", False):
        now = datetime.now()
        end_time = auction_data["end_time"]
        display_end_time = auction_data["display_end_time"]
        time_remaining = end_time - now
        highest_bid = max(current_bids.values())
        bid_count = len(current_bids)
        
        # Check if we're in the anti-snipe window (last minute before end time)
        if time_remaining <= timedelta(minutes=1) and not auction_data.get("extended", False):
            # First extension: 5 minutes
            new_end_time = end_time + timedelta(minutes=5)
            auction_data["end_time"] = new_end_time
            auction_data["extended"] = True
            auction_data["display_end_time"] = display_end_time + timedelta(minutes=5)

            # Reschedule the auction end job
            job_name = f"auction_end_{reply_to.message_id}"
            current_jobs = context.job_queue.get_jobs_by_name(job_name)
            for job in current_jobs:
                job.schedule_removal()

            context.job_queue.run_once(
                lambda ctx: end_auction(ctx, reply_to.message_id),
                new_end_time - datetime.now(),
                name=job_name
            )

            # Notify everyone about the extension in the comments
            await context.bot.send_message(
                chat_id=message.chat_id,
                reply_to_message_id=reply_to.message_id,
                text=
                f"⏰ Auction extended by 5 minutes due to last minute bid! (Anti-Snipe)\n"
                ""
                f"Current highest bid: ${highest_bid:.2f}\n"
                ""
                f"New end time: {auction_data['display_end_time'].strftime('%H%MH')}"
            )
        # If already extended and someone bids in the last minute, extend by 1 minute
        elif auction_data.get("extended", False) and time_remaining <= timedelta(minutes=1):
            # Additional extension: 1 minute
            new_end_time = end_time + timedelta(minutes=1)
            auction_data["end_time"] = new_end_time
            auction_data["display_end_time"] = auction_data["display_end_time"] + timedelta(minutes=1)

            # Reschedule the auction end job
            job_name = f"auction_end_{reply_to.message_id}"
            current_jobs = context.job_queue.get_jobs_by_name(job_name)
            for job in current_jobs:
                job.schedule_removal()

            context.job_queue.run_once(
                lambda ctx: end_auction(ctx, reply_to.message_id),
                new_end_time - datetime.now(),
                name=job_name
            )

            # Notify everyone about the extension in the comments
            await context.bot.send_message(
                chat_id=message.chat_id,
                reply_to_message_id=reply_to.message_id,
                text=
                f"⏰ Auction extended by 1 minute due to ongoing bidding! (Anti-Snipe)\n"
                ""
                f"Current highest bid: ${highest_bid:.2f}\n"
                ""
                f"New end time: {auction_data['display_end_time'].strftime('%H%MH')}"
            )


async def auction_highest_bid_reminder(context: CallbackContext):
    """Remind about the current highest bid in the auction every 30 minutes."""
    message_id = context.job.data
    auction_data = auction_posts.get(message_id)
    
    if not auction_data or not auction_data.get("active", False):
        return
    
    chat_id = auction_data.get("chat_id")
    if not chat_id:
        return
    
    current_bids = auction_data["bids"]
    time_left = auction_data["end_time"] - datetime.now()
    minutes_left = max(0, int(time_left.total_seconds() // 60))
    
    if current_bids:
        highest_bid = max(current_bids.values())
        bid_count = len(current_bids)
        message_text = (
            f"⚡ Auction Update:\n"
            f"🏆 Current highest bid: ${highest_bid:.2f}\n"
            f"📊 Total bids: {bid_count}\n"
            f"⏰ Time left: {minutes_left} minutes\n\n"
            f"Hurry up and place your bids! 🚀"
        )
    else:
        message_text = (
            f"⚡ Auction Update:\n"
            f"❌ No bids yet! Be the first to bid!\n"
            f"⏰ Time left: {minutes_left} minutes\n\n"
            f"Start the bidding! 💰"
        )
    
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            reply_to_message_id=message_id,
            text=message_text
        )
    except Exception as e:
        logger.error(f"Error sending highest bid reminder: {e}")

async def auction_30min_reminder(context: CallbackContext):
    """Remind 30 minutes before auction ends with current highest bid."""
    message_id = context.job.data
    auction_data = auction_posts.get(message_id)
    
    if not auction_data or not auction_data.get("active", False):
        return
    
    chat_id = auction_data.get("chat_id")
    if not chat_id:
        return
    
    current_bids = auction_data["bids"]
    
    if current_bids:
        highest_bid = max(current_bids.values())
        bid_count = len(current_bids)
        message_text = (
            f"⏰⏰⏰ 30 MINUTES LEFT! ⏰⏰⏰\n\n"
            f"🏆 Current highest bid: ${highest_bid:.2f}\n"
            f"📊 Total bids: {bid_count}\n\n"
            f"Last chance to place your bids! Auction ends at {auction_data['display_end_time'].strftime('%H%MH')} 🚨"
        )
    else:
        message_text = (
            f"⏰⏰⏰ 30 MINUTES LEFT! ⏰⏰⏰\n\n"
            f"❌ No bids yet! This auction might end with no winner!\n\n"
            f"Last chance to place your bid! Auction ends at {auction_data['display_end_time'].strftime('%H%MH')} 🚨"
        )
    
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            reply_to_message_id=message_id, # ← This makes it a comment
            text=message_text
        )
    except Exception as e:
        logger.error(f"Error sending 30min reminder: {e}")

async def auction_5min_reminder(context: CallbackContext):
    """Remind 5 minutes before auction ends with current highest bid."""
    message_id = context.job.data
    auction_data = auction_posts.get(message_id)
    
    if not auction_data or not auction_data.get("active", False):
        return
    
    chat_id = auction_data.get("chat_id")
    if not chat_id:
        return
    
    current_bids = auction_data["bids"]
    
    if current_bids:
        highest_bid = max(current_bids.values())
        bid_count = len(current_bids)
        message_text = (
            f"⏰⏰⏰ 5 MINUTES LEFT! ⏰⏰⏰\n\n"
            f"🏆 Current highest bid: ${highest_bid:.2f}\n"
            f"📊 Total bids: {bid_count}\n\n"
            f"Last chance to place your bids! Auction ends at {auction_data['display_end_time'].strftime('%H%MH')} 🚨"
        )
    else:
        message_text = (
            f"⏰⏰⏰ 5 MINUTES LEFT! ⏰⏰⏰\n\n"
            f"❌ No bids yet! This auction might end with no winner!\n\n"
            f"Last chance to place your bid! Auction ends at {auction_data['display_end_time'].strftime('%H%MH')} 🚨"
        )
    
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            reply_to_message_id=message_id, # ← This makes it a comment
            text=message_text
        )
    except Exception as e:
        logger.error(f"Error sending 5min reminder: {e}")

async def auction_1min_reminder(context: CallbackContext):
    """Remind 1 minute before auction ends with current highest bid."""
    message_id = context.job.data
    auction_data = auction_posts.get(message_id)
    
    if not auction_data or not auction_data.get("active", False):
        return
    
    chat_id = auction_data.get("chat_id")
    if not chat_id:
        return
    
    current_bids = auction_data["bids"]
    
    if current_bids:
        highest_bid = max(current_bids.values())
        message_text = (
            f"🚨🚨🚨 FINAL MINUTE! 🚨🚨🚨\n\n"
            f"🏆 Current highest bid: ${highest_bid:.2f}\n\n"
            f"FINAL CHANCE TO BID! Auction ends at {auction_data['display_end_time'].strftime('%H%MH')} ⚡"
        )
    else:
        message_text = (
            f"🚨🚨🚨 FINAL MINUTE! 🚨🚨🚨\n\n"
            f"❌ No bids! Auction ending with no winner in 1 minute!\n\n"
            f"Last chance to bid! Auction ends at {auction_data['display_end_time'].strftime('%H%MH')} ⚡"
        )
    
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            reply_to_message_id=message_id, # ← This makes it a comment
            text=message_text
        )
    except Exception as e:
        logger.error(f"Error sending 1min reminder: {e}")


async def end_auction(context: CallbackContext, message_id: int):
    """End an auction and declare the winner."""
    if message_id not in auction_posts:
        return
    
    auction_data = auction_posts[message_id]
    chat_id = auction_data.get("chat_id")
    
    if not chat_id:
        logger.error(f"No chat_id available for auction end: {message_id}")
        return
    
    # Cancel all reminder jobs for this auction
    reminder_jobs = [
        f"auction_reminder_{message_id}",
        f"auction_5min_{message_id}", 
        f"auction_1min_{message_id}"
    ]
    
    for job_name in reminder_jobs:
        for job in context.job_queue.get_jobs_by_name(job_name):
            job.schedule_removal()
    
    if not auction_data["bids"]:
        # Send notification in comments section of the original post
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                reply_to_message_id=message_id,
                text=f"⏰ Auction ended at {auction_data['display_end_time'].strftime('%H%MH')} with no bids."
            )
        except Exception as e:
            logger.error(f"Error posting auction end message: {e}")
        auction_data["active"] = False
        return
    
    # Get the highest bidder
    highest_bidder_id = max(auction_data["bids"], key=auction_data["bids"].get)
    highest_bid = auction_data["bids"][highest_bidder_id]

    # Get the highest bidder's username
    try:
        highest_bidder_info = await context.bot.get_chat(highest_bidder_id)
        highest_bidder_username = f"@{highest_bidder_info.username}" if highest_bidder_info.username else f"User {highest_bidder_id}"
    except:
        highest_bidder_username = f"User {highest_bidder_id}"

    # Notify the channel in comments section of the original post
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            reply_to_message_id=message_id,
            text=f"⏰ Auction ended at {auction_data['display_end_time'].strftime('%H%MH')}! Highest bid: ${highest_bid:.2f} by {highest_bidder_username}\n\n"
                 f"Daddy will confirm the winner shortly."
        )
    except Exception as e:
        logger.error(f"Error posting auction end message: {e}")

            # Clear notification tracking (if it exists)
    if "last_outbid_notification" in auction_data:
        del auction_data["last_outbid_notification"]
    
    auction_data["active"] = False

async def handle_auction_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin confirmation of auction results from any source."""
    message = update.effective_message
    reply_to = message.reply_to_message
    
    # Check if the message is a reply to either:
    # 1. The original auction post, OR
    # 2. A bid message from the highest bidder
    if not reply_to:
        return
    
    # Check if replying to auction post
    is_replying_to_auction = reply_to.message_id in auction_posts
    
    # Check if replying to a bid message (we need to find which auction it belongs to)
    is_replying_to_bid = False
    auction_message_id = None

    if not is_replying_to_auction and reply_to.from_user:
        # Search through all auctions to find if this is a reply to a bid message
        for auction_id, auction_data in auction_posts.items():
            # Check if the user who made the replied message has a bid in this auction
            if reply_to.from_user.id in auction_data["bids"]:
                is_replying_to_bid = True
                auction_message_id = auction_id
                break
            
            # Additional check: if the replied message looks like a bid message
            bid_text = reply_to.text or ""
            if re.search(r'\$?\d+(?:\.\d{2})?', bid_text):
                is_replying_to_bid = True
                auction_message_id = auction_id
                break
    
    if not is_replying_to_auction and not is_replying_to_bid:
        return
    
    # Get user info - could be from a user or channel
    user = update.effective_user
    
    # Allow admin confirmation from:
    # 1. Admin users (by user ID)
    is_admin_user = user and user.id in ADMIN_IDS
    
    # 2. Channel messages (check if sender chat is in admin IDs)
    is_channel_message = False
    if hasattr(message, 'sender_chat') and message.sender_chat:
        is_channel_message = message.sender_chat.id in ADMIN_IDS
    
    # 3. Also check if the message is from a channel that forwarded the message
    is_forward_from_channel = False
    if hasattr(message, 'forward_from_chat') and message.forward_from_chat:
        is_forward_from_channel = message.forward_from_chat.id in ADMIN_IDS
    
    if not (is_admin_user or is_channel_message or is_forward_from_channel):
        return
    
    # Get the auction data
    if is_replying_to_auction:
        auction_data = auction_posts[reply_to.message_id]
        auction_message = reply_to
    else:
        auction_data = auction_posts[auction_message_id]
        # Get the original auction message
        try:
            auction_message = await context.bot.get_message(
                chat_id=message.chat_id,
                message_id=auction_message_id
            )
        except:
            return
    
    # Check if auction is still active
    if auction_data.get("active", False):
        # Reply to the original message that was replied to
        await context.bot.send_message(
            chat_id=message.chat_id,
            reply_to_message_id=reply_to.message_id,
            text="⏰ Auction is still active! There is still time to bid. Please wait for the auction to end before confirming."
        )
        return
    
    if not auction_data["bids"]:
        return
    
    # Get the highest bidder
    highest_bidder_id = max(auction_data["bids"], key=auction_data["bids"].get)
    highest_bid = auction_data["bids"][highest_bidder_id]
    
    # If replying to a bid message, check if it's from the highest bidder
    if is_replying_to_bid and reply_to.from_user.id != highest_bidder_id:
        await message.reply_text("⚠️ You can only confirm to the highest bidder's message!")
        return
    
    # Extract card name from the ORIGINAL auction post
    auction_text = auction_message.text or auction_message.caption or ""
    
    # Extract card name - look for text after "Auction" and before price/RP info
    card_name_match = re.search(r'(?:Auction|auction)\s+(.+?)(?:\n|\$|RP|$)', auction_text, re.IGNORECASE | re.DOTALL)
    
    if card_name_match:
        card_name = card_name_match.group(1).strip()
        # Clean up any extra whitespace or line breaks
        card_name = re.sub(r'\s+', ' ', card_name)
    else:
        # Fallback: use first meaningful line after removing "Auction"
        lines = auction_text.split('\n')
        card_name = "Auction Item"
        for line in lines:
            if line.strip() and not re.match(r'^(Auction|auction|RP|\$|Ends)', line.strip(), re.IGNORECASE):
                card_name = line.strip()[:30]
                break
    
    message_text = message.text or ""
    
    # Check if admin confirms with exactly "yours" or "urs" (case insensitive, single word)
    if re.fullmatch(r'(yours|urs)', message_text.strip(), re.IGNORECASE):
        logger.info(f"Auction confirmation detected for text: '{message_text}'")
        
        # Check if this auction has already been confirmed
        if auction_data.get("confirmed"):
            # Reply to the original message that was replied to
            await context.bot.send_message(
                chat_id=message.chat_id,
                reply_to_message_id=reply_to.message_id,
                text="⚠️ This auction has already been confirmed!"
            )
            return
            
        # Mark auction as confirmed to prevent duplicate confirmations
        auction_data["confirmed"] = True
        
        # Initialize user data if not exists
        if highest_bidder_id not in user_claims:
            try:
                user_info = await context.bot.get_chat(highest_bidder_id)
                username = f"@{user_info.username}" if user_info.username else "No username"
                full_name = user_info.first_name + (f" {user_info.last_name}" if user_info.last_name else "")
            except:
                username = "Unknown"
                full_name = "Unknown"
                
            user_claims[highest_bidder_id] = {
                "items": [], 
                "total": 0, 
                "username": username,
                "full_name": full_name,
                "payment_status": "unpaid",
                "shipping_method": None,
                "tracking_number": None
            }
        
        # Add the auction win to user's claims
        user_data = user_claims[highest_bidder_id]
        user_data["items"].append({
            "text": card_name,
            "price": highest_bid,
            "message_id": auction_message.message_id,  # Use auction post ID, not bid message ID
            "timestamp": datetime.now(),
            "quantity": 1,
            "is_auction": True
        })
        user_data["total"] += highest_bid
        
        # ✅ FIX: Update sold quantity and price when auction is WON (card released)
        asyncio.create_task(update_sold_quantity(auction_message.message_id, 1))
        asyncio.create_task(update_post_price(auction_message.message_id, highest_bid))
        
        # Notify the winner via private message
        try:
            await context.bot.send_message(
                chat_id=highest_bidder_id,
                text=f"🎉 Congratulations! You won {card_name} for ${highest_bid:.2f}!\n\n"
                     f"Please use /invoice to see your total and /pay to confirm payment."
            )
        except Exception as e:
            logger.error(f"Error notifying auction winner {highest_bidder_id}: {e}")
        
        # Get the highest bidder's username
        try:
            highest_bidder_info = await context.bot.get_chat(highest_bidder_id)
            highest_bidder_username = f"@{highest_bidder_info.username}" if highest_bidder_info.username else f"User {highest_bidder_id}"
        except:
            highest_bidder_username = f"User {highest_bidder_id}"

        # Post confirmation in comments - reply to the ORIGINAL AUCTION POST
        try:
            await context.bot.send_message(
                chat_id=message.chat_id,
                reply_to_message_id=auction_message.message_id,
                text=f"✅ Auction confirmed by Daddy! {card_name} sold to {highest_bidder_username} for ${highest_bid:.2f}"
            )
        except Exception as e:
            logger.error(f"Error posting auction confirmation: {e}")

        logger.info(f"Auction confirmed by Daddy: {card_name} sold to user {highest_bidder_id} for ${highest_bid:.2f}")
        auction_data["active"] = False
        
    # Check if admin rejects with "RP not met"
    elif re.search(r'rp not met', message_text, re.IGNORECASE):
        # Check if this auction has already been confirmed/rejected
        if auction_data.get("confirmed") is not None:
            await context.bot.send_message(
                chat_id=message.chat_id,
                reply_to_message_id=reply_to.message_id,
                text="⚠️ This auction has already been processed!"
            )
            return
            
        # Mark auction as rejected
        auction_data["confirmed"] = False
        
        # ✅ FIX: Update ONLY the price in Google Sheets when RP not met (DO NOT update sold quantity)
        # This records the final bid price but keeps availability unchanged
        asyncio.create_task(update_post_price(auction_message.message_id, highest_bid))
        
        # Notify the channel in comments - reply to the ORIGINAL AUCTION POST
        try:
            await context.bot.send_message(
                chat_id=message.chat_id,
                reply_to_message_id=auction_message.message_id,
                text=f"❌ Auction rejected: Daddy say RP is not met. Unfortunately, card cannot be released.\n"
                     f"Final bid: ${highest_bid:.2f}"
            )
        except Exception as e:
            logger.error(f"Error posting auction rejection: {e}")

        
        # Notify the highest bidder via private message
        try:
            await context.bot.send_message(
                chat_id=highest_bidder_id,
                text=f"For {card_name}, the RP is not met, thus the card is unreleased. Thank you for your participation!\n"
                     f"Final bid: ${highest_bid:.2f}"
            )
        except Exception as e:
            logger.error(f"Error notifying auction participant {highest_bidder_id}: {e}")
        
        logger.info(f"Auction rejected: Daddy say RP not met for {card_name}, highest bid was ${highest_bid:.2f}")
        auction_data["active"] = False

async def handle_ai_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle ALL user messages with AI in private chats"""
    user = update.effective_user
    user_message = update.message.text
    
    logger.info(f"🎯 AI HANDLER - User {user.id} in {update.effective_chat.type}: '{user_message}'")
    
    # Skip commands
    if user_message.startswith('/'):
        logger.info(f"🎯 AI SKIP - Command detected: '{user_message}'")
        return
    
    # Only handle private chats
    if update.effective_chat.type != "private":
        logger.info(f"🎯 AI SKIP - Not private chat: {update.effective_chat.type}")
        return
    
    # Skip replies (let reply handlers work)
    if update.message.reply_to_message:
        logger.info(f"🎯 AI SKIP - Reply detected")
        return
    
    logger.info(f"🎯 AI PROCESSING - User {user.id}: '{user_message}'")
    
    try:
        # Show typing action
        await update.message.chat.send_action(action="typing")
        
        # Get conversation history
        history = conversation_memory.get_history(user.id)
        logger.info(f"🎯 AI History length: {len(history)}")
        
        # Get AI response
        ai_response = await get_ai_response(user_message, history)
        
        # Update conversation memory
        conversation_memory.add_message(user.id, "user", user_message)
        conversation_memory.add_message(user.id, "assistant", ai_response)
        
        logger.info(f"🎯 AI RESPONSE SENT: '{ai_response[:50]}...'")
        await update.message.reply_text(ai_response)
        
    except Exception as e:
        logger.error(f"🎯 AI ERROR: {e}")
        await update.message.reply_text("❌ Sorry, I encountered an error. Going to explode! Please try again or use the commands directly.")

async def get_ai_response(user_message: str, conversation_history: list = None) -> str:
    """Get intelligent response from DeepSeek - now handles ALL bot functionality"""
    logger.info(f"🤖 AI Processing message: '{user_message}'")
    
    try:
        messages = []
        
        # Enhanced system prompt that knows about ALL bot functions
        system_prompt = """
        You are PokaiShop Assistant Intern, the intelligent AI intern for PokaiShop Pokémon card business. 
        You have access to ALL bot functions and can help users with everything.

        BOT FUNCTIONS YOU CAN GUIDE USERS TO:
        
        CLAIMS & ORDERS:
        - /list - View claimed items
        - /invoice - Check invoice total
        - /pay - Confirm payment
        - /shipping - Choose shipping method
        - /undo - Change shipping method
        
        SHIPPING INFO:
        - TikTok Mailer: https://vt.tiktok.com/ZSSjdaqEg/
        - Self-collect: FASTPOKE MART (2 Marina Boulevard, #B1-08A, The Sail @ Marina Bay, 018987)
        - Storage: Max 2 months
        
        PAYMENT:
        - Transfer to: 98168898
        - Payment deadline: 12 hours
        - Contact after payment: Daddy @kykaikai03
        
        GENERAL:
        - Contact: @kykaikai03
        - TikTok: https://www.tiktok.com/@pokaishop5
        
        HOW TO HELP:
        1. If user asks about their claims/invoice, guide them to use /list or /invoice and tell them to press /pay after they have paid
        2. If user wants to pay, guide them to /pay and tell them to send Daddy @kykaikai03 screenshot of their payment
        3. If user asks about shipping, explain options and guide to /shipping
        4. For Pokémon card questions, answer knowledgeably
        5. Always be friendly, helpful, and conversational
        6. If unsure, direct to contact Daddy @kykaikai03
        7. When user ask about Daddy, reply with "😂 The secret is ... Daddy just got a new child, and that child is YOU! Welcome to the family! 👶"
        
        Remember: You're the friendly face of PokaiShop!
        """
        
        messages.append({"role": "system", "content": system_prompt})
        
        # Add conversation history if available
        if conversation_history:
            messages.extend(conversation_history)
        
        # Add current user message
        messages.append({"role": "user", "content": user_message})
        
        logger.info(f"🤖 Sending request to DeepSeek API...")
        
        # DeepSeek API call
        async with httpx.AsyncClient() as client:
            response = await client.post(
                DEEPSEEK_API_URL,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {DEEPSEEK_API_KEY}"
                },
                json={
                    "model": "deepseek-chat",
                    "messages": messages,
                    "max_tokens": 500,
                    "temperature": 0.7,
                    "stream": False
                },
                timeout=30.0
            )
            
            logger.info(f"🤖 DeepSeek API response status: {response.status_code}")
            
            if response.status_code == 200:
                result = response.json()
                ai_response = result["choices"][0]["message"]["content"].strip()
                logger.info(f"🤖 AI Response generated: '{ai_response[:100]}...'")
                return ai_response
            else:
                logger.error(f"DeepSeek API error: {response.status_code} - {response.text}")
                return "I'm having trouble thinking right now. My brain is exploding! Please try using the commands directly or contact Daddy @kykaikai03 for help! Go away before I explode!"
        
    except Exception as e:
        logger.error(f"Error getting AI response: {e}")
        return "I'm having trouble thinking right now. My brain is exploding! Please try using the commands directly or contact Daddy @kykaikai03 for help! Go away before I explode!"

async def setup_auction_post(message, context, text):
    """Set up auction post with all necessary data."""
    # Extract end date and time
    end_time = None
    display_end_time = None
    anti_snipe = False
    
    # Check for anti-snipe setting
    anti_snipe_match = re.search(r'anti-snipe\s*(applies|yes|true)', text, re.IGNORECASE)
    if anti_snipe_match:
        anti_snipe = True
    
    # Try to extract date and time
    date_time_match = re.search(r'ends\s*(\d{1,2}/\d{1,2}/\d{4}),\s*(?:today|tomorrow|friday|monday|tuesday|wednesday|thursday|saturday|sunday),\s*(\d{4})h', text, re.IGNORECASE)
    if date_time_match:
        date_str, time_str = date_time_match.groups()
        try:
            day, month, year = map(int, date_str.split('/'))
            hour, minute = int(time_str[:2]), int(time_str[2:])
            
            # Create display time (what user sees - 2150H)
            display_end_time = datetime(year, month, day, hour, minute)
            
            # Create actual end time (2151H - bids until 2150:59 are valid)
            end_time = datetime(year, month, day, hour, minute) + timedelta(minutes=1)
            
        except (ValueError, IndexError):
            pass
    
    # If no specific end time found, default to 24 hours + 1 minute
    if end_time is None:
        end_time = datetime.now() + timedelta(hours=24, minutes=1)
        display_end_time = datetime.now() + timedelta(hours=24)
        logger.info(f"Using default 24-hour auction for message {message.message_id}")
    
    # Check if the end time is in the past
    if end_time and end_time < datetime.now():
        await message.reply_text(
            f"❌ Invalid auction! Daddy! The end time {display_end_time.strftime('%d/%m/%Y, %H%MH')} has already passed. "
            f"Please create a new auction with a valid future end time."
        )
        return
    
    # Set up auction data
    auction_posts[message.message_id] = {
        "bids": {},
        "end_time": end_time,
        "display_end_time": display_end_time,
        "extended": False,
        "active": True,
        "anti_snipe": anti_snipe,
        "chat_id": message.chat_id,
        "confirmed": None,
        "last_outbid_notification": {}
    }

    # Schedule reminders only if we have valid end times
    if end_time and display_end_time:
        time_until_end = end_time - datetime.now()
        
        # Schedule highest bid reminder every 30 minutes
        context.job_queue.run_repeating(
            auction_highest_bid_reminder,
            interval=timedelta(minutes=120),
            first=timedelta(minutes=5),
            data=message.message_id,
            name=f"auction_reminder_{message.message_id}"
        )

        # Schedule other reminders...
        # [Keep your existing reminder scheduling code here]
        
        # Schedule auction end
        context.job_queue.run_once(
            lambda ctx: end_auction(ctx, message.message_id),
            time_until_end,
            name=f"auction_end_{message.message_id}"
        )
    
    # Notify channel about the auction
    anti_snipe_text = "with anti-snipe" if anti_snipe else "without anti-snipe"

    await message.reply_text(
        f"⚡ Auction detected! This item is up for auction.\n\n"
        f"To bid, reply to this post with your bid amount (e.g., '10' or '$10').\n\n"
        f"Auction ends at {display_end_time.strftime('%d/%m/%Y, %H%MH')} {anti_snipe_text}.\n\n"
        f"Type /auction_status in this post to check current bids.",
        reply_to_message_id=message.message_id
    )

async def setup_multi_post(message, text):
    """Set up multiple claim post."""
    if message.message_id not in multiple_claims:
        multiple_claims[message.message_id] = {
            "type": "multi",
            "claims": {},
            "waitlist": {},
            "offers": {},
            "counter_offers": {},
            "accepted_offers": {}
        }
    # Auto-detect available quantity for multi posts
    available_quantity = 10  # Default for multi posts
    
    # Update Google Sheets with correct quantity
    if message.message_id in sheets_tracking:
        sheets_tracking[message.message_id]["available_quantity"] = available_quantity

async def setup_regular_post(message, text):
    """Set up regular claim post."""
    # Extract quantity for regular posts
    quantity_match = re.search(r'(?:qty|quantity)[:\s]*(\d+)', text, re.IGNORECASE)
    available_quantity = int(quantity_match.group(1)) if quantity_match else 1
    
    # Update Google Sheets with correct quantity if needed
    if message.message_id in sheets_tracking:
        current_qty = sheets_tracking[message.message_id].get("available_quantity", 0)
        if current_qty != available_quantity:
            sheets_tracking[message.message_id]["available_quantity"] = available_quantity
            sheets_tracking[message.message_id]["original_available_quantity"] = available_quantity
            logger.info(f"🔄 Updated claim post {message.message_id} quantity from {current_qty} to {available_quantity}")
            
async def payment_reminder(context: CallbackContext):
    """Send payment reminder to user."""
    user_id = context.job.data
        
    if user_id in user_claims and user_claims[user_id].get("payment_status") == "unpaid":
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="⏰ Reminder: Please pay my Daddy within the next 12 hours to avoid your items being rebidded or confiscated by me!\n\n"
                     "After you have paid, use /pay to confirm your payment."
            )
        except Exception as e:
            logger.error(f"Error sending payment reminder to {user_id}: {e}")

@private_chat_only
async def pay_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /pay command to confirm payment."""
    user = update.effective_user
    
    if user.id not in user_claims or not user_claims[user.id]["items"]:
        await update.message.reply_text("You have no items to pay for.")
        return ConversationHandler.END
    
    # Update payment status with timestamp
    user_claims[user.id]["payment_status"] = "paid"
    user_claims[user.id]["payment_time"] = datetime.now()
    user_claims[user.id]["last_reminder_sent"] = None  # Reset reminder tracking


    
    # Cancel the payment reminder job
    job_name = f"payment_reminder_{user.id}"
    current_jobs = context.job_queue.get_jobs_by_name(job_name)
    for job in current_jobs:
        job.schedule_removal()
    
    # Schedule reset after 4 hours
    context.job_queue.run_once(
        reset_user_after_payment, 
        timedelta(hours=4), 
        data=user.id, 
        name=f"reset_after_payment_{user.id}"
    )
    
    # Show shipping options after payment
    keyboard = [['TikTok Mailer', 'Self-collect', 'Storage']]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True)
    
    await update.message.reply_text(
        "✅ Payment confirmed! Thank you!\n\n"
        "Please send your payment SS to Daddy @kykaikai03!\n"
        "Your invoice will be reset in 4 hours for new claims.\n"
        "Please choose your shipping method:",
        reply_markup=reply_markup
    )
    
    return SHIPPING_METHOD



async def reset_user_after_payment(context: CallbackContext):
    """Reset user claims and invoice 4 hours after payment."""
    user_id = context.job.data
    
    if user_id in user_claims:
        # Reset user data but keep some basic info
        username = user_claims[user_id].get("username", "No username")
        full_name = user_claims[user_id].get("full_name", "Unknown")
        
        user_claims[user_id] = {
            "items": [], 
            "total": 0, 
            "username": username,
            "full_name": full_name,
            "payment_status": "unpaid",  # Reset payment status
            "shipping_method": None,
            "tracking_number": None
        }
        
        logger.info(f"Reset claims and invoice for user {user_id} after payment")
        
        # Notify user
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="🔄 Your invoice has been reset. You can now start claiming new items!\n\n"
                     "Use /start to see available commands."
            )
        except Exception as e:
            logger.error(f"Error notifying user about reset: {e}")

@private_chat_only   
async def shipping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /shipping command to choose shipping method."""
    user = update.effective_user
    
    if user.id not in user_claims or not user_claims[user.id]["items"]:
        await update.message.reply_text("You have no items to ship.")
        return ConversationHandler.END
    
    # Verify if user has paid first
    if user_claims[user.id].get("payment_status") != "paid":
        await update.message.reply_text(
            "⚠️ Please complete your payment first using /pay before selecting shipping options.\n\n"
            "After payment is confirmed, you can choose your shipping method."
        )
        return ConversationHandler.END
    
    # Show shipping options keyboard
    keyboard = [['TikTok Mailer', 'Self-collect', 'Storage']]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True)
    
    await update.message.reply_text(
        "Please choose your shipping method:",
        reply_markup=reply_markup
    )
    
    return SHIPPING_METHOD

async def shipping_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle shipping method selection."""
    user = update.effective_user
    method = update.message.text
    
    if user.id not in user_claims:
        await update.message.reply_text("Please start with /start first.")
        return ConversationHandler.END
    
    # Verify payment status again (in case someone bypasses the command)
    if user_claims[user.id].get("payment_status") != "paid":
        await update.message.reply_text(
            "⚠️ Payment required before selecting shipping options.\n\n"
            "Please use /pay to confirm your payment first."
        )
        return ConversationHandler.END
    
    user_claims[user.id]["shipping_method"] = method
    user_claims[user.id]["shipping_time"] = datetime.now()
    
    if method == "TikTok Mailer":
        await update.message.reply_text(
            "Please order via TikTok Mailer here:\n"
            "https://vt.tiktok.com/ZSSjdaqEg/\n\n"
            "After ordering, please send Daddy @kykaikai03 your TikTok Order ID (last 5 digits). \n"
            "Your items will be shipped within 3-5 business days after payment confirmation.",
            reply_markup=ReplyKeyboardRemove()
        )
    elif method == "Self-collect":
        await update.message.reply_text(
            "You've chosen self-collection.\n"
            "📍 Address: 018987, FASTPOKE MART\n"
            "⏰ Collection hours: Monday-Friday, 11.30AM - 7.00PM.\n"
            "You can let the shop owner know your TELEGRAM display name at the shop!\n"
            "Let Daddy @kykaikai03 know 1 day before you come and 2 hours before you collect your item. Thank you!",
            reply_markup=ReplyKeyboardRemove()
        )
    else:  # Storage
        # Set storage deadline (2 months from now)
        storage_deadline = datetime.now() + timedelta(days=60)
        user_claims[user.id]["storage_deadline"] = storage_deadline
        
        await update.message.reply_text(
            "You've chosen to store your items with us.\n"
            f"📦 Maximum storage period: 2 months (until {storage_deadline.strftime('%d/%m/%Y')})\n"
            "Please note: We do not track storage time automatically.\n"
            "You are responsible for arranging shipping within 2 months.\n"
            "After reminders, if not collected after 2 months, all items will be confiscated."
            "Thank you for your claims!",
            reply_markup=ReplyKeyboardRemove()
        )
    
    # Notify admin with detailed shipping information via PM (not group chat)
    for admin_id in ADMIN_IDS:
        try:
            # Send PM to admin, not to group chat
            await context.bot.send_message(
                chat_id=admin_id,  # This sends to admin's private messages
                text=f"🚚 Shipping method selected by @{user.username if user.username else 'No username'}\n"
                     f"📦 Method: {method}\n"
                     f"💰 Total: ${user_claims[user.id]['total']:.2f}\n"
                     f"📋 Items: {len(user_claims[user.id]['items'])}\n"
                     f"⏰ Selected at: {datetime.now().strftime('%d/%m/%Y %H:%M')} until {storage_deadline.strftime('%d/%m/%Y')}\n"
            )
        except Exception as e:
            logger.error(f"Error notifying admin {admin_id}: {e}")
    
    return ConversationHandler.END

@private_chat_only
async def undo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /undo command to change shipping method."""
    user = update.effective_user
    
    if user.id not in user_claims or not user_claims[user.id]["items"]:
        await update.message.reply_text("You have no items to modify shipping for.")
        return ConversationHandler.END
    
    # Check if user has already selected a shipping method
    if not user_claims[user.id].get("shipping_method"):
        await update.message.reply_text("You haven't selected a shipping method yet. Use /shipping to choose one.")
        return ConversationHandler.END
    
    # Show confirmation for undo
    keyboard = [['Yes, change shipping method', 'No, keep current method']]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True)
    
    current_method = user_claims[user.id]["shipping_method"]
    await update.message.reply_text(
        f"Your current shipping method is: {current_method}\n\n"
        "Do you want to change your shipping method?",
        reply_markup=reply_markup
    )
    
    return UNDO_SHIPPING

async def undo_shipping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle undo shipping confirmation."""
    user = update.effective_user
    choice = update.message.text
    
    if choice == 'No, keep current method':
        await update.message.reply_text(
            "Okay, keeping your current shipping method.",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END
    
    # Clear the current shipping method
    user_claims[user.id]["shipping_method"] = None
    if "storage_deadline" in user_claims[user.id]:
        del user_claims[user.id]["storage_deadline"]
    
    # Show the 3 shipping options keyboard again
    keyboard = [['TikTok Mailer', 'Self-collect', 'Storage']]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True)
    
    await update.message.reply_text(
        "Shipping method reset. Please choose your shipping method again:",
        reply_markup=reply_markup
    )
    
    # Notify admin about the change
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"🔄@{user.username if user.username else 'No username'} reset their shipping method.\n"
                     f"They are now selecting a new method."
            )
        except Exception as e:
            logger.error(f"Error notifying admin {admin_id}: {e}")
    
    return SHIPPING_METHOD

@private_chat_only
async def list_claims(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a list of all claimed items when the command /list is issued."""
    user = update.effective_user
    
    if user.id not in user_claims or not user_claims[user.id]["items"]:
        await update.message.reply_text("No claim data found.")
        return
    
    items_list = []
    for item in user_claims[user.id]["items"]:

              # Clean up the item name (remove any price/quantity info that might be there)
        item_name = item['text']
        # Extract only the part before any newline (in case it contains multiple lines)
        item_name = item_name.split('\n')[0].strip()
        item_name = re.sub(r'\$.*', '', item_name).strip()
        item_name = re.sub(r'(?i)(qty|quantity).*', '', item_name).strip()
        
        # Format as name - $price
        items_list.append(f"• {item_name} - ${item['price']:.2f}")
    
    message_text = "Your claimed items:\n" + "\n".join(items_list)
    
    # Add total at the bottom
    total = user_claims[user.id]["total"]
    message_text += f"\n\nTotal: ${total:.2f}"
    
    await update.message.reply_text(message_text)


@private_chat_only
async def invoice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send invoice total when the command /invoice is issued."""
    user = update.effective_user
    
    if user.id not in user_claims or not user_claims[user.id]["items"]:
        await update.message.reply_text("No claim data found.")
        return
    
    total = user_claims[user.id]["total"]
    status = user_claims[user.id].get("payment_status", "unpaid")
    
    message = f"Your invoice total: ${total:.2f}\n"
    message += f"Payment status: {'✅ Paid' if status == 'paid' else '❌ Unpaid'}\n\n"
    message += "Please transfer to 98168898 within 12 hours."
    
    await update.message.reply_text(message)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the current conversation."""
    await update.message.reply_text(
        'Operation cancelled.',
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END

@private_chat_only
@admin_only
async def test_ai_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Test if AI can respond to a direct message"""
    user = update.effective_user
    await update.message.reply_text("🤖 Testing AI response...")
    
    # Test the AI directly
    try:
        test_response = await get_ai_response("Hello, are you working?", [])
        await update.message.reply_text(f"✅ AI Response:\n{test_response}")
    except Exception as e:
        await update.message.reply_text(f"❌ AI Error: {e}")

@private_chat_only
@admin_only
async def debug_multiple(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Debug command to see multiple claims state (admin only)."""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return
    
    debug_info = "🔍 MULTIPLE CLAIMS DEBUG INFO\n\n"
    
    for msg_id, multi_data in multiple_claims.items():
        debug_info += f"📝 Message {msg_id}:\n"
        debug_info += f"  Claims: {multi_data.get('claims', {})}\n"
        debug_info += f"  Waitlist: {multi_data.get('waitlist', {})}\n"
        debug_info += f"  Offers: {multi_data.get('offers', {})}\n"
        debug_info += f"  Counter Offers: {multi_data.get('counter_offers', {})}\n"
        debug_info += f"  Accepted Offers: {multi_data.get('accepted_offers', {})}\n\n"
    
    if not multiple_claims:
        debug_info += "No multiple claims data found."
    
    await update.message.reply_text(debug_info)


@private_chat_only
@admin_only
async def reminder_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to remind all users with unpaid items to pay."""
    user = update.effective_user
    
    # Check if user is admin
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("This command is for Daddy only.")
        return
    
    # Find all users with unpaid items
    users_to_remind = []
    for user_id, data in user_claims.items():
        if data["items"] and data.get("payment_status") == "unpaid":
            users_to_remind.append((user_id, data))
    
    if not users_to_remind:
        await update.message.reply_text("✅ No users need payment reminders - all paid up!")
        return
    
    # Send reminders
    success_count = 0
    fail_count = 0
    failed_users = []
    
    for user_id, user_data in users_to_remind:
        try:
            total_amount = user_data["total"]
            item_count = len(user_data["items"])
            username = user_data["username"]
            
            # Create reminder message
            reminder_message = (
                f"⏰ PAYMENT REMINDER FROM DADDY ⏰\n\n"
                f"You have {item_count} item(s) totaling ${total_amount:.2f} that are still unpaid. Press /list to view items.\n\n"
                f"💳 Please transfer ${total_amount:.2f} to 98168898\n"
                f"📱 After payment, send SS to daddy @kykaikai03 and press /pay to confirm your payment\n\n"
                f"⏳ Please complete payment within 12 hours to avoid penalty and items being confiscated!\n\n"
                f"Contact @kykaikai03 if you have any questions."
            )
            
            await context.bot.send_message(
                chat_id=user_id,
                text=reminder_message
            )
            success_count += 1
            logger.info(f"Sent payment reminder to user {user_id} ({username})")
            
        except Exception as e:
            fail_count += 1
            failed_users.append(user_data["username"])
            logger.error(f"Error sending reminder to {user_id}: {e}")
    
    # Send summary to admin
    summary_message = (
        f"📊 REMINDER SUMMARY\n\n"
        f"✅ Successfully sent: {success_count} users\n"
        f"❌ Failed to send: {fail_count} users\n"
    )
    
    if failed_users:
        summary_message += f"\nFailed users:\n" + "\n".join(failed_users)
    
    await update.message.reply_text(summary_message)

@private_chat_only
@admin_only
async def smart_reminder_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to remind users with unpaid items, with smart tracking."""
    user = update.effective_user
    
    # Check if user is admin
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("This command is for Daddy only.")
        return
    
    # Find all users with unpaid items
    users_to_remind = []
    now = datetime.now()
    
    for user_id, data in user_claims.items():
        if data["items"] and data.get("payment_status") == "unpaid":
            # Check if we sent a reminder recently (within 6 hours)
            last_reminder = data.get("last_reminder_sent")
            if last_reminder and (now - last_reminder) < timedelta(hours=6):
                continue  # Skip if reminder was sent recently
            users_to_remind.append((user_id, data))
    
    if not users_to_remind:
        await update.message.reply_text("✅ No users need payment reminders - all either paid or were reminded recently!")
        return
    
    # Send reminders
    success_count = 0
    fail_count = 0
    failed_users = []
    
    for user_id, user_data in users_to_remind:
        try:
            total_amount = user_data["total"]
            item_count = len(user_data["items"])
            username = user_data["username"]
            
            # Create detailed reminder message
            items_list = []
            for item in user_data["items"][:5]:  # Show first 5 items
                item_name = item['text'].split('\n')[0][:30]  # Shorten long names
                items_list.append(f"• {item_name} - ${item['price']:.2f}")
            
            if len(user_data["items"]) > 5:
                items_list.append(f"• ... and {len(user_data['items']) - 5} more items")
            
            reminder_message = (
                f"⏰ PAYMENT REMINDER FROM DADDY ⏰\n\n"
                f"You have {item_count} item(s) totaling ${total_amount:.2f}\n\n"
            )
            
            if items_list:
                reminder_message += "Your items:\n" + "\n".join(items_list) + "\n\n"
            
            reminder_message += (
                f"💳 Please transfer ${total_amount:.2f} to 98168898\n"
                f"📱 After payment, use /pay to confirm\n\n"
                f"⏳ Complete payment within 12 hours to avoid losing your items!\n\n"
                f"Contact @kykaikai03 for help."
            )
            
            await context.bot.send_message(
                chat_id=user_id,
                text=reminder_message
            )
            
            # Update last reminder timestamp
            user_claims[user_id]["last_reminder_sent"] = now
            success_count += 1
            logger.info(f"Sent payment reminder to user {user_id} ({username})")
            
        except Exception as e:
            fail_count += 1
            failed_users.append(user_data["username"])
            logger.error(f"Error sending reminder to {user_id}: {e}")
    
    # Send detailed summary to admin
    summary_message = (
        f"📊 SMART REMINDER SUMMARY\n\n"
        f"✅ Successfully sent: {success_count} users\n"
        f"❌ Failed to send: {fail_count} users\n"
        f"⏰ Time: {now.strftime('%d/%m/%Y %H:%M')}\n"
    )
    
    if failed_users:
        summary_message += f"\nFailed to reach:\n" + "\n".join(failed_users)
    
    # Also show who was reminded
    reminded_users = [data['username'] for user_id, data in users_to_remind if user_data['username'] not in failed_users]
    if reminded_users:
        summary_message += f"\n\nReminded users:\n" + "\n".join(reminded_users[:10])  # Show first 10
        if len(reminded_users) > 10:
            summary_message += f"\n... and {len(reminded_users) - 10} more"
    
    await update.message.reply_text(summary_message)

async def auction_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check current auction status - available to everyone in groups."""
    message = update.effective_message
    reply_to = message.reply_to_message
    
    if not reply_to or reply_to.message_id not in auction_posts:
        await message.reply_text("Please reply to an auction post to check its status.")
        return
    
    auction_data = auction_posts[reply_to.message_id]
    
    if not auction_data.get("active", False):
        await message.reply_text("This auction has ended.")
        return
    
    current_bids = auction_data["bids"]
    time_left = auction_data["end_time"] - datetime.now()
    minutes_left = max(0, int(time_left.total_seconds() // 60))
    
    if current_bids:
        highest_bid = max(current_bids.values())
        bid_count = len(current_bids)
        message_text = f"⚡ Auction Status:\n🏆 Highest bid: ${highest_bid:.2f}\n📊 Total bids: {bid_count}\n⏰ Time left: {minutes_left} minutes"
    else:
        message_text = f"⚡ Auction Status:\n❌ No bids yet!\n⏰ Time left: {minutes_left} minutes"
    
    await message.reply_text(message_text, reply_to_message_id=reply_to.message_id)

@private_chat_only
@admin_only
async def tracking_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to add tracking information for a user."""
    user = update.effective_user
    
    # Check if user is admin
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("This command is for admins only.")
        return
    
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Usage: /tracking @username tracking_number")
        return
    
    username = context.args[0].lstrip('@')
    tracking_number = ' '.join(context.args[1:])
    
    # Find user
    found_user_id = None
    for user_id, data in user_claims.items():
        if data['username'].lstrip('@') == username:
            found_user_id = user_id
            break
    
    if not found_user_id:
        await update.message.reply_text(f"User {username} not found or has no claims.")
        return
    
    # Update tracking info
    user_claims[found_user_id]["tracking_number"] = tracking_number
    
    # Notify user
    try:
        await context.bot.send_message(
            chat_id=found_user_id,
            text=f"📦 Your tracking number: {tracking_number}\n\n"
                 "Your order has been shipped!"
        )
    except Exception as e:
        logger.error(f"Error sending tracking info to {found_user_id}: {e}")
        await update.message.reply_text(f"Could not send message to user: {e}")
    else:
        await update.message.reply_text(f"Tracking number added for @{username}")

@private_chat_only
@admin_only
async def admin_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to see all users' claims and totals."""
    user = update.effective_user
    
    # Check if user is admin
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("This command is for Daddy only.")
        return
    
    if not user_claims:
        await update.message.reply_text("No claims data found.")
        return
    
    summary = "📊 CLAIM SUMMARY (Admin View)\n\n"
    total_revenue = 0
    paid_revenue = 0
    processed_users = set()  # Track which users we've already processed
    
    for user_id, data in user_claims.items():
        if data["items"]:
            status = data.get("payment_status", "unpaid")
            summary += f"👤 {data['full_name']} ({data['username']})\n"
            
            # List all items with clean names
            for item in data["items"]:
                # Clean up the item name (remove any price/quantity info that might be there)
                item_name = item['text']
                item_name = re.sub(r'\$.*', '', item_name).strip()
                item_name = re.sub(r'(?i)(qty|quantity).*', '', item_name).strip()
        
                # Add indicator for counter offers/offers
                item_type = ""
                if item.get('is_counter_offer'):
                    item_type = " (Counter Offer)"
                elif item.get('is_offer'):
                    item_type = " (Offer)"
                elif item.get('is_auction'):
                    item_type = " (Auction)"
                elif item.get('is_multiple_claim'):
                    item_type = f" (Multiple #{item.get('claim_number', '?')})"
        
                summary += f"   • {item_name}{item_type} - ${item['price']:.2f}\n"
            
            summary += f"   💰 Total: ${data['total']:.2f}\n"
            summary += f"   📊 Status: {'✅ Paid' if status == 'paid' else '❌ Unpaid'}\n"
            
            # Only add to revenue totals if we haven't processed this user yet
            if user_id not in processed_users:
                if status == "paid":
                    paid_revenue += data['total']
                total_revenue += data['total']
                processed_users.add(user_id)
            
            if data.get('shipping_method'):
                summary += f"   🚚 Shipping: {data['shipping_method']}\n"
            
            if data.get('tracking_number'):
                summary += f"   📦 Tracking: {data['tracking_number']}\n"
            
            summary += "\n"
    
    summary += f"💰 TOTAL REVENUE (All Users): ${total_revenue:.2f}\n"
    summary += f"💰 PAID REVENUE (Paid Users): ${paid_revenue:.2f}\n"
    summary += f"👥 TOTAL USERS WITH CLAIMS: {len(processed_users)}"
    
    # Split message if too long (Telegram has a 4096 character limit)
    if len(summary) > 4000:
        parts = [summary[i:i+4000] for i in range(0, len(summary), 4000)]
        for part in parts:
            await update.message.reply_text(part)
    else:
        await update.message.reply_text(summary)


async def check_storage_reminders(context: CallbackContext):
    """Check and send reminders for storage items expiring soon."""
    for user_id, data in user_claims.items():
        if data.get('shipping_method') == 'Storage' and data.get('storage_deadline'):
            days_left = (data['storage_deadline'] - datetime.now()).days
            
            # Send 7-day reminder
            if days_left == 7:
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"📦 Storage Reminder: Your items will expire in 7 days ({data['storage_deadline'].strftime('%d/%m/%Y')}).\n"
                             f"Please arrange for shipping or collection soon."
                    )
                except Exception as e:
                    logger.error(f"Error sending storage reminder to {user_id}: {e}")
            
            # Send 1-day reminder
            elif days_left == 1:
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"⏰ Final Reminder: Your storage expires TOMORROW ({data['storage_deadline'].strftime('%d/%m/%Y')}).\n"
                             f"Please contact my Daddy @kykaikai03 immediately to avoid item disposal."
                    )
                except Exception as e:
                    logger.error(f"Error sending final reminder to {user_id}: {e}")

async def handle_offer_acceptance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin acceptance of customer offers with 'yours'."""
    message = update.effective_message
    reply_to = message.reply_to_message
    user = update.effective_user
    
    # Allow admin acceptance from:
    # 1. Admin users (by user ID)
    is_admin_user = user and user.id in ADMIN_IDS
    
    # 2. Channel messages (check if sender chat is in admin IDs)
    is_channel_message = False
    if hasattr(message, 'sender_chat') and message.sender_chat:
        is_channel_message = message.sender_chat.id in ADMIN_IDS
    
    # 3. Also check if the message is from a channel that forwarded the message
    is_forward_from_channel = False
    if hasattr(message, 'forward_from_chat') and message.forward_from_chat:
        is_forward_from_channel = message.forward_from_chat.id in ADMIN_IDS
    
    if not (is_admin_user or is_channel_message or is_forward_from_channel):
        return
    
    # Check if there are active offers
    if not offer_posts[reply_to.message_id]["offers"]:
        await message.reply_text("No active offers found for this post.")
        return
    
    # Get the current highest offer and user
    current_offers = offer_posts[reply_to.message_id]["offers"]
    offer_user_id = max(current_offers, key=current_offers.get)
    offer_amount = current_offers[offer_user_id]

               # Handle quantity when offer is accepted
    quantity = 1
    text_to_search = reply_to.text or reply_to.caption or ""
    quantity_match = re.search(r'(?:qty|quantity)[:\s]*(\d+)', text_to_search, re.IGNORECASE)
    if quantity_match:
        quantity = int(quantity_match.group(1))
    
    # If it's a multi-quantity item, store the offer price for remaining quantities
    if quantity > 1:
        if reply_to.message_id in claimed_posts:
            # Item already has some claims - reduce available quantity
            if "quantity" in claimed_posts[reply_to.message_id]:
                claimed_posts[reply_to.message_id]["quantity"] -= 1
            else:
                # Convert from single claim to quantity format with offer price
                claimed_posts[reply_to.message_id] = {
                    "user_id": offer_user_id,
                    "quantity": quantity - 1,
                    "offer_price": offer_amount  # Store the offer price
                }
        else:
            # First claim of this multi-quantity item via offer
            claimed_posts[reply_to.message_id] = {
                "user_id": offer_user_id,
                "quantity": quantity - 1,  # Remaining quantity
                "offer_price": offer_amount  # Store the offer price
            }
    else:
        # Single item - mark as fully claimed at offer price
        claimed_posts[reply_to.message_id] = {"user_id": offer_user_id}
    
    # Get user info
    try:
        user_info = await context.bot.get_chat(offer_user_id)
        username = f"@{user_info.username}" if user_info.username else "No username"
        full_name = user_info.first_name + (f" {user_info.last_name}" if user_info.last_name else "")
    except:
        username = "Unknown"
        full_name = "Unknown"
    
    # Initialize user data if not exists
    if offer_user_id not in user_claims:
        user_claims[offer_user_id] = {
            "items": [], 
            "total": 0, 
            "username": username,
            "full_name": full_name,
            "payment_status": "unpaid",
            "shipping_method": None,
            "tracking_number": None
        }
    
    # Get the original post content and extract item name
    try:
        original_post = reply_to
        item_text = (original_post.text or original_post.caption or "Item")[:50]
    except Exception as e:
        logger.error(f"Error getting original post: {e}")
        item_text = "Item"
    
    # Add the accepted offer to user's claims
    user_data = user_claims[offer_user_id]
    user_data["items"].append({
        "text": item_text,
        "price": offer_amount,
        "message_id": reply_to.message_id,
        "timestamp": datetime.now(),
        "quantity": 1,
        "is_offer": True
    })
    user_data["total"] += offer_amount
    
    # Clear the offers for this post since it's been accepted
    offer_posts[reply_to.message_id]["offers"] = {}
    if offer_user_id in offer_posts[reply_to.message_id]["counter_offers"]:
        del offer_posts[reply_to.message_id]["counter_offers"][offer_user_id]
    
    # Notify in comments (reply to original post)
    try:
        await context.bot.send_message(
            chat_id=message.chat_id,
            reply_to_message_id=reply_to.message_id,
            text=f"✅ Offer accepted! @{user_info.username if user_info.username else 'user'} gets the item for ${offer_amount:.2f}"
        )
    except Exception as e:
        logger.error(f"Error posting acceptance in comments: {e}")
    
    # Notify the user via PM
    try:
        await context.bot.send_message(
            chat_id=offer_user_id,
            text=f"🎉 Your offer of ${offer_amount:.2f} has been accepted!\n\n"
                 f"Item has been added to your claims.\n"
                 f"Please use /invoice to see your total and /pay to confirm payment."
        )
    except Exception as e:
        logger.error(f"Error notifying user {offer_user_id}: {e}")
        await message.reply_text(f"Could not notify user: {e}")

        # Update sold quantity and price for accepted offer
    asyncio.create_task(update_sold_quantity(reply_to.message_id, 1))
    asyncio.create_task(update_post_price(reply_to.message_id, offer_amount))

    
    # Schedule a reminder for payment (in 12 hours)
    context.job_queue.run_once(
        payment_reminder, 
        timedelta(hours=12), 
        data=offer_user_id, 
        name=f"payment_reminder_{offer_user_id}"
    )

async def handle_multiple_offer_acceptance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin acceptance of multiple claim offers with waitlist support."""
    message = update.effective_message
    reply_to = message.reply_to_message
    user = update.effective_user
    
    # Allow admin acceptance from admin sources
    is_admin_user = user and user.id in ADMIN_IDS
    is_channel_message = hasattr(message, 'sender_chat') and message.sender_chat and message.sender_chat.id in ADMIN_IDS
    is_forward_from_channel = hasattr(message, 'forward_from_chat') and message.forward_from_chat and message.forward_from_chat.id in ADMIN_IDS
    
    if not (is_admin_user or is_channel_message or is_forward_from_channel):
        return
    
    # Check if this is a multiple claim post
    text_to_search = reply_to.text or reply_to.caption or ""
    first_line = text_to_search.split('\n')[0].strip() if text_to_search else ""
    
    if "multiple" not in first_line.lower() or reply_to.message_id not in multiple_claims:
        return
    
    multi_data = multiple_claims[reply_to.message_id]
    
    # Parse which claim number is being accepted - format: "yours X" or "urs X"
    acceptance_match = re.search(r'(?:yours|urs)\s*(\d+)', message.text, re.IGNORECASE)
    if not acceptance_match:
        return
    
    claim_number = int(acceptance_match.group(1))
    
    # ✅ FIX: Validate claim number is within the available range
    total_quantity = multi_data.get("total_quantity", 10)
    if claim_number < 1 or claim_number > total_quantity:
        await message.reply_text(f"❌ Claim number must be between 1 and {total_quantity} for this multi post.")
        return
    
    # Check if this claim number is already accepted/claimed
    if claim_number in multi_data.get("accepted_offers", {}) or claim_number in multi_data.get("claims", {}):
        await message.reply_text(f"❌ Claim #{claim_number} has already been accepted/claimed.")
        return
    
    # Check if there are offers for this claim number
    if claim_number not in multi_data.get("offers", {}) or not multi_data["offers"][claim_number]:
        await message.reply_text(f"No active offers found for claim #{claim_number}.")
        return
    
    # Get the highest offer for this claim number
    offers_for_number = multi_data["offers"][claim_number]
    highest_offer_user = max(offers_for_number, key=offers_for_number.get)
    highest_offer_amount = offers_for_number[highest_offer_user]
    
    # Extract item details
    lines = text_to_search.split('\n')
    item_name = ""
    for i in range(1, min(4, len(lines))):
        if lines[i].strip():
            clean_line = lines[i].strip()
            clean_line = re.sub(r'\$.*', '', clean_line).strip()
            clean_line = re.sub(r'(?i)(qty|quantity).*', '', clean_line).strip()
            if clean_line:
                item_name += clean_line + " "
    item_name = item_name.strip()
    
    if not item_name:
        item_name = "Multiple Claim Item"
    
    # Initialize user data if not exists
    if highest_offer_user not in user_claims:
        try:
            user_info = await context.bot.get_chat(highest_offer_user)
            username = f"@{user_info.username}" if user_info.username else "No username"
            full_name = user_info.first_name + (f" {user_info.last_name}" if user_info.last_name else "")
        except:
            username = "Unknown"
            full_name = "Unknown"
            
        user_claims[highest_offer_user] = {
            "items": [], 
            "total": 0, 
            "username": username,
            "full_name": full_name,
            "payment_status": "unpaid",
            "shipping_method": None,
            "tracking_number": None
        }
    
    # Add the accepted offer to user's claims
    user_data = user_claims[highest_offer_user]
    user_data["items"].append({
        "text": f"{item_name} (#{claim_number})",
        "price": highest_offer_amount,
        "message_id": reply_to.message_id,
        "timestamp": datetime.now(),
        "quantity": 1,
        "claim_number": claim_number,
        "is_multiple_claim": True,
        "is_offer": True
    })
    user_data["total"] += highest_offer_amount
    
    # Mark this claim number as accepted
    if "accepted_offers" not in multi_data:
        multi_data["accepted_offers"] = {}
    multi_data["accepted_offers"][claim_number] = highest_offer_user
    
    # Clear offers and counter offers for this claim number
    if claim_number in multi_data.get("offers", {}):
        del multi_data["offers"][claim_number]
    if claim_number in multi_data.get("counter_offers", {}):
        del multi_data["counter_offers"][claim_number]
    
    try:
        user_info = await context.bot.get_chat(highest_offer_user)
        username = f"@{user_info.username}" if user_info.username else "user"
        
        # Post acceptance in comments
        await context.bot.send_message(
            chat_id=message.chat_id,
            reply_to_message_id=reply_to.message_id,
            text=f"✅ Offer accepted! {username} gets claim #{claim_number} for ${highest_offer_amount:.2f}\n\n"
                 f"Subsequent claims for #{claim_number} will be added to waitlist."
        )
        
        # Notify the winning user
        await context.bot.send_message(
            chat_id=highest_offer_user,
            text=f"🎉 Your offer of ${highest_offer_amount:.2f} for claim #{claim_number} has been accepted!\n\n"
                 f"Item has been added to your claims.\n"
                 f"Please use /invoice to see your total and /pay to confirm payment."
        )
        
    except Exception as e:
        logger.error(f"Error processing multiple offer acceptance: {e}")

        # Update sold quantity and price for multiple accepted offer
    asyncio.create_task(update_sold_quantity(reply_to.message_id, 1))
    asyncio.create_task(update_post_price(reply_to.message_id, highest_offer_amount))
    
    # Schedule payment reminder
    context.job_queue.run_once(
        payment_reminder, 
        timedelta(hours=12), 
        data=highest_offer_user, 
        name=f"payment_reminder_{highest_offer_user}"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all messages for multiple post types."""
    message_text = update.effective_message.text or ""
    reply_to = update.effective_message.reply_to_message
    
    if not reply_to:
        return
    
    message_text_clean = message_text.strip().lower()
    
    # First, check for admin commands that work across all post types
    if reply_to:
        # Auction confirmations (admin only)
        if message_text_clean in ['yours', 'urs'] or 'rp not met' in message_text_clean:
            if reply_to.message_id in auction_posts:
                await handle_auction_confirmation(update, context)
                return
            elif reply_to.message_id in offer_posts:
                await handle_offer_acceptance(update, context)
                return
        
        # Multiple claim offer acceptances (admin only)
        if re.search(r'(?:yours|urs)\s*\d+', message_text_clean):
            await handle_multiple_offer_acceptance(update, context)
            return
        
        # Multiple claim counter offers (admin only)
        if re.search(r'co\s*\d+\s*(?:at|for)\s*(\$?\d+(?:\.\d{1,2})?)', message_text_clean):
            await handle_multiple_counter_offer(update, context)
            return
        
        # Counter offers (admin only) - REMOVED DUPLICATE
        if reply_to.message_id in offer_posts and re.search(r'^co\s*\d', message_text, re.IGNORECASE):
            await handle_counter_offer(update, context)
            return
    
    # Check if this is a multiple claim post FIRST
    text_to_search = reply_to.text or reply_to.caption or ""
    first_line = text_to_search.split('\n')[0].strip() if text_to_search else ""
    
    if "multiple" in first_line.lower():
        # This is a multiple claim post - handle claims differently
        
        # IMPROVED REGEX PATTERNS:
        if re.search(r'^claim\s+\d+', message_text, re.IGNORECASE):
            await handle_multiple_claim(update, context)
            return
        elif re.search(r'^claim$', message_text, re.IGNORECASE):
            await update.message.reply_text("❌ Please use format 'claim X' where X is a number from 1-10 for multiple claim posts.")
            return
        elif re.search(r'^unclaim\s+\d+', message_text, re.IGNORECASE):
            await handle_unclaim(update, context)  # This will route to specific unclaim
            return
        elif re.search(r'^unclaim$', message_text, re.IGNORECASE):
            await handle_unclaim(update, context)  # This will route to most recent unclaim
            return
        elif re.search(r'^offer\s+\d+\s+(?:at|for)\s+\$?\d+(?:\.\d{1,2})?', message_text, re.IGNORECASE):
            await handle_multiple_offer(update, context)  # CHANGED: route to handle_multiple_offer directly
            return
        elif re.search(r'^offer\s+\$?\d+(?:\.\d{1,2})?', message_text, re.IGNORECASE):
            await update.message.reply_text("❌ For multiple claim posts, please specify which claim number: 'offer X at Y' where X is number (1-10) and Y is price.")
            return
        elif re.search(r'^take\s+\d+', message_text, re.IGNORECASE):
            # Handle multiple claim counter offer acceptance
            await handle_take(update, context)
            return
        elif re.search(r'^take$', message_text, re.IGNORECASE):
            await update.message.reply_text("❌ For multiple claim posts, please specify which claim number: 'take X' where X is the claim number.")
            return
        else:
            # For multiple claim posts, ignore other types of messages
            logger.info(f"Ignored message for multi post: '{message_text}'")
            return
    
    # Handle user interactions based on post type for NON-multiple posts
    if reply_to.message_id in auction_posts:
        # This is an auction post
        auction_data = auction_posts[reply_to.message_id]
        
        if not auction_data.get("active", False):
            await update.message.reply_text("This auction has ended.")
            return
            
        if re.search(r'^take$', message_text, re.IGNORECASE):
            await update.message.reply_text("❌ 'take' command is for counter offers, not auctions. Use a bid amount instead.")
            return
        elif re.search(r'^claim$', message_text, re.IGNORECASE):
            await update.message.reply_text("❌ Use bid amounts for auctions, not 'claim'.")
            return
        elif re.search(r'^unclaim$', message_text, re.IGNORECASE):
            await update.message.reply_text("❌ Cannot unclaim auctions. Contact admin if there's an issue.")
            return
        elif re.search(r'^(\$?\d+(?:\.\d{1,2})?)$', message_text.strip()):
            await handle_auction_bid(update, context)
            return
        elif re.search(r'offer\s*(\$?\d+(?:\.\d{1,2})?)', message_text, re.IGNORECASE):
            await update.message.reply_text("❌ Use direct bids for auctions, not offers.")
            return
            
    elif reply_to.message_id in offer_posts:
        # This is an offer post
        if re.search(r'^take$', message_text, re.IGNORECASE):
            await handle_take(update, context)
            return
        elif re.search(r'^claim$', message_text, re.IGNORECASE):
            await handle_claim(update, context)
            return
        elif re.search(r'^unclaim$', message_text, re.IGNORECASE):
            await handle_unclaim(update, context)
            return
        elif re.search(r'offer\s*(\$?\d+(?:\.\d{1,2})?)', message_text, re.IGNORECASE):
            await handle_offer(update, context)
            return
        elif re.search(r'^(\$?\d+(?:\.\d{1,2})?)$', message_text.strip()):
            await update.message.reply_text("❌ Use 'offer X' format for offer posts, not just numbers.")
            return
            
    else:
        # This is a regular post (neither auction nor active offer)
        if re.search(r'^claim$', message_text, re.IGNORECASE):
            await handle_claim(update, context)
            return
        elif re.search(r'^unclaim$', message_text, re.IGNORECASE):
            await handle_unclaim(update, context)
            return
        elif re.search(r'offer\s*(\$?\d+(?:\.\d{1,2})?)', message_text, re.IGNORECASE):
            await handle_offer(update, context)
            return
        elif re.search(r'^take$', message_text, re.IGNORECASE):
            # Check if user has any counter offers across all posts
            user = update.effective_user
            for post_id, post_data in offer_posts.items():
                if user.id in post_data.get("counter_offers", {}):
                    await handle_take(update, context)
                    return
            # Also check multiple claims for counter offers
            for post_id, multi_data in multiple_claims.items():
                for claim_num, counter_offers in multi_data.get("counter_offers", {}).items():
                    if user.id in counter_offers:
                        await update.message.reply_text("❌ For multiple claim counter offers, please use 'take X' where X is the claim number.")
                        return
            await update.message.reply_text("No counter offer found for you.")
            return


@private_chat_only
@admin_only
async def debug_posts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Debug command to see all active posts (admin only)."""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return
    
    debug_info = "📊 ACTIVE POSTS DEBUG INFO\n\n"
    
    # Auction posts
    debug_info += f"🏆 AUCTION POSTS: {len(auction_posts)}\n"
    for msg_id, auction_data in auction_posts.items():
        active = auction_data.get('active', False)
        bids = len(auction_data.get('bids', {}))
        debug_info += f"  - {msg_id}: active={active}, bids={bids}\n"
    
    # Offer posts  
    debug_info += f"\n💵 OFFER POSTS: {len(offer_posts)}\n"
    for msg_id, offer_data in offer_posts.items():
        offers = len(offer_data.get('offers', {}))
        counter_offers = len(offer_data.get('counter_offers', {}))
        debug_info += f"  - {msg_id}: offers={offers}, counter_offers={counter_offers}\n"
    
    # Claimed posts
    debug_info += f"\n✅ CLAIMED POSTS: {len(claimed_posts)}\n"
    for msg_id, claim_data in claimed_posts.items():
        if isinstance(claim_data, dict) and 'quantity' in claim_data:
            debug_info += f"  - {msg_id}: quantity={claim_data['quantity']}\n"
        else:
            debug_info += f"  - {msg_id}: claimed\n"
    
    await update.message.reply_text(debug_info)

def detect_post_type(message_text):
    """Enhanced post type detection with better categorization."""
    if not message_text:
        return "unknown"
    
    text = message_text.lower()
    
    # Check for auction first (most specific)
    if "auction" in text and any(keyword in text for keyword in ['end', 'ends', 'bid']):
        return "auction"
    
    # Check for multiple claims
    elif "multiple" in text or "multi" in text:
        return "multi" 
    
    # ✅ FIXED: More inclusive claim detection
    # Check for ANY price indication - dollar sign OR numbers that look like prices
    elif (re.search(r'\$\d+(?:\.\d{1,2})?', text) or  # $10 or $10.50
          re.search(r'(?:price|cost|each)[:\s]*\d+(?:\.\d{1,2})?', text) or  # price: 10
          re.search(r'\b\d+(?:\.\d{1,2})?\s*(?:dollars?|sgd|usd)\b', text) or  # 10 dollars
          re.search(r'^\d+(?:\.\d{1,2})?$', text.strip())):  # Just "10" as first line
        return "claim"
    
    else:
        return "other"

async def handle_new_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle ALL new posts - both auction detection and Google Sheets recording."""
    message = update.effective_message
    text = message.text or message.caption or ''
    
    # Skip if this is a reply (not an original post)
    if message.reply_to_message:
        logger.info(f"Skipping reply message {message.message_id}")
        return
    
    # Skip empty messages
    if not text.strip():
        logger.info(f"Skipping empty message {message.message_id}")
        return
    
    # Step 1: Detect post type
    post_type = detect_post_type(text)
    logger.info(f"Detected post type for message {message.message_id}: {post_type}")
    
    # ✅ FIX: Only record specific post types in Google Sheets
    if post_type in ["claim", "multi", "auction"]:
        # Step 2: Record in Google Sheets (only for claim/multi/auction posts)
        try:
            if message.message_id not in sheets_tracking:
                await record_post_immediately(message.message_id, text, post_type)
                logger.info(f"✅ Recorded {post_type} post {message.message_id} in Google Sheets")
            else:
                logger.info(f"⚠️ Post {message.message_id} already in sheets_tracking")
        except Exception as e:
            logger.error(f"❌ Error recording {post_type} post {message.message_id} in sheets: {e}")
    else:
        logger.info(f"⏭️ Skipping Google Sheets recording for {post_type} post {message.message_id}")
    
    # Step 3: Initialize appropriate data structures based on post type
    try:
        if post_type == "auction":
            await setup_auction_post(message, context, text)
        elif post_type == "multi":
            await setup_multi_post(message, text)
        elif post_type == "claim":
            await setup_regular_post(message, text)
        else:
            logger.info(f"⏭️ No special setup needed for {post_type} post {message.message_id}")
            
        logger.info(f"✅ Successfully processed {post_type} post {message.message_id}")
        
    except Exception as e:
        logger.error(f"❌ Error processing {post_type} post {message.message_id}: {e}")


def main():
    """Start the bot."""
    # Create the Application
    application = Application.builder().token(TOKEN).build()
    
    # Add debug logging for admin IDs
    logger.info(f"Admin IDs: {ADMIN_IDS}")
    
    # Add storage reminder job
    application.job_queue.run_repeating(
        check_storage_reminders,
        interval=timedelta(hours=24),  # Check daily
        first=10  # Start after 10 seconds
    )
    
    # Conversation handler
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('pay', pay_command),
            CommandHandler('shipping', shipping_command),
            CommandHandler('undo', undo_command)
        ],
        states={
            SHIPPING_METHOD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, shipping_method)
            ],
            UNDO_SHIPPING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, undo_shipping)
            ],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    # Add ConversationHandler FIRST
    application.add_handler(conv_handler)

    # THEN add individual command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("list", list_claims))
    application.add_handler(CommandHandler("invoice", invoice))
    application.add_handler(CommandHandler("tracking", tracking_command))
    application.add_handler(CommandHandler("summary", admin_summary))
    application.add_handler(CommandHandler("reminder", reminder_command))
    application.add_handler(CommandHandler("auction_status", auction_status))
    application.add_handler(CommandHandler("debug_posts", debug_posts))
    application.add_handler(CommandHandler("debug_multiple", debug_multiple))
    application.add_handler(CommandHandler("setup_sheet", setup_sheet_command))
    application.add_handler(CommandHandler("recover_post", recover_post))
    application.add_handler(CommandHandler("debug_claim", debug_claim))
    application.add_handler(CommandHandler("test_posts", test_post_detection))
    application.add_handler(CommandHandler("record_manual", record_manual))
    application.add_handler(CommandHandler("debug_sheets", debug_sheets))
    application.add_handler(CommandHandler("reset_all_claims", reset_all_claims))
    application.add_handler(CommandHandler("reset_user", reset_specific_user))
    application.add_handler(CommandHandler("reset_status", reset_status))
    application.add_handler(CommandHandler("find_user", find_user))
    application.add_handler(CommandHandler("test_ai", test_ai_response))
    
    # FIXED HANDLER ORDER - SIMPLIFIED APPROACH
    
    # 1. Single handler for ALL post categorization (including auctions)
    application.add_handler(MessageHandler(
        (filters.TEXT | filters.CAPTION) & ~filters.COMMAND & ~filters.REPLY,
        handle_new_post
    ))
    
    # 2. Handler for user interactions (replies)
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.REPLY,
        handle_message
    ))

    # 3. AI conversation handler - CATCHES ALL non-command, non-reply messages in PRIVATE chats
    application.add_handler(MessageHandler(
        filters.TEXT & 
        ~filters.COMMAND & 
        ~filters.REPLY &
        filters.ChatType.PRIVATE,  # ONLY in private chats
        handle_ai_conversation
    ), group=1)  # Higher priority group

    
    # Start the Bot
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main() 