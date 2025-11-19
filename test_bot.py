import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commande /start"""
    await update.message.reply_text(
        "🎉 **Bot Coach Sommeil™ fonctionne !**\n\n"
        "Tu viens de créer ton premier bot Telegram !\n\n"
        "✅ Python installé\n"
        "✅ Bibliothèques installées\n"
        "✅ Bot fonctionnel\n\n"
        "Prochaine étape : déploiement sur Render ! 🚀",
        parse_mode='Markdown'
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Je suis un bot de test ! Tape /start")

def main():
    # REMPLACE PAR TON TOKEN ICI
    TOKEN = "REMPLACE_PAR_TON_TOKEN"
    
    print("🤖 Démarrage du bot de test...")
    
    application = Application.builder().token(TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    
    print("✅ Bot démarré ! Appuie sur Ctrl+C pour arrêter")
    application.run_polling()

if __name__ == '__main__':
    main()