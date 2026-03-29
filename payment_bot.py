import re
import logging
import asyncio
import aiohttp
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from datetime import datetime

# ========== CONFIG ==========
BOT_TOKEN = "8746590870:AAEEasQ1ruOx56JOfaerAa0EgtwuNENhNVc"
CHAT_ID = 7318114944

# Firebase REST API (কোনো serviceAccountKey লাগবে না)
FIREBASE_PROJECT = "sagor-a0803"
FIREBASE_API_KEY = "AIzaSyApAp2f-ukEjCYwNanmIL_7yiit8XB9yzM"
FIRESTORE_URL = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT}/databases/(default)/documents"

# ========== LOGGING ==========
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== SMS PARSER ==========
def parse_sms(text):
    result = {}
    amount_match = re.search(r'(?:Amount|Tk)[:\s]*(?:Tk\s*)?([0-9,]+(?:\.[0-9]{1,2})?)', text, re.IGNORECASE)
    if amount_match:
        result['amount'] = float(amount_match.group(1).replace(',', ''))
    txn_match = re.search(r'(?:TxnID|TrxID|Txn\s*ID|Transaction\s*ID)[:\s]*([A-Z0-9]{5,20})', text, re.IGNORECASE)
    if txn_match:
        result['txn_id'] = txn_match.group(1).strip().upper()
    sender_match = re.search(r'(?:Sender|From)[:\s]*(01[0-9]{9})', text, re.IGNORECASE)
    if sender_match:
        result['sender'] = sender_match.group(1)
    return result

# ========== FIRESTORE REST HELPERS ==========
def fs_value(val):
    """Python value → Firestore REST format"""
    if isinstance(val, bool):
        return {"booleanValue": val}
    elif isinstance(val, int):
        return {"integerValue": str(val)}
    elif isinstance(val, float):
        return {"doubleValue": val}
    elif isinstance(val, str):
        return {"stringValue": val}
    return {"stringValue": str(val)}

def parse_fs_value(v):
    """Firestore REST value → Python"""
    if "stringValue" in v: return v["stringValue"]
    if "integerValue" in v: return int(v["integerValue"])
    if "doubleValue" in v: return float(v["doubleValue"])
    if "booleanValue" in v: return v["booleanValue"]
    return None

def parse_fs_doc(doc):
    """Firestore document → Python dict"""
    fields = doc.get("fields", {})
    return {k: parse_fs_value(v) for k, v in fields.items()}

async def fs_query(session, collection, filters_list):
    """Firestore structured query"""
    url = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT}/databases/(default)/documents:runQuery?key={FIREBASE_API_KEY}"
    
    where_filters = []
    for field, op, value in filters_list:
        where_filters.append({
            "fieldFilter": {
                "field": {"fieldPath": field},
                "op": op,
                "value": fs_value(value)
            }
        })
    
    if len(where_filters) == 1:
        where_clause = where_filters[0]
    else:
        where_clause = {"compositeFilter": {"op": "AND", "filters": where_filters}}
    
    body = {
        "structuredQuery": {
            "from": [{"collectionId": collection}],
            "where": where_clause,
            "limit": 5
        }
    }
    
    async with session.post(url, json=body) as resp:
        data = await resp.json()
        results = []
        for item in data:
            if "document" in item:
                doc = item["document"]
                name = doc["name"]
                doc_id = name.split("/")[-1]
                fields = parse_fs_doc(doc)
                results.append({"id": doc_id, "data": fields, "name": name})
        return results

async def fs_add(session, collection, data):
    """Firestore এ নতুন document add করো"""
    url = f"{FIRESTORE_URL}/{collection}?key={FIREBASE_API_KEY}"
    fields = {k: fs_value(v) for k, v in data.items()}
    async with session.post(url, json={"fields": fields}) as resp:
        return await resp.json()

async def fs_update(session, doc_name, data):
    """Firestore document update করো"""
    url = f"https://firestore.googleapis.com/v1/{doc_name}?key={FIREBASE_API_KEY}"
    fields = {k: fs_value(v) for k, v in data.items()}
    # PATCH দিয়ে update
    update_mask = "&".join([f"updateMask.fieldPaths={k}" for k in data.keys()])
    patch_url = f"{url}&{update_mask}"
    async with session.patch(patch_url, json={"fields": fields}) as resp:
        return await resp.json()

async def fs_get(session, collection, doc_id):
    """Single document get"""
    url = f"{FIRESTORE_URL}/{collection}/{doc_id}?key={FIREBASE_API_KEY}"
    async with session.get(url) as resp:
        if resp.status == 200:
            doc = await resp.json()
            return parse_fs_doc(doc), doc["name"]
        return None, None

