"""
NetShield BD — Auto Payment Bot
================================
দুইটা দিক থেকে কাজ করে:
১. SMS আসলে → সাথে সাথে pending recharge খোঁজে approve করে
২. New recharge request আসলে → সাথে সাথে saved SMS খোঁজে approve করে
   (Firestore realtime listen + fast poller দিয়ে)
"""

import re
import logging
import asyncio
import aiohttp
import json
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from datetime import datetime

# ========== CONFIG ==========
BOT_TOKEN        = "8746590870:AAEEasQ1ruOx56JOfaerAa0EgtwuNENhNVc"
CHAT_ID          = 7318114944
FIREBASE_PROJECT = "sagor-a0803"
FIREBASE_API_KEY = "AIzaSyApAp2f-ukEjCYwNanmIL_7yiit8XB9yzM"
FIRESTORE_URL    = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT}/databases/(default)/documents"

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ইতিমধ্যে approve করা TrxID track করো — double approve ঠেকাতে
approved_set = set()

# ========== SMS PARSER ==========
def parse_sms(text: str) -> dict | None:
    """
    বিকাশ: You have received Tk 50.00 from 01XXXXXXXXX. TrxID DCT8LLFMDG
    নগদ:  Cash In of BDT 50.00 from 01XXXXXXXXX successful. TrxID: ABC123
    """
    # Amount
    amt = re.search(
        r'(?:received|Cash\s*In(?:\s*of)?)\s+(?:Tk|BDT)\s*([\d,]+(?:\.\d+)?)',
        text, re.IGNORECASE
    )
    if not amt:
        amt = re.search(r'(?:Tk|BDT)\s*([\d,]+(?:\.\d+)?)', text, re.IGNORECASE)

    # TrxID
    trx = re.search(
        r'(?:TrxID|TxnID|Txn\s*ID|Transaction\s*ID)[:\s]+([A-Z0-9]{5,20})',
        text, re.IGNORECASE
    )

    if not amt or not trx:
        return None

    sender = re.search(r'from\s+(01[0-9]{9})', text, re.IGNORECASE)

    return {
        'amount': float(amt.group(1).replace(',', '')),
        'txn_id': trx.group(1).strip().upper(),
        'sender': sender.group(1) if sender else '',
        'method': 'Nagad' if 'nagad' in text.lower() else 'bKash'
    }

# ========== FIRESTORE REST ==========
def fs_val(v):
    if isinstance(v, bool):  return {"booleanValue": v}
    if isinstance(v, int):   return {"integerValue": str(v)}
    if isinstance(v, float): return {"doubleValue": v}
    return {"stringValue": str(v)}

def parse_val(v):
    if "stringValue"  in v: return v["stringValue"]
    if "integerValue" in v: return int(v["integerValue"])
    if "doubleValue"  in v: return float(v["doubleValue"])
    if "booleanValue" in v: return v["booleanValue"]
    return None

def parse_doc(doc):
    return {k: parse_val(v) for k, v in doc.get("fields", {}).items()}

async def fs_query(session, col, filter_list, limit=20):
    url = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT}/databases/(default)/documents:runQuery?key={FIREBASE_API_KEY}"
    flt = [{"fieldFilter": {"field": {"fieldPath": f}, "op": op, "value": fs_val(v)}} for f, op, v in filter_list]
    where = flt[0] if len(flt) == 1 else {"compositeFilter": {"op": "AND", "filters": flt}}
    body  = {"structuredQuery": {"from": [{"collectionId": col}], "where": where, "limit": limit}}
    async with session.post(url, json=body) as r:
        data = await r.json()
        return [{"id": d["document"]["name"].split("/")[-1],
                 "data": parse_doc(d["document"]),
                 "name": d["document"]["name"]} for d in data if "document" in d]

async def fs_get(session, col, doc_id):
    url = f"{FIRESTORE_URL}/{col}/{doc_id}?key={FIREBASE_API_KEY}"
    async with session.get(url) as r:
        if r.status == 200:
            doc = await r.json()
            return parse_doc(doc), doc["name"]
        return None, None

async def fs_update(session, name, data):
    mask = "&".join([f"updateMask.fieldPaths={k}" for k in data])
    url  = f"https://firestore.googleapis.com/v1/{name}?key={FIREBASE_API_KEY}&{mask}"
    async with session.patch(url, json={"fields": {k: fs_val(v) for k, v in data.items()}}) as r:
        return await r.json()

async def fs_add(session, col, data):
    url = f"{FIRESTORE_URL}/{col}?key={FIREBASE_API_KEY}"
    async with session.post(url, json={"fields": {k: fs_val(v) for k, v in data.items()}}) as r:
        return await r.json()

