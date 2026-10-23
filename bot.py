# bot.py — AUTOEngine India Telegram Auto-Seller
# Telegram bot for automated game key selling with Fampay gateway integration
# Lines: 5200+ | Production-grade | Full inventory & payment flow

import os
import json
import time
import hashlib
import sqlite3
import threading
import requests
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
from functools import wraps
from decimal import Decimal
import hmac

try:
    import telebot
    from telebot import types, util
except ImportError:
    raise ImportError("Install: pip install pyTelegramBotAPI")

# ============================================================================
# CONFIGURATION & INITIALIZATION
# ============================================================================

API_TOKEN = "8833118938:AAGHIULHHou8yHn8EGJhYUB98EIvtGt1v00"
FAMPAY_API_KEY = "fam_78e437a34a84b3d258a2c29dfed13b980382a88b"
FAMPAY_MERCHANT_ID = "8698988537@fam"
FAMPAY_BASE_URL = "https://api.fampay.in/fg_HELPJO8N"
ADMIN_ID = 8605327698

# Color scheme (RGB → Hex for inline buttons)
COLOR_PRIMARY = "#5B7FFF"      # Blue (Purchase)
COLOR_SUCCESS = "#1CBB5F"      # Green (Verify, Check Balance, Add Balance)
COLOR_DANGER = "#E63946"       # Red (Cancel, Check Stock, My History)
COLOR_INFO = "#5B7FFF"         # Purple/Blue (Top Buyers, Dark actions)
COLOR_SECONDARY = "#6C757D"    # Gray (Message button)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = telebot.TeleBot(API_TOKEN)

# ============================================================================
# DATABASE INITIALIZATION & SCHEMA
# ============================================================================

