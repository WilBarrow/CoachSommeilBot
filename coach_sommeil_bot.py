import logging
import os
import json
from datetime import datetime, timedelta
import psycopg2
from psycopg2.extras import RealDictCursor
import stripe
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# Configuration du logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# États pour le diagnostic
AGE, SIESTES, COUCHER, REVEILS = range(4)

# Configuration Stripe
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')
STRIPE_PRICE_ID = os.environ.get('STRIPE_PRICE_ID')

# URL de la base de données
DATABASE_URL = os.environ.get('DATABASE_URL')

# =============================================================================
# GESTION DE LA BASE DE DONNÉES
# =============================================================================

def get_db_connection():
    """Crée une connexion à la base de données"""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        logger.error(f"Erreur connexion DB: {e}")
        return None

def init_database():
    """Initialise la base de données"""
    conn = get_db_connection()
    if not conn:
        logger.error("Impossible d'initialiser la base de données")
        return False
    
    try:
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username VARCHAR(255),
                first_name VARCHAR(255),
                is_premium BOOLEAN DEFAULT FALSE,
                subscription_until TIMESTAMP,
                stripe_customer_id VARCHAR(255),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        conn.commit()
        cursor.close()
        conn.close()
        
        logger.info("✅ Base de données initialisée")
        return True
        
    except Exception as e:
        logger.error(f"Erreur init DB: {e}")
        if conn:
            conn.close()
        return False

def get_user_data(user_id):
    """Récupère les données d'un utilisateur"""
    conn = get_db_connection()
    if not conn:
        return None
    
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
        user = cursor.fetchone()
        cursor.close()
        conn.close()
        return dict(user) if user else None
    except Exception as e:
        logger.error(f"Erreur get_user_data: {e}")
        if conn:
            conn.close()
        return None

def create_or_update_user(user_id, username=None, first_name=None):
    """Crée ou met à jour un utilisateur"""
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users WHERE user_id = %s", (user_id,))
        exists = cursor.fetchone()
        
        if exists:
            cursor.execute(
                "UPDATE users SET last_activity = CURRENT_TIMESTAMP WHERE user_id = %s",
                (user_id,)
            )
        else:
            cursor.execute(
                """INSERT INTO users (user_id, username, first_name, is_premium, created_at)
                   VALUES (%s, %s, %s, FALSE, CURRENT_TIMESTAMP)""",
                (user_id, username, first_name)
            )
        
        conn.commit()
        cursor.close()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Erreur create_or_update_user: {e}")
        if conn:
            conn.close()
        return False

def is_premium(user_id):
    """Vérifie si l'utilisateur a un abonnement premium actif"""
    user = get_user_data(user_id)
    if not user or not user['is_premium']:
        return False
    if user['subscription_until'] and datetime.now() > user['subscription_until']:
        deactivate_premium(user_id)
        return False
    return True

def activate_premium(user_id, months=1, stripe_customer_id=None):
    """Active l'abonnement premium"""
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        cursor = conn.cursor()
        expiry = datetime.now() + timedelta(days=30 * months)
        
        if stripe_customer_id:
            cursor.execute(
                """UPDATE users 
                   SET is_premium = TRUE, subscription_until = %s, stripe_customer_id = %s
                   WHERE user_id = %s""",
                (expiry, stripe_customer_id, user_id)
            )
        else:
            cursor.execute(
                """UPDATE users 
                   SET is_premium = TRUE, subscription_until = %s
                   WHERE user_id = %s""",
                (expiry, user_id)
            )
        
        conn.commit()
        cursor.close()
        conn.close()
        logger.info(f"✅ Premium activé pour user {user_id}")
        return True
    except Exception as e:
        logger.error(f"Erreur activate_premium: {e}")
        if conn:
            conn.close()
        return False

def deactivate_premium(user_id):
    """Désactive l'abonnement premium"""
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET is_premium = FALSE WHERE user_id = %s", (user_id,))
        conn.commit()
        cursor.close()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Erreur deactivate_premium: {e}")
        if conn:
            conn.close()
        return False

# =============================================================================
# WEBHOOK STRIPE
# =============================================================================

