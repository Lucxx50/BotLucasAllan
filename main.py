import logging
import os
import sqlite3
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from telegram import Bot, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, ChatMemberHandler
from telegram.constants import ChatMemberStatus
from telegram.error import Forbidden
from dotenv import load_dotenv
import requests
import sys

# Carrega .env
load_dotenv()

# Logging com codificação UTF-8
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('bot_logs.txt', encoding='utf-8')]
)
logger = logging.getLogger(__name__)

# Variáveis
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
GROUP_ID = int(os.getenv('GROUP_ID', '-2777853613'))
ADMIN_ID = 6426059059
KIWIFY_WEBHOOK_SECRET = os.getenv('KIWIFY_WEBHOOK_SECRET', '')
DB_PATH = 'subscriptions.db'

# Lista para rastrear novos membros
new_members = {}  # {user_id: timestamp}

# Inicializar DB
def init_db():
    try:
        logger.info("Tentando inicializar banco SQLite")
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS subscriptions
                     (user_id INTEGER PRIMARY KEY, email TEXT, plan TEXT, expiry DATE, status TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS email_mapping
                     (email TEXT PRIMARY KEY, user_id INTEGER)''')
        conn.commit()
        conn.close()
        logger.info("Banco SQLite inicializado com sucesso")
    except Exception as e:
        logger.error(f"Erro ao inicializar banco SQLite: {e}")
        raise

try:
    init_db()
except Exception as e:
    logger.error(f"Falha ao inicializar banco na inicialização: {e}")
    raise

# Adicionar assinatura
def add_subscription(user_id, email, plan, expiry_date):
    try:
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
        # Remover da lista de verificação
        new_members.pop(user_id, None)
    except Exception as e:
        logger.error(f"Erro ao adicionar assinatura: {e}")

# Mapear email para user_id
def get_user_id_from_email(email):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT user_id FROM email_mapping WHERE email = ?", (email,))
        result = c.fetchone()
        conn.close()
        return result[0] if result else None
    except Exception as e:
        logger.error(f"Erro ao buscar user_id para email {email}: {e}")
        return None

# Verificar assinaturas expirando
def get_expiring(days=5):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        today = datetime.now().date()
        c.execute("SELECT * FROM subscriptions WHERE status = 'active' AND expiry BETWEEN ? AND ?", (today, today + timedelta(days=days)))
        results = c.fetchall()
        conn.close()
        return results
    except Exception as e:
        logger.error(f"Erro ao buscar assinaturas expirando: {e}")
        return []

# Verificar se usuário tem assinatura ativa
def has_active_subscription(user_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT * FROM subscriptions WHERE user_id = ? AND status = 'active'", (user_id,))
        result = c.fetchone()
        conn.close()
        return bool(result)
    except Exception as e:
        logger.error(f"Erro ao verificar assinatura ativa para user_id {user_id}: {e}")
        return False

# Desativar assinatura
def deactivate(user_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("UPDATE subscriptions SET status = 'expired' WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        logger.info(f"Desativada assinatura de {user_id}")
    except Exception as e:
        logger.error(f"Erro ao desativar assinatura de {user_id}: {e}")

# Remover do grupo
def remove_from_group(user_id):
    bot = Bot(TELEGRAM_TOKEN)
    try:
        bot.ban_chat_member(GROUP_ID, user_id)
        try:
            bot.send_message(user_id, "Você foi removido do grupo por não ter uma assinatura ativa. Compre ou renove na Kiwify!")
        except Forbidden as e:
            logger.warning(f"Não foi possível enviar DM para {user_id}: {e}")
        logger.info(f"Removido {user_id} do grupo")
    except Exception as e:
        logger.error(f"Erro ao remover {user_id} do grupo: {e}")

# Verificar permissões do bot
async def check_bot_permissions(context: ContextTypes.DEFAULT_TYPE):
    try:
        bot = context.bot
        chat_member = await bot.get_chat_member(GROUP_ID, bot.id)
        if chat_member.status != ChatMemberStatus.ADMINISTRATOR:
            logger.error(f"Bot {bot.id} não é administrador no grupo {GROUP_ID}")
            await context.bot.send_message(ADMIN_ID, f"Bot não é administrador no grupo {GROUP_ID}. Verifique!")
            return False
        if not (chat_member.can_post_messages or chat_member.can_delete_messages):
            logger.error(f"Bot {bot.id} sem permissões necessárias no grupo {GROUP_ID}")
            await context.bot.send_message(ADMIN_ID, f"Bot sem permissões necessárias (enviar ou deletar mensagens) no grupo {GROUP_ID}. Verifique!")
            return False
        logger.info(f"Permissões do bot verificadas com sucesso no grupo {GROUP_ID}")
        return True
    except Exception as e:
        logger.error(f"Erro ao verificar permissões do bot: {e}")
        await context.bot.send_message(ADMIN_ID, f"Erro ao verificar permissões do bot: {e}")
        return False

# Verificar bot na inicialização
async def verify_bot(context: ContextTypes.DEFAULT_TYPE):
    try:
        bot = context.bot
        me = await bot.get_me()
        logger.info(f"Bot verificado: {me.username} ({me.id})")
        if not await check_bot_permissions(context):
            logger.error("Bot sem permissões adequadas na inicialização")
    except Exception as e:
        logger.error(f"Erro ao verificar bot: {e}")
        await context.bot.send_message(ADMIN_ID, f"Erro ao verificar bot: {e}")

# Webhook Kiwify
app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def kiwify_webhook():
    try:
        data = request.json
        logger.info(f"Webhook Kiwify recebido: {data}")
    except Exception as e:
        logger.error(f"Erro ao processar JSON: {e}")
        return jsonify({'error': 'Invalid JSON'}), 400

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

    if not all([event, email, expiry]):
        logger.error(f"Dados incompletos: event={event}, email={email}, expiry={expiry}")
        return jsonify({'error': 'Missing required fields'}), 400

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

    logger.info(f"Webhook Kiwify processado: {event} para {email}")
    return jsonify({'status': 'success'}), 200

# Endpoint para verificar banimentos
@app.route('/check_bans', methods=['GET'])
def check_bans():
    try:
        logger.info("Endpoint /check_bans chamado")
        current_time = datetime.now()
        expired_users = []
        for user_id, join_time in new_members.items():
            if (current_time - join_time).total_seconds() >= 120:
                logger.info(f"Verificando assinatura para user_id={user_id} após 2 minutos")
                if not has_active_subscription(user_id):
                    logger.info(f"Sem assinatura ativa para user_id={user_id}. Banindo...")
                    remove_from_group(user_id)
                else:
                    logger.info(f"Assinatura ativa encontrada para user_id={user_id}")
                expired_users.append(user_id)
        for user_id in expired_users:
            new_members.pop(user_id, None)
        logger.info(f"Verificação de novos membros concluída. Usuários restantes: {new_members}")
        return jsonify({'status': 'success'}), 200
    except Exception as e:
        logger.error(f"Erro no endpoint /check_bans: {e}")
        return jsonify({'error': str(e)}), 500

# Job diário
async def check_daily(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Executando verificação diária de assinaturas")
    expiring = get_expiring(5)
    for sub in expiring:
        user_id, email, plan, expiry, status = sub
        expiry_date = datetime.strptime(str(expiry), '%Y-%m-%d').date()
        days_left = (expiry_date - datetime.now().date()).days
        if 3 <= days_left <= 5:
            try:
                context.bot.send_message(user_id, f"Sua assinatura {plan} vence em {days_left} dias. Renove na Kiwify!")
                context.bot.send_message(ADMIN_ID, f"Aviso para {user_id} ({email}): {days_left} dias.")
            except Forbidden as e:
                logger.warning(f"Não foi possível enviar mensagem para {user_id}: {e}")
        elif days_left <= 0:
            deactivate(user_id)
            remove_from_group(user_id)
            context.bot.send_message(ADMIN_ID, f"Removido {user_id} ({email}) por expiração.")

# Comandos do Telegram
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Comando /start recebido de chat_id: {update.effective_chat.id}, user_id={update.effective_user.id}")
    await context.bot.send_message(chat_id=update.effective_chat.id, text="Bot Lucas Allan: Monitor de assinaturas Kiwify ativo. Envie /register seu@email.com para vincular sua assinatura!")

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
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO email_mapping VALUES (?, ?)", (email, user_id))
        conn.commit()
        conn.close()
        logger.info(f"Email {email} registrado para user_id {user_id}")
        # Verificar se há assinatura ativa
        if has_active_subscription(user_id):
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Email {email} registrado com sucesso! Sua assinatura está ativa.")
            # Remover da lista de verificação se registrado
            new_members.pop(user_id, None)
        else:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Email {email} registrado, mas nenhuma assinatura ativa foi encontrada. Compre na Kiwify para acessar o grupo!")
    except Exception as e:
        logger.error(f"Erro ao registrar email {email} para user_id {user_id}: {e}")
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Erro ao registrar email. Tente novamente ou contate o suporte.")

async def new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        logger.info(f"Evento de novo membro detectado: user_id={update.chat_member.new_chat_member.user.id}, chat_id={update.chat_member.chat.id}")
        user_id = update.chat_member.new_chat_member.user.id
        chat_id = update.chat_member.chat.id
        username = update.chat_member.new_chat_member.user.username or update.chat_member.new_chat_member.user.first_name or "usuário"
        if chat_id != GROUP_ID:
            logger.info(f"Ignorando evento: chat_id {chat_id} não é GROUP_ID {GROUP_ID}")
            return
        # Registrar entrada do membro
        new_members[user_id] = datetime.now()
        logger.info(f"Novo membro {user_id} registrado na lista de verificação")
        # Enviar mensagem no grupo pedindo /start
        await context.bot.send_message(
            chat_id=GROUP_ID,
            text=f"@{username}, bem-vindo à Mentoria LA Macs e Notes! Envie /start para o bot em DM e depois use /register seu@email.com com o email da Kiwify. Você tem 2 minutos ou será removido!"
        )
        logger.info(f"Novo membro {user_id} no grupo. Mensagem enviada no grupo.")
        # Tentar enviar mensagem por DM
        try:
            await context.bot.send_message(
                user_id,
                f"Bem-vindo à Mentoria LA Macs e Notes, {username}! Envie /register seu@email.com com o email usado na Kiwify. Você tem 2 minutos ou será removido!"
            )
            logger.info(f"Novo membro {user_id} no grupo. DM enviada pedindo registro.")
        except Forbidden as e:
            logger.warning(f"Não foi possível enviar DM para {user_id}: {e}")
        # Verificar permissões do bot
        if not await check_bot_permissions(context):
            logger.error(f"Bot sem permissões adequadas no grupo {GROUP_ID}")
            await context.bot.send_message(ADMIN_ID, f"Bot sem permissões adequadas no grupo {GROUP_ID}. Verifique!")
    except Exception as e:
        logger.error(f"Erro ao processar novo membro {user_id}: {e}")
        await context.bot.send_message(ADMIN_ID, f"Erro ao processar novo membro {user_id}: {e}")

def run_telegram_bot():
    try:
        logger.info("Tentando construir aplicação Telegram")
        bot = Bot(TELEGRAM_TOKEN)
        logger.info("Bot inicializado com sucesso")
        application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        logger.info("Aplicação Telegram construída com sucesso")

        from datetime import time
        schedule_time = time(hour=9, minute=0)
        application.job_queue.run_daily(check_daily, schedule_time)
        logger.info("Job diário agendado para 9:00")

        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("check", check))
        application.add_handler(CommandHandler("register", register))
        application.add_handler(ChatMemberHandler(new_member, ChatMemberHandler.CHAT_MEMBER))
        logger.info("Handlers do Telegram registrados")

        # Verificar bot na inicialização
        application.job_queue.run_once(verify_bot, 0)
        logger.info("Verificação inicial do bot agendada")

        logger.info("Iniciando polling do bot Telegram")
        application.run_polling(
            allowed_updates=["chat_member", "message"],
            drop_pending_updates=True,
            poll_interval=1.0,
            timeout=20,
            bootstrap_retries=5,
            read_timeout=30,
            write_timeout=30,
            connect_timeout=30
        )
    except Exception as e:
        logger.error(f"Erro ao iniciar bot Telegram: {e}")
        raise

if __name__ == '__main__':
    try:
        logger.info("Iniciando aplicação: %s", sys.argv)
        if len(sys.argv) > 1 and sys.argv[1] == '--telegram':
            run_telegram_bot()
        else:
            app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
    except Exception as e:
        logger.error(f"Erro ao executar aplicação: {e}")
        raise