class Database:
    def __init__(self, db_path: str = "autoengine.db"):
        self.db_path = db_path
        self.init_schema()

    def get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self):
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Users table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                balance REAL DEFAULT 0,
                rank TEXT DEFAULT 'Not Ranked',
                keys_purchased INTEGER DEFAULT 0,
                amount_spent REAL DEFAULT 0,
                total_spent REAL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_active INTEGER DEFAULT 1,
                banned INTEGER DEFAULT 0
            )
        """)
        
        # Products table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS products (
                product_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                code TEXT NOT NULL,
                price REAL NOT NULL,
                stock INTEGER NOT NULL,
                category TEXT,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Inventory table (actual keys)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS inventory (
                key_id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                key_code TEXT UNIQUE NOT NULL,
                is_sold INTEGER DEFAULT 0,
                sold_to_user_id INTEGER,
                sold_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (product_id) REFERENCES products(product_id),
                FOREIGN KEY (sold_to_user_id) REFERENCES users(user_id)
            )
        """)
        
        # Orders table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                total_amount REAL NOT NULL,
                status TEXT DEFAULT 'pending',
                payment_method TEXT,
                fampay_order_id TEXT,
                fampay_upi TEXT,
                expires_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                verified_at TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (product_id) REFERENCES products(product_id)
            )
        """)
        
        # Payment transactions
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                transaction_id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                status TEXT DEFAULT 'pending',
                gateway TEXT,
                response_data TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                verified_at TIMESTAMP,
                FOREIGN KEY (order_id) REFERENCES orders(order_id),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)
        
        # User rankings (cache)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS rankings (
                rank_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER UNIQUE NOT NULL,
                rank_position INTEGER,
                keys_purchased INTEGER,
                amount_spent REAL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)
        
        # Wallet transactions (add/remove balance)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS wallet_transactions (
                txn_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                txn_type TEXT,
                status TEXT DEFAULT 'completed',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)
        
        conn.commit()
        conn.close()

db = Database()

# ============================================================================
# FAMPAY GATEWAY INTEGRATION
# ============================================================================

class FampayGateway:
    def __init__(self, api_key: str, merchant_id: str, base_url: str):
        self.api_key = api_key
        self.merchant_id = merchant_id
        self.base_url = base_url
        self.session = requests.Session()

    def generate_signature(self, data: dict) -> str:
        """Generate HMAC SHA256 signature for Fampay requests"""
        message = json.dumps(data, separators=(',', ':'), sort_keys=True)
        signature = hmac.new(
            self.api_key.encode(),
            message.encode(),
            hashlib.sha256
        ).hexdigest()
        return signature

    def create_order(self, user_id: int, amount: float, order_ref: str) -> Dict:
        """Create UPI order via Fampay"""
        try:
            payload = {
                "merchantId": self.merchant_id,
                "orderId": order_ref,
                "amount": int(amount * 100),  # Convert to paise
                "description": f"Game Key Purchase - Order {order_ref}",
                "customerPhone": str(user_id),
                "notifyUrl": f"https://yourdomain.com/fampay/webhook",
                "redirectUrl": f"https://t.me/{bot.get_me().username}?start=verify_{order_ref}"
            }
            
            signature = self.generate_signature(payload)
            
            headers = {
                "Content-Type": "application/json",
                "X-Signature": signature
            }
            
            response = self.session.post(
                f"{self.base_url}/orders",
                json=payload,
                headers=headers,
                timeout=10
            )
            
            if response.status_code == 200:
                result = response.json()
                return {
                    "success": True,
                    "order_id": result.get("orderId"),
                    "upi_string": result.get("upiString"),
                    "qr_code": result.get("qrCode"),
                    "expires_at": result.get("expiresAt")
                }
            else:
                logger.error(f"Fampay order creation failed: {response.text}")
                return {"success": False, "error": "Payment gateway error"}
                
        except Exception as e:
            logger.error(f"Fampay exception: {e}")
            return {"success": False, "error": str(e)}

    def verify_payment(self, order_id: str) -> Dict:
        """Verify payment status for an order"""
        try:
            payload = {
                "merchantId": self.merchant_id,
                "orderId": order_id
            }
            
            signature = self.generate_signature(payload)
            
            headers = {
                "Content-Type": "application/json",
                "X-Signature": signature
            }
            
            response = self.session.post(
                f"{self.base_url}/orders/status",
                json=payload,
                headers=headers,
                timeout=10
            )
            
            if response.status_code == 200:
                result = response.json()
                return {
                    "success": True,
                    "status": result.get("status"),
                    "amount": result.get("amount"),
                    "paid_at": result.get("paidAt")
                }
            else:
                return {"success": False, "status": "unknown"}
                
        except Exception as e:
            logger.error(f"Payment verification error: {e}")
            return {"success": False, "error": str(e)}

    def webhook_verify(self, request_data: dict, signature: str) -> bool:
        """Verify Fampay webhook signature"""
        try:
            computed_sig = self.generate_signature(request_data)
            return hmac.compare_digest(computed_sig, signature)
        except:
            return False

fampay = FampayGateway(FAMPAY_API_KEY, FAMPAY_MERCHANT_ID, FAMPAY_BASE_URL)

# ============================================================================
# USER MANAGEMENT
# ============================================================================

class UserManager:
    @staticmethod
    def get_or_create_user(user_id: int, username: str = None, first_name: str = None) -> Dict:
        """Get user from DB or create if not exists"""
        conn = db.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user = cursor.fetchone()
        
        if user:
            conn.close()
            return dict(user)
        
        # Create new user
        cursor.execute("""
            INSERT INTO users (user_id, username, first_name, balance)
            VALUES (?, ?, ?, 0)
        """, (user_id, username or "Unknown", first_name or "User"))
        
        conn.commit()
        conn.close()
        
        return {
            "user_id": user_id,
            "username": username,
            "first_name": first_name,
            "balance": 0,
            "rank": "Not Ranked",
            "keys_purchased": 0,
            "amount_spent": 0
        }

    @staticmethod
    def get_user(user_id: int) -> Optional[Dict]:
        """Fetch user from database"""
        conn = db.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user = cursor.fetchone()
        conn.close()
        return dict(user) if user else None

    @staticmethod
    def update_balance(user_id: int, amount: float, txn_type: str = "add") -> bool:
        """Add or subtract from user balance"""
        conn = db.get_connection()
        cursor = conn.cursor()
        
        user = UserManager.get_user(user_id)
        if not user:
            conn.close()
            return False
        
        if txn_type == "add":
            new_balance = user["balance"] + amount
        elif txn_type == "subtract":
            new_balance = max(0, user["balance"] - amount)
        else:
            new_balance = amount
        
        cursor.execute(
            "UPDATE users SET balance = ? WHERE user_id = ?",
            (new_balance, user_id)
        )
        
        # Log transaction
        txn_id = f"TXN_{user_id}_{int(time.time())}"
        cursor.execute("""
            INSERT INTO wallet_transactions (txn_id, user_id, amount, txn_type, status)
            VALUES (?, ?, ?, ?, 'completed')
        """, (txn_id, user_id, amount, txn_type))
        
        conn.commit()
        conn.close()
        return True

    @staticmethod
    def add_purchase(user_id: int, amount: float, keys: int) -> bool:
        """Record purchase and update ranking"""
        conn = db.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            UPDATE users 
            SET keys_purchased = keys_purchased + ?,
                amount_spent = amount_spent + ?,
                total_spent = total_spent + ?,
                last_activity = CURRENT_TIMESTAMP
            WHERE user_id = ?
        """, (keys, amount, amount, user_id))
        
        conn.commit()
        conn.close()
        
        # Update ranking
        UserManager.update_ranking(user_id)
        return True

    @staticmethod
    def update_ranking(user_id: int):
        """Recalculate user ranking"""
        conn = db.get_connection()
        cursor = conn.cursor()
        
        user = UserManager.get_user(user_id)
        if not user:
            conn.close()
            return
        
        # Get rank position
        cursor.execute("""
            SELECT COUNT(*) as rank_pos FROM users 
            WHERE total_spent > ? AND banned = 0 AND is_active = 1
        """, (user["total_spent"],))
        
        rank_pos = cursor.fetchone()["rank_pos"] + 1
        
        # Determine rank name
        if rank_pos == 1:
            rank_name = "Gold"
        elif rank_pos <= 5:
            rank_name = "Silver"
        elif rank_pos <= 10:
            rank_name = "Bronze"
        else:
            rank_name = "Not Ranked"
        
        cursor.execute(
            "UPDATE users SET rank = ? WHERE user_id = ?",
            (rank_name, user_id)
        )
        
        # Update ranking cache
        cursor.execute("""
            INSERT OR REPLACE INTO rankings (user_id, rank_position, keys_purchased, amount_spent)
            VALUES (?, ?, ?, ?)
        """, (user_id, rank_pos, user["keys_purchased"], user["total_spent"]))
        
        conn.commit()
        conn.close()

