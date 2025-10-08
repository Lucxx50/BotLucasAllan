import logging
import os
import sqlite3
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from telegram import Bot, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from dotenv import load_dotenv
import requests
import sys

# Carrega .env
load_dotenv()

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', handlers=[logging.FileHandler('bot_logs.txt')])
logger = logging.getLogger(__name__)

# Variáveis
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')  # 8020429707:AAFRF6Oeh9Qttm3ibzwzMQDYhxoPhuJCYNA
GROUP_ID = int(os.getenv('GROUP_ID'))  # -2777853613
ADMIN_ID = 6426059059
KIWIFY_WEBHOOK_SECRET = os.getenv('KIWIFY_WEBHOOK_SECRET', '')
DB_PATH = 'subscriptions.db'

# Inicializar DB
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS subscriptions
                 (user_id INTEGER PRIMARY KEY, email TEXT, plan TEXT, expiry DATE, status TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS email_mapping
                 (email TEXT PRIMARY KEY, user_id INTEGER)''')
    conn.commit()
    conn.close()

init_db()

# Adicionar assinatura
def add_subscription(user_id, email, plan, expiry_date):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO subscriptions VALUES (?, ?, ?, ?, 'active')", (user_id, email, plan, expiry_date))
    conn.commit()
    conn.close()
    logger.info(f"Assinatura adicionada: {user_id}, {plan}, expiry {expiry_date}")
    # Adicionar ao grupo
    bot = Bot(TELEGRAM_TOKEN)
    try:
        bot.unban_chat_member(GROUP_ID, user_id)
        bot.send_message(user_id, f"Assinatura {plan} aprovada! Bem-vindo ao grupo de mentoria.")
    except Exception as e:
        logger.error(f"Erro ao adicionar {user_id} ao grupo: {e}")

# Mapear email para user_id
def get_user_id_from_email(email):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id FROM email_mapping WHERE email = ?", (email,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None

# Verificar assinaturas expirando
def get_expiring(days=5):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    today = datetime.now().date()
    c.execute("SELECT * FROM subscriptions WHERE status = 'active' AND expiry BETWEEN ? AND ?", (today, today + timedelta(days=days)))
    results = c.fetchall()
    conn.close()
    return results

# Desativar assinatura
def deactivate(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE subscriptions SET status = 'expired' WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    logger.info(f"Desativada assinatura de {user_id}")

# Remover do grupo
def remove_from_group(user_id):
    bot = Bot(TELEGRAM_TOKEN)
    try:
        bot.ban_chat_member(GROUP_ID, user_id)
        bot.send_message(user_id, "Assinatura expirada. Removido do grupo. Renove na Kiwify!")
        logger.info(f"Removido {user_id} do grupo")
    except Exception as e:
        logger.error(f"Erro ao remover {user_id} do grupo: {e}")

# Webhook Kiwify
app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    logger.info(f"Webhook recebido: {data}")
    if KIWIFY_WEBHOOK_SECRET:
        token = data.get('token') or data.get('data', {}).get('token')
        if token != KIWIFY_WEBHOOK_SECRET:
            logger.warning(f"Token inválido: {token}")
            return jsonify({'error': 'Unauthorized'}), 401

    event = data.get('event')
    payload = data.get('data', {})
    email = payload.get('user_email')
    plan_amount = payload.get('plan_amount', 0)
    expiry = payload.get('expiry_date')

    user_id = get_user_id_from_email(email)
    if not user_id:
        logger.warning(f"Email {email} não mapeado.")
        return jsonify({'status': 'email not mapped'}), 200

    plan = 'mensal' if plan_amount == 100 else 'trimestral' if plan_amount == 260 else 'desconhecido'

    bot = Bot(TELEGRAM_TOKEN)
    if event in ['Compra aprovada', 'Assinatura renovada']:
        add_subscription(user_id, email, plan, expiry)
        bot.send_message(ADMIN_ID, f"Nova/renovada {plan} para {email} (user_id {user_id}).")
    elif event in ['Assinatura cancelada', 'Assinatura atrasada']:
        deactivate(user_id)
        remove_from_group(user_id)
        bot.send_message(ADMIN_ID, f"Cancelado/atrasado {plan} para {email} (user_id {user_id}).")

    logger.info(f"Webhook processado: {event} para {email}")
    return jsonify({'status': 'success'}), 200

# Job diário
async def check_daily(context: ContextTypes.DEFAULT_TYPE):
    expiring = get_expiring(5)
    for sub in expiring:
        user_id, email, plan, expiry, status = sub
        expiry_date = datetime.strptime(expiry, '%Y-%m-%d').date()
        days_left = (expiry_date - datetime.now().date()).days
        if 3 <= days_left <= 5:
            context.bot.send_message(user_id, f"Sua assinatura {plan} vence em {days_left} dias. Renove na Kiwify!")
            context.bot.send_message(ADMIN_ID, f"Aviso para {user_id} ({email}): {days_left} dias.")
        elif days_left <= 0:
            deactivate(user_id)
            remove_from_group(user_id)
            context.bot.send_message(ADMIN_ID, f"Removido {user_id} ({email}) por expiração.")

# Comandos do Telegram
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Comando /start recebido de chat_id: {update.effective_chat.id}")
    await context.bot.send_message(chat_id=update.effective_chat.id, text="Bot Lucas Allan: Monitor de assinaturas Kiwify ativo.")

async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id != ADMIN_ID:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Acesso negado.")
        return
    await check_daily(context)
    await context.bot.send_message(chat_id=update.effective_chat.id, text="Verificação concluída.")

async def register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not context.args:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Por favor, forneça seu email: /register seu@email.com")
        return
    email = context.args[0]
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO email_mapping (email, user_id) VALUES (?, ?)", (email, user_id))
    conn.commit()
    conn.close()
    logger.info(f"Email {email} registrado para user_id {user_id}")
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Email {email} registrado com sucesso!")

def run_telegram_bot():
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    if not application.job_queue:
        logger.error("JobQueue não está configurado. Instale python-telegram-bot[job-queue].")
        raise RuntimeError("JobQueue não configurado.")
    
    from datetime import time
    schedule_time = time(hour=9, minute=0)
    application.job_queue.run_daily(check_daily, time=schedule_time)
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("check", check))
    application.add_handler(CommandHandler("register", register))
    
    application.run_polling()

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--telegram':
        run_telegram_bot()
    else:
        app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))