# ========== CORE LOGIC ==========
async def save_sms_and_match(txn_id: str, amount: float, sender: str, raw_sms: str):
    async with aiohttp.ClientSession() as session:
        # আগে এই TxnID save আছে কিনা check
        existing = await fs_query(session, "txn_sms", [("txn_id", "EQUAL", txn_id)])
        if existing:
            logger.info(f"TxnID {txn_id} already saved.")
            # তবুও pending recharge match করার চেষ্টা করো
            await try_approve(session, txn_id, amount)
            return

        # SMS save করো
        await fs_add(session, "txn_sms", {
            "txn_id": txn_id,
            "amount": amount,
            "sender": sender,
            "raw_sms": raw_sms,
            "received_at": datetime.now().isoformat(),
            "used": False
        })
        logger.info(f"💾 SMS saved → TxnID: {txn_id} | ৳{amount}")

        # Pending recharge match করার চেষ্টা
        await try_approve(session, txn_id, amount)

async def try_approve(session, txn_id: str, sms_amount: float):
    """Pending recharge খুঁজে approve করো"""
    recharges = await fs_query(session, "recharges", [
        ("trxId", "EQUAL", txn_id),
        ("status", "EQUAL", "pending")
    ])

    if not recharges:
        logger.info(f"⏳ No pending recharge for {txn_id} — SMS saved, will approve when user submits.")
        return

    recharge = recharges[0]
    rd = recharge["data"]
    submitted_amount = float(rd.get("amount", 0))

    # Amount check (±2 টাকা tolerance)
    if abs(submitted_amount - sms_amount) > 2:
        logger.warning(f"❌ Amount mismatch | TxnID: {txn_id} | SMS: ৳{sms_amount} | Submitted: ৳{submitted_amount}")
        return

    user_id = rd.get("userId", "")

    # Recharge approve করো
    await fs_update(session, recharge["name"], {
        "status": "approved",
        "approvedAt": datetime.now().isoformat(),
        "autoApproved": True
    })

    # User balance update
    user_data, user_name = await fs_get(session, "users", user_id)
    if user_data:
        current_balance = float(user_data.get("balance", 0))
        new_balance = current_balance + submitted_amount
        await fs_update(session, user_name, {"balance": new_balance})

        # SMS used mark করো
        sms_docs = await fs_query(session, "txn_sms", [
            ("txn_id", "EQUAL", txn_id),
            ("used", "EQUAL", False)
        ])
        if sms_docs:
            await fs_update(session, sms_docs[0]["name"], {"used": True})

        logger.info(
            f"✅ AUTO APPROVED | TxnID: {txn_id} | ৳{submitted_amount} | "
            f"User: {rd.get('userName','?')} | New Balance: ৳{new_balance}"
        )

# ========== POLLING: নতুন pending recharge check ==========
async def poll_pending_recharges():
    """
    প্রতি ১০ সেকেন্ডে pending recharge check করো।
    যদি saved SMS match করে → auto approve।
    """
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                recharges = await fs_query(session, "recharges", [
                    ("status", "EQUAL", "pending")
                ])
                for recharge in recharges:
                    rd = recharge["data"]
                    txn_id = rd.get("trxId", "").upper()
                    amount = float(rd.get("amount", 0))
                    if not txn_id:
                        continue

                    # Saved SMS এ আছে কিনা
                    sms_docs = await fs_query(session, "txn_sms", [
                        ("txn_id", "EQUAL", txn_id),
                        ("used", "EQUAL", False)
                    ])
                    if sms_docs:
                        sms_amount = float(sms_docs[0]["data"].get("amount", 0))
                        if abs(amount - sms_amount) <= 2:
                            logger.info(f"🔔 Match found in poll | TxnID: {txn_id}")
                            await try_approve(session, txn_id, sms_amount)

        except Exception as e:
            logger.error(f"Poll error: {e}")

        await asyncio.sleep(10)  # প্রতি ১০ সেকেন্ডে check

# ========== TELEGRAM HANDLER ==========
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return
    if message.chat_id != CHAT_ID:
        return

    text = message.text
    parsed = parse_sms(text)

    if not parsed.get('txn_id') or not parsed.get('amount'):
        return

    txn_id = parsed['txn_id']
    amount = parsed['amount']
    sender = parsed.get('sender', 'Unknown')

    logger.info(f"📩 SMS → TxnID: {txn_id} | ৳{amount} | Sender: {sender}")
    await save_sms_and_match(txn_id, amount, sender, text)

# ========== MAIN ==========
async def main():
    logger.info("🤖 NetShield Payment Bot চালু হচ্ছে...")

    # Background polling চালু করো
    asyncio.create_task(poll_pending_recharges())

    # Telegram bot চালু করো
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("✅ Bot ready! SMS এর জন্য অপেক্ষা করছে...")
    await app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    asyncio.run(main())