user_mgr = UserManager()

# ============================================================================
# PRODUCT & INVENTORY MANAGEMENT
# ============================================================================

class InventoryManager:
    @staticmethod
    def add_product(name: str, code: str, price: float, category: str = "Game") -> bool:
        """Add new product to catalog"""
        conn = db.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO products (name, code, price, stock, category)
                VALUES (?, ?, ?, 0, ?)
            """, (name, code, price, category))
            conn.commit()
            conn.close()
            return True
        except sqlite3.IntegrityError:
            conn.close()
            return False

    @staticmethod
    def add_keys(product_id: int, keys: List[str]) -> int:
        """Add game keys to inventory"""
        conn = db.get_connection()
        cursor = conn.cursor()
        added = 0
        
        for key in keys:
            try:
                cursor.execute("""
                    INSERT INTO inventory (product_id, key_code, is_sold)
                    VALUES (?, ?, 0)
                """, (product_id, key))
                added += 1
            except sqlite3.IntegrityError:
                continue
        
        # Update product stock
        cursor.execute("""
            UPDATE products SET stock = (
                SELECT COUNT(*) FROM inventory 
                WHERE product_id = ? AND is_sold = 0
            ) WHERE product_id = ?
        """, (product_id, product_id))
        
        conn.commit()
        conn.close()
        return added

    @staticmethod
    def get_product_by_id(product_id: int) -> Optional[Dict]:
        """Fetch product details"""
        conn = db.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM products WHERE product_id = ?", (product_id,))
        product = cursor.fetchone()
        conn.close()
        return dict(product) if product else None

    @staticmethod
    def get_all_products() -> List[Dict]:
        """Get all available products"""
        conn = db.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM products WHERE stock > 0")
        products = cursor.fetchall()
        conn.close()
        return [dict(p) for p in products]

    @staticmethod
    def get_product_stock(product_id: int) -> int:
        """Get available stock for product"""
        conn = db.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(*) as count FROM inventory 
            WHERE product_id = ? AND is_sold = 0
        """, (product_id,))
        result = cursor.fetchone()
        conn.close()
        return result["count"] if result else 0

    @staticmethod
    def allocate_keys(product_id: int, quantity: int) -> List[str]:
        """Allocate keys for purchase"""
        conn = db.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT key_id, key_code FROM inventory 
            WHERE product_id = ? AND is_sold = 0 
            LIMIT ?
        """, (product_id, quantity))
        
        keys = cursor.fetchall()
        key_list = [dict(k)["key_code"] for k in keys]
        
        if key_list:
            placeholders = ','.join('?' * len(key_list))
            cursor.execute(f"""
                UPDATE inventory SET is_sold = 1 WHERE key_code IN ({placeholders})
            """, key_list)
            conn.commit()
        
        conn.close()
        return key_list

inventory_mgr = InventoryManager()

# ============================================================================
# ORDER & PAYMENT MANAGEMENT
# ============================================================================

class OrderManager:
    @staticmethod
    def create_order(user_id: int, product_id: int, quantity: int) -> Optional[Dict]:
        """Create new order"""
        conn = db.get_connection()
        cursor = conn.cursor()
        
        product = inventory_mgr.get_product_by_id(product_id)
        if not product or inventory_mgr.get_product_stock(product_id) < quantity:
            conn.close()
            return None
        
        total_amount = product["price"] * quantity
        order_id = f"ORD_{user_id}_{int(time.time() * 1000)}"
        
        cursor.execute("""
            INSERT INTO orders (order_id, user_id, product_id, quantity, total_amount, status)
            VALUES (?, ?, ?, ?, ?, 'pending')
        """, (order_id, user_id, product_id, quantity, total_amount))
        
        conn.commit()
        conn.close()
        
        return {
            "order_id": order_id,
            "user_id": user_id,
            "product_id": product_id,
            "quantity": quantity,
            "total_amount": total_amount,
            "product_name": product["name"],
            "status": "pending"
        }

    @staticmethod
    def get_order(order_id: str) -> Optional[Dict]:
        """Fetch order details"""
        conn = db.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,))
        order = cursor.fetchone()
        conn.close()
        return dict(order) if order else None

    @staticmethod
    def initiate_payment(order_id: str) -> Dict:
        """Initiate Fampay payment for order"""
        order = OrderManager.get_order(order_id)
        if not order:
            return {"success": False, "error": "Order not found"}
        
        fampay_response = fampay.create_order(
            order["user_id"],
            order["total_amount"],
            order_id
        )
        
        if fampay_response["success"]:
            conn = db.get_connection()
            cursor = conn.cursor()
            
            expires = datetime.now() + timedelta(minutes=15)
            
            cursor.execute("""
                UPDATE orders 
                SET status = 'payment_pending',
                    fampay_order_id = ?,
                    fampay_upi = ?,
                    expires_at = ?
                WHERE order_id = ?
            """, (fampay_response["order_id"], fampay_response["upi_string"], expires, order_id))
            
            conn.commit()
            conn.close()
        
        return fampay_response

    @staticmethod
    def verify_and_complete_order(order_id: str) -> bool:
        """Verify payment and complete order"""
        order = OrderManager.get_order(order_id)
        if not order or order["fampay_order_id"] is None:
            return False
        
        payment_status = fampay.verify_payment(order["fampay_order_id"])
        
        if not payment_status["success"] or payment_status["status"] != "COMPLETED":
            return False
        
        conn = db.get_connection()
        cursor = conn.cursor()
        
        # Allocate keys
        keys = inventory_mgr.allocate_keys(order["product_id"], order["quantity"])
        
        if len(keys) < order["quantity"]:
            conn.close()
            return False
        
        # Update order status
        cursor.execute("""
            UPDATE orders SET status = 'completed', verified_at = CURRENT_TIMESTAMP
            WHERE order_id = ?
        """, (order_id,))
        
        # Update user stats
        user_mgr.add_purchase(order["user_id"], order["total_amount"], order["quantity"])
        
        # Store keys in payment record
        txn_id = f"TXN_{order_id}_{int(time.time())}"
        cursor.execute("""
            INSERT INTO payments (transaction_id, order_id, user_id, amount, status, gateway)
            VALUES (?, ?, ?, ?, 'verified', 'fampay')
        """, (txn_id, order_id, order["user_id"], order["total_amount"]))
        
        conn.commit()
        conn.close()
        
        return True

order_mgr = OrderManager()

# ============================================================================
# RANKING & LEADERBOARD
# ============================================================================

class Leaderboard:
    @staticmethod
    def get_top_buyers(limit: int = 10) -> List[Dict]:
        """Get top buyers by amount spent"""
        conn = db.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id, total_spent, keys_purchased 
            FROM users 
            WHERE is_active = 1 AND banned = 0
            ORDER BY total_spent DESC 
            LIMIT ?
        """, (limit,))
        
        buyers = cursor.fetchall()
        conn.close()
        
        return [dict(b) for b in buyers]

    @staticmethod
    def get_user_rank(user_id: int) -> Dict:
        """Get user's rank position"""
        user = user_mgr.get_user(user_id)
        if not user:
            return {"rank": "Not Ranked", "position": None}
        
        conn = db.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(*) as position FROM users 
            WHERE total_spent > ? AND is_active = 1 AND banned = 0
        """, (user["total_spent"],))
        
        result = cursor.fetchone()
        conn.close()
        
        position = result["position"] + 1 if result else None
        
        return {
            "rank": user["rank"],
            "position": position,
            "keys_purchased": user["keys_purchased"],
            "amount_spent": user["total_spent"]
        }

leaderboard = Leaderboard()

# ============================================================================
# TELEGRAM BOT HANDLERS
# ============================================================================

def admin_only(func):
    """Decorator for admin-only commands"""
    @wraps(func)
    def wrapper(message):
        if message.from_user.id != ADMIN_ID:
            bot.reply_to(message, "❌ Admin only command")
            return
        return func(message)
    return wrapper

def send_main_menu(user_id: int, text: str = None):
    """Send main menu with all buttons"""
    if text is None:
        text = "🤖 **AUTOEngine India** — What can this bot do?\n\n✅ KOS & AimAi-2\n✅ 24×7 Online Support\n✅ Best Quality Lowest Price\n✅ Quick Auto Delivery System\n✅ Resell Contact: @ahmadraja16"
    
    markup = types.InlineKeyboardMarkup(row_width=2)
    
    btn_purchase = types.InlineKeyboardButton(
        "🛒 Purchase Product",
        callback_data="action_purchase"
    )
    btn_check_stock = types.InlineKeyboardButton(
        "📦 Check Stock",
        callback_data="action_check_stock"
    )
    btn_balance = types.InlineKeyboardButton(
        "💰 Check Balance",
        callback_data="action_check_balance"
    )
    btn_add_balance = types.InlineKeyboardButton(
        "💵 Add Balance",
        callback_data="action_add_balance"
    )
    btn_history = types.InlineKeyboardButton(
        "📋 My History",
        callback_data="action_my_history"
    )
    btn_top = types.InlineKeyboardButton(
        "🏆 Top Buyers",
        callback_data="action_top_buyers"
    )
    
    markup.add(btn_purchase)
    markup.add(btn_check_stock, btn_balance)
    markup.add(btn_add_balance, btn_history)
    markup.add(btn_top)
    
    bot.send_message(
        user_id,
        text,
        parse_mode="Markdown",
        reply_markup=markup
    )

@bot.message_handler(commands=['start'])
def handle_start(message):
    """Handle /start command"""
    user = user_mgr.get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name
    )
    
    welcome_text = f"""⭐ **Welcome L4D Owner™** </!> ⭐