async def stripe_webhook(request):
    """Endpoint webhook pour Stripe"""
    try:
        payload = await request.text()
        sig_header = request.headers.get('stripe-signature')
        webhook_secret = os.environ.get('STRIPE_WEBHOOK_SECRET')
        
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
        except stripe.error.SignatureVerificationError:
            logger.error("❌ Signature webhook invalide")
            return web.Response(status=400)
        
        if event['type'] == 'checkout.session.completed':
            session = event['data']['object']
            user_id = session.get('client_reference_id')
            customer_id = session.get('customer')
            
            if user_id:
                activate_premium(int(user_id), months=1, stripe_customer_id=customer_id)
                logger.info(f"✅ Paiement confirmé pour user {user_id}")
        
        elif event['type'] == 'invoice.payment_succeeded':
            invoice = event['data']['object']
            customer_id = invoice.get('customer')
            
            conn = get_db_connection()
            if conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT user_id FROM users WHERE stripe_customer_id = %s",
                    (customer_id,)
                )
                result = cursor.fetchone()
                if result:
                    activate_premium(result[0], months=1)
                    logger.info(f"🔄 Abonnement renouvelé pour user {result[0]}")
                cursor.close()
                conn.close()
        
        elif event['type'] == 'customer.subscription.deleted':
            subscription = event['data']['object']
            customer_id = subscription.get('customer')
            
            conn = get_db_connection()
            if conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT user_id FROM users WHERE stripe_customer_id = %s",
                    (customer_id,)
                )
                result = cursor.fetchone()
                if result:
                    deactivate_premium(result[0])
                    logger.info(f"❌ Abonnement annulé pour user {result[0]}")
                cursor.close()
                conn.close()
        
        return web.Response(text='OK', status=200)
        
    except Exception as e:
        logger.error(f"Erreur webhook: {e}")
        return web.Response(status=400)
