#!/usr/bin/env python3
import logging
import os
import asyncio
from logging.handlers import RotatingFileHandler
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
from telegram.error import NetworkError, TimedOut, RetryAfter
from groq import Groq, APIConnectionError, APIStatusError, RateLimitError

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY   = os.environ["GROQ_API_KEY"]
MAX_HISTORY    = 30
MAX_RETRIES    = 3
RETRY_DELAY    = 2

SYSTEM_PROMPT = (
    "Tu es un assistant développeur expert et professionnel. "
    "Tu réponds en français par défaut sauf si on te parle dans une autre langue. "
    "Tu aides avec : code (Python, JavaScript, TypeScript, Go, Rust, etc.), debugging, "
    "architecture logicielle, DevOps, Docker, git, bases de données, APIs, sécurité, "
    "et tout sujet tech. Tu donnes des réponses précises, directes et concrètes. "
    "Tu utilises des blocs de code avec la syntaxe Markdown quand c'est utile. "
    "Si une question est ambiguë, tu demandes une précision avant de répondre."
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

groq_client = Groq(api_key=GROQ_API_KEY)
conversation_histories: dict[int, list] = {}


def get_history(user_id: int) -> list:
    return conversation_histories.setdefault(user_id, [])


def trim_history(user_id: int):
    h = conversation_histories.get(user_id, [])
    if len(h) > MAX_HISTORY:
        conversation_histories[user_id] = h[-MAX_HISTORY:]


async def call_groq(messages: list) -> str:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "system", "content": SYSTEM_PROMPT}] + messages,
                max_tokens=2048,
                temperature=0.7,
                timeout=30,
            )
            return resp.choices[0].message.content

        except RateLimitError:
            wait = RETRY_DELAY * attempt
            log.warning("Groq rate limit — attente %ds (tentative %d/%d)", wait, attempt, MAX_RETRIES)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(wait)
            else:
                raise

        except APIConnectionError as e:
            log.warning("Groq connexion error (tentative %d/%d): %s", attempt, MAX_RETRIES, e)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY)
            else:
                raise

        except APIStatusError as e:
            log.error("Groq API error %s: %s", e.status_code, e.message)
            raise

    raise RuntimeError("Groq: toutes les tentatives ont échoué")


async def send_long(update: Update, text: str):
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i+4000])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conversation_histories[user_id] = []
    await update.message.reply_text(
        "👋 *Assistant Développeur IA* — propulsé par Groq + LLaMA 3.3 70B\n\n"
        "Pose-moi n'importe quelle question technique :\n"
        "• Code & debugging\n"
        "• Architecture & design patterns\n"
        "• Docker / DevOps / CI-CD\n"
        "• Bases de données & APIs\n"
        "• Git & workflows\n\n"
        "📌 Commandes : /clear — effacer l'historique | /help — aide",
        parse_mode="Markdown",
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conversation_histories[update.effective_user.id] = []
    await update.message.reply_text("✅ Historique effacé. Nouvelle conversation !")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Commandes disponibles*\n\n"
        "/start — redémarrer\n"
        "/clear — effacer l'historique de conversation\n"
        "/help  — afficher cette aide\n\n"
        "💡 Je retiens le contexte de la conversation. "
        "Si je semble perdu, tape /clear pour repartir de zéro.",
        parse_mode="Markdown",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id   = update.effective_user.id
    user_name = update.effective_user.first_name or "User"
    text      = update.message.text.strip()

    if not text:
        return

    log.info("Message reçu de %s (%d): %.80s", user_name, user_id, text)

    history = get_history(user_id)
    history.append({"role": "user", "content": text})
    trim_history(user_id)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        reply = await call_groq(conversation_histories[user_id])
        history.append({"role": "assistant", "content": reply})
        await send_long(update, reply)
        log.info("Réponse envoyée à %s (%d) — %d chars", user_name, user_id, len(reply))

    except RateLimitError:
        await update.message.reply_text(
            "⚠️ Limite de requêtes atteinte. Réessaie dans 30 secondes."
        )
        history.pop()

    except (APIConnectionError, APIStatusError) as e:
        log.error("Groq error pour %d: %s", user_id, e)
        await update.message.reply_text(
            "❌ Erreur de connexion à l'IA. Réessaie dans quelques secondes."
        )
        history.pop()

    except Exception as e:
        log.error("Erreur inattendue pour %d: %s", user_id, e, exc_info=True)
        await update.message.reply_text(
            "❌ Erreur inattendue. Tape /clear puis réessaie."
        )
        history.pop()


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        log.warning("Erreur réseau Telegram (auto-récupération): %s", err)
    elif isinstance(err, RetryAfter):
        log.warning("Telegram rate limit — attente %ds", err.retry_after)
        await asyncio.sleep(err.retry_after)
    else:
        log.error("Erreur non gérée: %s", err, exc_info=context.error)


def main():
    log.info("Démarrage du bot (Groq llama-3.3-70b-versatile)")

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .connect_timeout(10)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(10)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    log.info("Bot prêt — polling Telegram en cours")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message"],
    )


if __name__ == "__main__":
    main()