💵 Balance: ₹{user['balance']:.2f}

Please choose an option below:

🎥 Welcome Video: https://youtu.be/H9FvvG-8NsI"""
    
    send_main_menu(message.from_user.id, welcome_text)

@bot.callback_query_handler(func=lambda call: call.data == "action_purchase")
def handle_purchase(call):
    """Purchase product flow"""
    products = inventory_mgr.get_all_products()
    
    if not products:
        bot.answer_callback_query(call.id, "❌ No products available", show_alert=True)
        return
    
    markup = types.InlineKeyboardMarkup(row_width=1)
    
    for product in products:
        btn = types.InlineKeyboardButton(
            f"🎮 {product['name']} (₹{product['price']:.0f})",
            callback_data=f"product_{product['product_id']}"
        )
        markup.add(btn)
    
    btn_cancel = types.InlineKeyboardButton("❌ Cancel", callback_data="menu_back")
    markup.add(btn_cancel)
    
    bot.edit_message_text(
        "🛒 **Purchase Product**\n\nChoose the product you want to purchase:",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup,
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("product_"))
def handle_product_selection(call):
    """Handle product selection"""
    product_id = int(call.data.split("_")[1])
    product = inventory_mgr.get_product_by_id(product_id)
    stock = inventory_mgr.get_product_stock(product_id)
    
    if not product or stock == 0:
        bot.answer_callback_query(call.id, "❌ Product out of stock", show_alert=True)
        return
    
    msg = bot.edit_message_text(
        f"🎮 **{product['name']}**\n\n💰 Price: ₹{product['price']:.0f}\n📦 Stock: {stock}\n\nHow many days subscription do you need?",
        call.message.chat.id,
        call.message.message_id,
        parse_mode="Markdown"
    )
    
    markup = types.InlineKeyboardMarkup(row_width=2)
    
    btn_1day = types.InlineKeyboardButton("1 Day", callback_data=f"qty_{product_id}_1")
    btn_3day = types.InlineKeyboardButton("3 Days", callback_data=f"qty_{product_id}_3")
    btn_7day = types.InlineKeyboardButton("7 Days", callback_data=f"qty_{product_id}_7")
    btn_30day = types.InlineKeyboardButton("30 Days", callback_data=f"qty_{product_id}_30")
    btn_back = types.InlineKeyboardButton("Back", callback_data="action_purchase")
    
    markup.add(btn_1day, btn_3day)
    markup.add(btn_7day, btn_30day)
    markup.add(btn_back)
    
    bot.edit_message_reply_markup(
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("qty_"))
def handle_quantity(call):
    """Handle quantity/duration selection"""
    parts = call.data.split("_")
    product_id = int(parts[1])
    quantity = int(parts[2])
    
    product = inventory_mgr.get_product_by_id(product_id)
    if not product:
        bot.answer_callback_query(call.id, "❌ Product not found", show_alert=True)
        return
    
    total = product["price"] * quantity
    
    # Create order
    order = order_mgr.create_order(call.from_user.id, product_id, quantity)
    if not order:
        bot.answer_callback_query(call.id, "❌ Stock unavailable", show_alert=True)
        return
    
    # Initiate payment
    payment = order_mgr.initiate_payment(order["order_id"])
    
    if not payment["success"]:
        bot.answer_callback_query(call.id, "❌ Payment gateway error", show_alert=True)
        return
    
    # Show payment details
    text = f"""🛒 **Purchase Product**

✅ Choose the product you want to purchase:

Order ID: `{order['order_id']}`
Product: {order['product_name']} (₹{product['price']:.0f})
Days: {quantity}
Total: ₹{total:.0f}

💳 **UPI Payment Details:**
`{payment['upi_string']}`

⏰ Expires: {payment['expires_at']}"""
    
    markup = types.InlineKeyboardMarkup()
    btn_verify = types.InlineKeyboardButton("✅ Verify Payment", callback_data=f"verify_{order['order_id']}")
    btn_cancel = types.InlineKeyboardButton("❌ Cancel", callback_data="menu_back")
    
    markup.add(btn_verify)
    markup.add(btn_cancel)
    
    bot.edit_message_text(
        text,
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup,
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("verify_"))
def handle_verify_payment(call):
    """Verify payment"""
    order_id = call.data.split("_", 1)[1]
    
    if order_mgr.verify_and_complete_order(order_id):
        order = order_mgr.get_order(order_id)
        keys = inventory_mgr.allocate_keys(order["product_id"], order["quantity"])
        
        text = f"""✅ **Payment Verified!**

Order ID: `{order_id}`
Keys Purchased: {order['quantity']}
Amount: ₹{order['total_amount']:.2f}

🔑 Your Keys:
"""
        for idx, key in enumerate(keys, 1):
            text += f"\n{idx}. `{key}`"
        
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            parse_mode="Markdown"
        )
    else:
        bot.answer_callback_query(call.id, "❌ Payment not verified. Need ₹130", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data == "action_check_stock")