# =============================================================================
# COMMANDES BOT
# =============================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commande /start"""
    user = update.effective_user
    create_or_update_user(user.id, user.username, user.first_name)
    
    if context.args:
        if context.args[0] == 'payment_success':
            await update.message.reply_text(
                "✅ **Paiement réussi !**\n\n"
                "Ton abonnement Premium sera activé dans quelques instants.\n"
                "Tape /status pour vérifier.\n\n"
                "Si pas d'activation sous 5 min, contacte le support.",
                parse_mode='Markdown'
            )
            return
        elif context.args[0] == 'payment_cancel':
            await update.message.reply_text(
                "❌ **Paiement annulé**\n\n"
                "Pas de souci ! Tu peux réessayer avec /premium",
                parse_mode='Markdown'
            )
            return
    
    welcome_text = f"""
👋 Bonjour {user.first_name} !

Bienvenue sur **Coach Sommeil™** 🌙

Je suis là pour t'aider à améliorer le sommeil de ton bébé (0-3 ans).

🔹 **Commandes disponibles :**

📊 /diagnostic - Analyse complète
😴 /siestes - Horaires idéaux selon l'âge
🌙 /coucher - Routine du soir
⏰ /reveil - Décoder un réveil nocturne
🆘 /crise - Protocole d'urgence
🌊 /regression - Situations spéciales
📋 /routine - Routine selon l'âge
💡 /conseil - Conseil du jour
❓ /help - Toutes les commandes

✨ **Version Premium (9,90€/mois) :**
→ Diagnostic illimité
→ Conseils personnalisés
→ Contenu exclusif
→ Support prioritaire

Tape /premium pour en savoir plus !

💪 Prêt(e) à retrouver des nuits paisibles ?
"""
    await update.message.reply_text(welcome_text, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commande /help"""
    help_text = """
📖 **Commandes Coach Sommeil™**

🔍 **Diagnostic**
/diagnostic - Questionnaire guidé

📅 **Horaires & Routines**
/siestes <âge> - Ex: /siestes 6
/routine <âge> - Ex: /routine 8
/coucher - Routine du soir

🌙 **Problèmes nocturnes**
/reveil <heure> - Ex: /reveil 2h30
/crise - Bébé hurle, que faire ?

🌊 **Situations spéciales**
/regression - Dents, régression, etc.

💡 **Conseils**
/conseil - Conseil quotidien

✨ **Premium**
/premium - Infos abonnement
/status - Ton statut

❓ /help - Cette aide
"""
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commande /premium avec Stripe"""
    user_id = update.effective_user.id
    
    if is_premium(user_id):
        user = get_user_data(user_id)
        expiry = user['subscription_until']
        text = f"""
✨ **Tu es abonné(e) Premium !**

📅 Actif jusqu'au : {expiry.strftime('%d/%m/%Y')}

🎁 **Tes avantages :**
✅ Diagnostic illimité
✅ Conseils personnalisés
✅ Contenus exclusifs
✅ Support prioritaire

💚 Merci de ta confiance !
"""
        await update.message.reply_text(text, parse_mode='Markdown')
    else:
        try:
            checkout_session = stripe.checkout.Session.create(
                payment_method_types=['card'],
                line_items=[{'price': STRIPE_PRICE_ID, 'quantity': 1}],
                mode='subscription',
                success_url=f'https://t.me/{context.bot.username}?start=payment_success',
                cancel_url=f'https://t.me/{context.bot.username}?start=payment_cancel',
                client_reference_id=str(user_id),
                metadata={
                    'user_id': str(user_id),
                    'username': update.effective_user.username or 'N/A',
                    'first_name': update.effective_user.first_name or 'N/A',
                },
                allow_promotion_codes=True,
            )
            
            keyboard = [[
                InlineKeyboardButton("✨ S'abonner (9,90€/mois)", url=checkout_session.url)
            ],[
                InlineKeyboardButton("🎯 Test DEMO gratuit", callback_data="activate_premium_demo")
            ]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            text = """
✨ **Coach Sommeil Premium**

💰 **9,90€/mois** - Sans engagement

🎁 **Avantages :**
✅ Diagnostic illimité
✅ Plan personnalisé
✅ Conseils quotidiens adaptés
✅ PDF et tableaux exclusifs
✅ Support dédié

🚀 **Pourquoi Premium ?**
→ Accompagnement sur-mesure
→ Résultats en 2-3 semaines
→ Soutien continu

💳 **Paiement sécurisé Stripe**
→ Résiliable en 1 clic
→ Garantie 14 jours

👇 Clique pour t'abonner :
"""
            await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')
            
        except Exception as e:
            logger.error(f"Erreur Stripe: {e}")
            await update.message.reply_text(
                "❌ Erreur lors de la création du paiement. Réessaie dans quelques instants."
            )

async def premium_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback bouton demo"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "activate_premium_demo":
        activate_premium(query.from_user.id, months=1)
        await query.edit_message_text(
            "🎉 **Premium activé ! (Mode DEMO)**\n\n"
            "Tu as accès à toutes les fonctionnalités premium.\n"
            "Tape /status pour voir ton abonnement.",
            parse_mode='Markdown'
        )

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commande /status"""
    user_id = update.effective_user.id
    user = get_user_data(user_id)
    
    if not user:
        await update.message.reply_text("❌ Tape /start pour t'inscrire.")
        return
    
    if is_premium(user_id):
        expiry = user['subscription_until']
        text = f"""
✅ **Statut : Premium Actif**

📅 Jusqu'au : {expiry.strftime('%d/%m/%Y')}
💚 Toutes les fonctionnalités débloquées !
"""
    else:
        text = """
📊 **Statut : Version Gratuite**

✨ Passe Premium pour :
→ Diagnostic illimité
→ Conseils personnalisés
→ Contenu exclusif

Tape /premium pour en savoir plus !
"""
    
    await update.message.reply_text(text, parse_mode='Markdown')

# =============================================================================
# DIAGNOSTIC
# =============================================================================

async def diagnostic_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔍 **Diagnostic - Étape 1/4**\n\n"
        "Quel est l'âge de ton bébé ? (en mois)\n"
        "Ex: 6, 12, 18...",
        parse_mode='Markdown'
    )
    return AGE

async def diagnostic_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        age = int(update.message.text)
        context.user_data['diagnostic_age'] = age
        await update.message.reply_text(
            "📅 **Diagnostic - Étape 2/4**\n\n"
            "Combien de siestes par jour ?\n"
            "Ex: 2, 3...",
            parse_mode='Markdown'
        )
        return SIESTES
    except ValueError:
        await update.message.reply_text("Merci d'entrer un nombre.")
        return AGE

async def diagnostic_siestes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        siestes = int(update.message.text)
        context.user_data['diagnostic_siestes'] = siestes
        await update.message.reply_text(
            "🌙 **Diagnostic - Étape 3/4**\n\n"
            "Heure du coucher le soir ?\n"
            "Ex: 19h30, 20h...",
            parse_mode='Markdown'
        )
        return COUCHER
    except ValueError:
        await update.message.reply_text("Merci d'entrer un nombre.")
        return SIESTES

async def diagnostic_coucher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['diagnostic_coucher'] = update.message.text
    await update.message.reply_text(
        "😴 **Diagnostic - Étape 4/4**\n\n"
        "Réveils nocturnes (nombre moyen) ?\n"
        "Ex: 0, 2, 5...",
        parse_mode='Markdown'
    )
    return REVEILS

async def diagnostic_reveils(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        reveils = int(update.message.text)
        age = context.user_data['diagnostic_age']
        siestes = context.user_data['diagnostic_siestes']
        coucher = context.user_data['diagnostic_coucher']
        
        result = f"""
✅ **Résultat du Diagnostic**

📋 **Situation :**
- Âge : {age} mois
- Siestes : {siestes}/jour
- Coucher : {coucher}
- Réveils : {reveils}/nuit

🔍 **Analyse :**
"""
        
        siestes_ideal = 4 if age <= 3 else 3 if age <= 6 else 2 if age <= 12 else 1
        
        if siestes > siestes_ideal:
            result += f"\n⚠️ Trop de siestes. Idéal : {siestes_ideal}"
        elif siestes < siestes_ideal:
            result += f"\n💤 Besoin de plus de repos. Idéal : {siestes_ideal}"
        else:
            result += f"\n✅ Nombre de siestes adapté"
        
        if reveils > 3:
            result += "\n\n🌙 Réveils fréquents. Causes possibles :"
            result += "\n• Fenêtre de sommeil inadaptée"
            result += "\n• Coucher trop tardif"
        elif reveils > 0:
            result += "\n\n🌙 Quelques réveils normaux, optimisables"
        else:
            result += "\n\n✨ Excellent ! Bébé dort bien"
        
        result += f"\n\n💡 **Recommandations :**"
        result += f"\n→ /routine {age} - Routine idéale"
        result += f"\n→ /siestes {age} - Horaires optimaux"
        result += "\n→ /coucher - Routine du soir"
        
        if not is_premium(update.effective_user.id):
            result += "\n\n✨ **Premium : diagnostic illimité + plan détaillé**"
            result += "\n→ /premium"
        
        await update.message.reply_text(result, parse_mode='Markdown')
        return ConversationHandler.END
        
    except ValueError:
        await update.message.reply_text("Merci d'entrer un nombre.")
        return REVEILS

async def diagnostic_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Diagnostic annulé. Tape /diagnostic pour recommencer.")
    return ConversationHandler.END

# =============================================================================
# AUTRES COMMANDES
# =============================================================================

async def siestes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage : /siestes <âge>\nEx: /siestes 6")
        return
    
    age = int(context.args[0])
    
    if age <= 3:
        text = "😴 **0-3 mois : 4-5 siestes**\n\nCourtes et fréquentes, toutes les 1h30-2h"
    elif age <= 6:
        text = "😴 **4-6 mois : 3 siestes**\n\nMatin, midi, fin d'après-midi\nFenêtre 2-2h30 entre chaque"
    elif age <= 12:
        text = "😴 **7-12 mois : 2 siestes**\n\n9h-9h30 : Matin (1h-1h30)\n13h-13h30 : Après-midi (1h30-2h)"
    else:
        text = "😴 **12+ mois : 1 sieste**\n\n12h30-13h : Sieste unique (2-3h)"
    
    await update.message.reply_text(text, parse_mode='Markdown')

async def coucher_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """
🌙 **Routine du soir idéale**

18h30 : Repas calme
19h : Bain tiède
19h15 : Pyjama
19h20 : Histoire/berceuse
19h30 : Coucher

💡 Même ordre chaque soir = clé du succès !
"""
    await update.message.reply_text(text, parse_mode='Markdown')

async def reveil_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage : /reveil <heure>\nEx: /reveil 2h30")
        return
    
    text = f"""
⏰ **Réveil à {context.args[0]}**

🔍 **Actions immédiates :**
→ Vérifier couche
→ Rassurer calmement
→ Pas de grande lumière
→ Retour au lit rapide

💡 Si récurrent : ajuste siestes et heure du coucher
"""
    await update.message.reply_text(text, parse_mode='Markdown')

async def crise_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """
🆘 **Protocole Anti-Crise**

✅ **Vérifications (30 sec)**
□ Couche ? Faim ? Froid/chaud ?

✅ **Apaisement**
→ Prends-le contre toi
→ Balancement doux
→ Chuchote "chhhh"

💡 Tu fais de ton mieux ❤️
Les bébés pleurent, ce n'est pas ta faute.
"""
    await update.message.reply_text(text, parse_mode='Markdown')

async def regression_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """
🌊 **Situations spéciales**

🦷 **DENTS** : Douleur = réveils (3-7 jours)
📉 **RÉGRESSION 4 MOIS** : Cycles de sommeil (2-4 sem)
🤒 **MALADIE** : Priorité confort
✈️ **VOYAGE** : Adapter progressivement

💡 Maintiens la routine = repère #1 du bébé
"""
    await update.message.reply_text(text, parse_mode='Markdown')

async def routine_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage : /routine <âge>\nEx: /routine 7")
        return
    
    text = """
📋 **Routine journalière**

7h : Réveil
Siestes adaptées à l'âge
19h30 : Coucher

Utilise /siestes pour les horaires détaillés.
"""
    await update.message.reply_text(text, parse_mode='Markdown')

CONSEILS = [
    "🌙 Bébé qui dort bien = bébé qui mange bien",
    "💡 Régularité > perfection",
    "😴 Bébé trop fatigué = dort moins bien",
    "🌡️ Température idéale : 19-20°C",
    "💤 Endormissement autonome = clé des nuits complètes",
    "📱 Pas d'écran 2h avant le coucher",
    "🎵 Bruit blanc OK mais pas trop fort (50 dB max)",
    "👶 Bébé peut gémir sans se réveiller - attends 5 min",
]

async def conseil_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import random
    await update.message.reply_text(f"💡 **Conseil du jour**\n\n{random.choice(CONSEILS)}")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception: {context.error}")
    if update and update.effective_message:
        await update.effective_message.reply_text("❌ Erreur. Réessaie ou tape /help")
# =============================================================================
# SERVEUR WEBHOOK ET MAIN
# =============================================================================

async def start_webhook_server(app):
    """Démarre le serveur webhook"""
    webapp = web.Application()
    webapp.router.add_post('/webhook/stripe', stripe_webhook)
    
    runner = web.AppRunner(webapp)
    await runner.setup()
    
    port = int(os.environ.get('PORT', 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"🌐 Webhook sur port {port}")

def main():
    """Lance le bot"""
    # Récupérer le token depuis la variable d'environnement
    TOKEN = os.environ.get('TELEGRAM_TOKEN')
    
    if not TOKEN:
        print("❌ ERREUR : TELEGRAM_TOKEN manquant")
        print("Configure la variable d'environnement TELEGRAM_TOKEN")
        exit(1)
    
    if not DATABASE_URL:
       print("❌ ERREUR : DATABASE_URL manquant")
       print("Configure la variable d'environnement DATABASE_URL")
       exit(1)
    
    if not stripe.api_key:
        print("⚠️ STRIPE_SECRET_KEY manquant (paiements désactivés)")
    
    print("📊 Initialisation de la base de données...")
   if not init_database():
       print("❌ Échec de l'initialisation de la base de données")
       exit(1)
    
    # Créer l'application
    application = Application.builder().token(TOKEN).build()
    
    # ConversationHandler pour /diagnostic
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('diagnostic', diagnostic_start)],
        states={
            AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, diagnostic_age)],
            SIESTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, diagnostic_siestes)],
            COUCHER: [MessageHandler(filters.TEXT & ~filters.COMMAND, diagnostic_coucher)],
            REVEILS: [MessageHandler(filters.TEXT & ~filters.COMMAND, diagnostic_reveils)],
        },
        fallbacks=[CommandHandler('cancel', diagnostic_cancel)],
    )
    
    # Ajout de tous les handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("siestes", siestes_command))
    application.add_handler(CommandHandler("coucher", coucher_command))
    application.add_handler(CommandHandler("reveil", reveil_command))
    application.add_handler(CommandHandler("crise", crise_command))
    application.add_handler(CommandHandler("regression", regression_command))
    application.add_handler(CommandHandler("routine", routine_command))
    application.add_handler(CommandHandler("conseil", conseil_command))
    application.add_handler(CommandHandler("premium", premium_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CallbackQueryHandler(premium_callback))
    application.add_error_handler(error_handler)
    
    # Démarrer le serveur webhook
    import asyncio
    loop = asyncio.get_event_loop()
    loop.create_task(start_webhook_server(application))
    
    print("🤖 Bot Coach Sommeil™ démarré avec PostgreSQL + Stripe !")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()