# ========== CORE: APPROVE ==========
async def approve(session, trx_id: str, sms_amount: float) -> str | None:
    """
    Firebase-এ pending recharge খুঁজে approve করো।
    Return: success/fail message অথবা None (recharge নেই)
    """
    # Double approve ঠেকাও
    if trx_id in approved_set:
        return None

    # TrxID দিয়ে খোঁজো (upper + lower)
    recharges = await fs_query(session, "recharges", [
        ("trxId", "EQUAL", trx_id.upper()), ("status", "EQUAL", "pending")
    ])
    if not recharges:
        recharges = await fs_query(session, "recharges", [
            ("trxId", "EQUAL", trx_id.lower()), ("status", "EQUAL", "pending")
        ])
    if not recharges:
        return None  # এখনো request আসেনি

    r  = recharges[0]
    rd = r["data"]
    submitted = float(rd.get("amount", 0))

    # ★ Amount চেক — SMS amount ≠ submitted amount → reject
    if abs(submitted - sms_amount) > 2:
        await fs_update(session, r["name"], {"status": "rejected"})
        logger.warning(f"🚫 REJECT | {trx_id} | SMS:৳{sms_amount} | Request:৳{submitted}")
        return (f"🚫 Amount মিলছে না — REJECT!\n\n"
                f"👤 {rd.get('userName','?')} ({rd.get('userPhone','?')})\n"
                f"SMS-এ এসেছে: ৳{sms_amount}\n"
                f"Request-এ দিয়েছে: ৳{submitted}\n"
                f"⛔ সম্ভাব্য প্রতারণা!")

    # Balance আপডেট
    uid = rd.get("userId", "")
    user, upath = await fs_get(session, "users", uid)
    if not user:
        return "❌ ইউজার পাওয়া যায়নি!"

    old_bal = float(user.get("balance", 0))
    new_bal = old_bal + submitted

    # Approve + Balance একসাথে
    await asyncio.gather(
        fs_update(session, r["name"], {
            "status": "approved",
            "approvedAt": datetime.now().isoformat(),
            "autoApproved": True
        }),
        fs_update(session, upath, {"balance": new_bal})
    )

    approved_set.add(trx_id)  # track করো
    logger.info(f"✅ APPROVED | {trx_id} | ৳{submitted} | {rd.get('userName')} | ৳{old_bal}→৳{new_bal}")

    return (f"✅ অটো অ্যাপ্রুভ!\n\n"
            f"👤 {rd.get('userName','?')}\n"
            f"📱 {rd.get('userPhone','?')}\n"
            f"💰 ৳{submitted} ({rd.get('method','bKash')})\n"
            f"🔑 {trx_id}\n"
            f"আগের ব্যালেন্স: ৳{old_bal}\n"
            f"নতুন ব্যালেন্স: ৳{new_bal}")

# ========== SMS PROCESSING ==========
async def process_sms(parsed: dict, bot):
    """SMS আসলে এই function call হয়"""
    txn_id = parsed['txn_id']
    amount = parsed['amount']

    async with aiohttp.ClientSession() as session:
        # ১. সাথে সাথে approve করার চেষ্টা
        result = await approve(session, txn_id, amount)

        if result:
            # Approve হয়েছে বা reject হয়েছে
            await bot.send_message(chat_id=CHAT_ID, text=result)
        else:
            # Pending recharge নেই — SMS save করো
            await fs_add(session, "txn_sms", {
                "txn_id": txn_id,
                "amount": amount,
                "sender": parsed.get('sender', ''),
                "method": parsed.get('method', 'bKash'),
                "received_at": datetime.now().isoformat(),
                "used": False
            })
            logger.info(f"💾 SMS saved | {txn_id} | ৳{amount} — waiting for recharge request")

# ========== POLLER: নতুন pending recharge আসলে ==========
async def recharge_poller(bot):
    """
    ইউজার যখনই TrxID দিয়ে recharge request দেবে,
    এই poller ৩ সেকেন্ডের মধ্যে ধরবে এবং saved SMS দিয়ে approve করবে
    """
    logger.info("🔄 Recharge Poller চালু (প্রতি ৩ সেকেন্ড)")
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                pending = await fs_query(session, "recharges", [
                    ("status", "EQUAL", "pending")
                ], limit=50)

                for r in pending:
                    rd     = r["data"]
                    trx_id = rd.get("trxId", "").strip().upper()
                    if not trx_id or trx_id in approved_set:
                        continue

                    # এই TrxID-র saved SMS আছে?
                    sms_docs = await fs_query(session, "txn_sms", [
                        ("txn_id", "EQUAL", trx_id),
                        ("used",   "EQUAL", False)
                    ])
                    if not sms_docs:
                        continue

                    sms_amount = float(sms_docs[0]["data"].get("amount", 0))
                    result = await approve(session, trx_id, sms_amount)

                    if result:
                        # SMS used mark করো
                        await fs_update(session, sms_docs[0]["name"], {"used": True})
                        await bot.send_message(chat_id=CHAT_ID, text=result)

        except Exception as e:
            logger.error(f"Poller error: {e}")

        await asyncio.sleep(3)  # ৩ সেকেন্ড

# ========== TELEGRAM HANDLERS ==========
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text or msg.chat_id != CHAT_ID:
        return

    parsed = parse_sms(msg.text)
    if not parsed:
        return

    logger.info(f"📩 SMS | TrxID:{parsed['txn_id']} | ৳{parsed['amount']} | {parsed['sender']}")
    await process_sms(parsed, context.bot)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat_id != CHAT_ID:
        return
    await update.message.reply_text(
        "🤖 NetShield BD Payment Bot চালু!\n\n"
        "SMS আসলে → সাথে সাথে approve ✅\n"
        "Request আগে, SMS পরে → ৩ সেকেন্ডে approve ✅\n"
        "Amount গরমিল → reject 🚫\n\n"
        "/pending — পেন্ডিং দেখুন"
    )

async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat_id != CHAT_ID:
        return
    async with aiohttp.ClientSession() as session:
        items = await fs_query(session, "recharges", [("status", "EQUAL", "pending")], limit=20)
    if not items:
        await update.message.reply_text("✅ কোনো পেন্ডিং নেই!")
        return
    msg = f"⏳ পেন্ডিং: {len(items)} টি\n\n"
    for r in items:
        d = r['data']
        msg += f"👤 {d.get('userName','?')} | ৳{d.get('amount','?')} | {d.get('trxId','?')}\n"
    await update.message.reply_text(msg)

# ========== MAIN ==========
async def main():
    logger.info("🤖 NetShield Payment Bot শুরু হচ্ছে...")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async with app:
        await app.start()
        asyncio.create_task(recharge_poller(app.bot))
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("✅ Bot ও Poller চালু!")
        await asyncio.Event().wait()

if __name__ == '__main__':
    asyncio.run(main())