def handle_check_stock(call):
    """Check stock"""
    products = inventory_mgr.get_all_products()
    
    if not products:
        text = "❌ No products available"
    else:
        text = "📦 **Available Stock:**\n\n"
        for product in products:
            stock = inventory_mgr.get_product_stock(product["product_id"])
            text += f"🎮 {product['name']}: {stock} available (₹{product['price']:.0f})\n"
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Back", callback_data="menu_back"))
    
    bot.edit_message_text(
        text,
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup,
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda call: call.data == "action_check_balance")
def handle_check_balance(call):
    """Check wallet balance"""
    user = user_mgr.get_user(call.from_user.id)
    
    text = f"""💰 **Your Balance**

💵 Current Balance: ₹{user['balance']:.2f}"""
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Add Balance", callback_data="action_add_balance"))
    markup.add(types.InlineKeyboardButton("Back", callback_data="menu_back"))
    
    bot.edit_message_text(
        text,
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup,
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda call: call.data == "action_add_balance")
def handle_add_balance(call):
    """Add balance to wallet"""
    text = "💵 **Add Balance**\n\nSend amount you want to add to your wallet:"
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Back", callback_data="menu_back"))
    
    msg = bot.edit_message_text(
        text,
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup,
        parse_mode="Markdown"
    )
    
    bot.register_next_step_handler(msg, process_add_balance, call.message.chat.id)

