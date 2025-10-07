import logging
import os
import sqlite3
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from telegram import Bot
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', handlers=[logging.FileHandler('bot_logs.txt')])
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')  # 8020429707:AAFRF6Oeh9Qttm3ibzwzMQDYhxoPhuJCYNA
GROUP_ID = int(os.getenv('GROUP_ID'))  # ID do grupo
ADMIN_ID = 6426059059
KIWIFY_WEBHOOK_SECRET = os.getenv('KIWIFY_WEBHOOK_SECRET', '')
DB_PATH = 'subscriptions.db'

app = Flask(__name__)
bot = Bot(TELEGRAM_TOKEN)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS subscriptions
                 (user_id INTEGER PRIMARY KEY, email TEXT, plan TEXT, expiry DATE, status TEXT)''')
    conn.commit()
    conn.close()

init_db()

def add_subscription(user_id, email, plan, expiry_date):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO subscriptions VALUES (?, ?, ?, ?, 'active')", (user_id, email, plan, expiry_date))
    conn.commit()
    conn.close()
    logger.info(f"Assinatura adicionada: {user_id}, {plan}, expiry {expiry_date}")
    # Adicionar ao grupo
    bot.unban_chat_member(GROUP_ID, user_id)
    bot.send_message(user_id, f"Assinatura {plan} aprovada! Bem-vindo ao grupo de mentoria.")

def get_user_id_from_email(email):
    # Implemente: mapa email para user_id (ex.: DB ou manual)
    email_map = {}  # Ex.: {'aluno@email.com': 123456789}
    return email_map.get(email)

def remove_from_group(user_id):
    bot.ban_chat_member(GROUP_ID, user_id)
    bot.send_message(user_id, "Assinatura expirada. Removido do grupo. Renove na Kiwify!")
    logger.info(f"Removido {user_id} do grupo")

def check_daily(context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    today = datetime.now().date()
    c.execute("SELECT * FROM subscriptions WHERE status = 'active'")
    results = c.fetchall()
    conn.close()
    for sub in results:
        user_id, email, plan, expiry, status = sub
        expiry_date = datetime.strptime(expiry, '%Y-%m-%d').date()
        days_left = (expiry_date - today).days
        if days_left <= 0:
            c.execute("UPDATE subscriptions SET status = 'expired' WHERE user_id = ?", (user_id,))
            conn.commit()
            remove_from_group(user_id)
            bot.send_message(ADMIN_ID, f"Removido {user_id} ({email}) por expiração.")
        elif 3 <= days_left <= 5:
            bot.send_message(user_id, f"Sua assinatura {plan} vence em {days_left} dias. Renove!")
            bot.send_message(ADMIN_ID, f"Aviso para {user_id} ({email}): {days_left} dias.")

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    if KIWIFY_WEBHOOK_SECRET and data.get('token') != KIWIFY_WEBHOOK_SECRET:
        return jsonify({'error': 'Unauthorized'}), 401

    event = data.get('event')
    payload = data.get('data', {})
    email = payload.get('user_email')
    plan_amount = payload.get('plan_amount', 0)
    expiry = payload.get('expiry_date')  # Formato YYYY-MM-DD

    user_id = get_user_id_from_email(email)
    if not user_id:
        logger.warning(f"Email {email} não mapeado.")
        return jsonify({'status': 'email not mapped'}), 200

    plan = 'mensal' if plan_amount == 100 else 'trimestral' if plan_amount == 260 else 'desconhecido'

    if event in ['Compra aprovada', 'Assinatura renovada']:
        add_subscription(user_id, email, plan, expiry)
        bot.send_message(ADMIN_ID, f"Nova/renovada {plan} para {email} (user_id {user_id}).")
    elif event in ['Assinatura cancelada', 'Assinatura atrasada']:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("UPDATE subscriptions SET status = 'expired' WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        remove_from_group(user_id)
        bot.send_message(ADMIN_ID, f"Cancelado/atrasado {plan} para {email} (user_id {user_id}).")

    return jsonify({'status': 'success'}), 200

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot Lucas Allan ativo! Gerenciando assinaturas.")

async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("Acesso negado.")
        return
    await check_daily(context)
    await update.message.reply_text("Verificação concluída.")

def main():
    global app
    app = Flask(__name__)
    
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.job_queue.run_daily(check_daily, time=datetime.time(9, 0))  # Diária às 9h
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("check", check))
    
    from threading import Thread
    Thread(target=application.run_polling, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))

if __name__ == '__main__':
    main()