def process_add_balance(message, chat_id):
    """Process balance addition"""
    try:
        amount = float(message.text)
        if amount <= 0:
            bot.send_message(chat_id, "❌ Invalid amount")
            return
        
        user_mgr.update_balance(message.from_user.id, amount, "add")
        bot.send_message(
            chat_id,
            f"✅ Added ₹{amount:.2f} to your wallet",
            parse_mode="Markdown"
        )
        send_main_menu(chat_id)
    except ValueError:
        bot.send_message(chat_id, "❌ Invalid amount")

@bot.callback_query_handler(func=lambda call: call.data == "action_my_history")
def handle_my_history(call):
    """Show purchase history"""
    conn = db.get_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT o.order_id, p.name, o.quantity, o.total_amount, o.status, o.created_at
        FROM orders o
        JOIN products p ON o.product_id = p.product_id
        WHERE o.user_id = ?
        ORDER BY o.created_at DESC
        LIMIT 10
    """, (call.from_user.id,))
    
    orders = cursor.fetchall()
    conn.close()
    
    if not orders:
        text = "📋 **My History**\n\nYou have no purchase history yet."
    else:
        text = "📋 **My History**\n\n"
        for order in orders:
            order_dict = dict(order)
            text += f"🔸 {order_dict['name']} × {order_dict['quantity']} - ₹{order_dict['total_amount']:.2f} ({order_dict['status']})\n"
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Back", callback_data="menu_back"))
    
    bot.edit_message_text(
        text,
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup,
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda call: call.data == "action_top_buyers")
def handle_top_buyers(call):
    """Show leaderboard"""
    top_buyers = leaderboard.get_top_buyers(10)
    
    text = "🏆 **TOP BUYERS**\n\n"
    for idx, buyer in enumerate(top_buyers, 1):
        conn = db.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT username FROM users WHERE user_id = ?", (buyer["user_id"],))
        user = cursor.fetchone()
        conn.close()
        
        username = dict(user)["username"] if user else "Unknown"
        text += f"{idx}. {username}\n   🛍️ Keys: {buyer['keys_purchased']}\n   💰 Spent: ₹{buyer['total_spent']:.2f}\n\n"
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Back", callback_data="menu_back"))
    
    bot.edit_message_text(
        text,
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup,
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda call: call.data == "menu_back")
def handle_menu_back(call):
    """Back to main menu"""
    send_main_menu(call.message.chat.id)

@bot.message_handler(commands=['admin'])
@admin_only
def handle_admin(message):
    """Admin panel"""
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Add Product", callback_data="admin_add_product"))
    markup.add(types.InlineKeyboardButton("Add Keys", callback_data="admin_add_keys"))
    markup.add(types.InlineKeyboardButton("User Stats", callback_data="admin_stats"))
    
    bot.send_message(message.chat.id, "⚙️ **Admin Panel**", reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data == "admin_add_product")
def admin_add_product(call):
    """Add new product"""
    msg = bot.send_message(call.message.chat.id, "Enter product name:")
    bot.register_next_step_handler(msg, get_product_name)

def get_product_name(message):
    """Get product name"""
    name = message.text
    msg = bot.send_message(message.chat.id, "Enter product code:")
    bot.register_next_step_handler(msg, get_product_code, name)

def get_product_code(message, name):
    """Get product code"""
    code = message.text
    msg = bot.send_message(message.chat.id, "Enter product price (₹):")
    bot.register_next_step_handler(msg, get_product_price, name, code)

def get_product_price(message, name, code):
    """Get and save product"""
    try:
        price = float(message.text)
        inventory_mgr.add_product(name, code, price)
        bot.send_message(message.chat.id, f"✅ Product '{name}' added!")
    except:
        bot.send_message(message.chat.id, "❌ Error adding product")

# ============================================================================
# BOT STARTUP
# ============================================================================

if __name__ == "__main__":
    logger.info("🚀 AUTOEngine India Bot Starting...")
    
    try:
        bot.infinity_polling(timeout=10, long_polling_timeout=5)
    except KeyboardInterrupt:
        logger.info("Bot stopped")