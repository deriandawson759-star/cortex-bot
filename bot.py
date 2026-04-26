#!/usr/bin/env python3
import logging
import os
import asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
from telegram.error import NetworkError, TimedOut, RetryAfter
from groq import Groq, APIConnectionError, APIStatusError, RateLimitError

try:
    from duckduckgo_search import DDGS
    WEB_SEARCH_AVAILABLE = True
except ImportError:
    WEB_SEARCH_AVAILABLE = False

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY   = os.environ["GROQ_API_KEY"]

MAX_HISTORY  = 50
MAX_RETRIES  = 3
RETRY_DELAY  = 2

MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
]

SYSTEM_PROMPT = """Tu es **Cortex**, un assistant IA de niveau expert senior avec 20+ ans d'expérience tech.

## Expertise
**Langages**: Python, JavaScript/TypeScript, Go, Rust, C/C++, Java, SQL, Bash
**Frontend**: React, Next.js, Vue.js, Tailwind CSS, HTML/CSS avancé, animations
**Backend**: FastAPI, Node.js, Django, Express, NestJS, microservices, REST/GraphQL
**DevOps**: Docker, Kubernetes, CI/CD, GitHub Actions, nginx, Terraform, Ansible
**Cloud**: AWS, GCP, Azure, Railway, Vercel, Fly.io, Cloudflare Workers
**Bases de données**: PostgreSQL, MySQL, MongoDB, Redis, Supabase, Prisma, vector DBs
**IA/ML**: LangChain, OpenAI/Groq API, RAG, embeddings, agents, fine-tuning
**Sécurité**: OWASP Top 10, JWT, OAuth2, audit de code, cryptographie
**Architecture**: Clean Architecture, DDD, CQRS, Event Sourcing, design patterns
**Git**: workflows avancés, rebase interactif, hooks, monorepos, résolution de conflits

## Style de réponse
- **Français par défaut**, dans la langue de l'utilisateur sinon
- **Direct et concret** : chaque mot compte, zéro remplissage
- **Code complet et fonctionnel** avec les bons blocs ```langage
- Explique le **pourquoi**, pas juste le comment
- Pour les **bugs** : identifie la cause racine, propose la solution la plus propre
- Pour l'**architecture** : pense scalabilité, maintenabilité, performance, coût
- Si plusieurs approches : présente les trade-offs clairement
- Tu critiques le code de façon **honnête et constructive**
- Question ambiguë → pose **une seule question précise** avant de répondre

## Personnalité
- Tu penses comme un **tech lead senior** : pragmatique, exigeant, pédagogue
- Tu n'hésites pas à dire qu'une approche est mauvaise, avec justification
- Tu proposes toujours la **meilleure solution**, pas la plus simple
- Tu mentionnes les pièges et edge cases importants
- Tu donnes des exemples réels et testables"""

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

groq_client = Groq(api_key=GROQ_API_KEY)
conversation_histories: dict[int, list] = {}
user_models: dict[int, int] = {}


def get_history(user_id: int) -> list:
    return conversation_histories.setdefault(user_id, [])


def trim_history(user_id: int):
    h = conversation_histories.get(user_id, [])
    if len(h) > MAX_HISTORY:
        conversation_histories[user_id] = h[-MAX_HISTORY:]


def get_model(user_id: int) -> str:
    return MODELS[user_models.get(user_id, 0) % len(MODELS)]


async def web_search(query: str, max_results: int = 5) -> str:
    if not WEB_SEARCH_AVAILABLE:
        return ""
    try:
        loop = asyncio.get_event_loop()
        def _search():
            with DDGS() as ddgs:
                return list(ddgs.text(query, max_results=max_results))
        results = await loop.run_in_executor(None, _search)
        if not results:
            return "Aucun résultat trouvé."
        parts = [f"• {r['title']}\n{r['body']}\n{r['href']}" for r in results]
        return "\n\n".join(parts)
    except Exception as e:
        log.warning("Web search error: %s", e)
        return ""


async def call_groq(messages: list, user_id: int, extra_context: str = "") -> str:
    model = get_model(user_id)
    system = SYSTEM_PROMPT
    if extra_context:
        system += f"\n\n## Résultats de recherche web (utilise ces infos)\n{extra_context}"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = groq_client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system}] + messages,
                max_tokens=4096,
                temperature=0.7,
                timeout=60,
            )
            return resp.choices[0].message.content

        except RateLimitError:
            next_idx = (user_models.get(user_id, 0) + 1) % len(MODELS)
            user_models[user_id] = next_idx
            log.warning("Rate limit — switch vers %s", MODELS[next_idx])
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY * attempt)
            else:
                raise

        except APIConnectionError as e:
            log.warning("Connexion error (tentative %d/%d): %s", attempt, MAX_RETRIES, e)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY)
            else:
                raise

        except APIStatusError as e:
            log.error("Groq API error %s: %s", e.status_code, e.message)
            raise

    raise RuntimeError("Groq: toutes les tentatives ont échoué")


async def send_long(update: Update, text: str):
    if len(text) <= 4000:
        try:
            await update.message.reply_text(text, parse_mode="Markdown")
        except Exception:
            await update.message.reply_text(text)
        return

    chunks = []
    while text:
        if len(text) <= 4000:
            chunks.append(text)
            break
        split_at = text.rfind('\n\n', 0, 4000)
        if split_at == -1:
            split_at = text.rfind('\n', 0, 4000)
        if split_at == -1:
            split_at = 4000
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip()

    for chunk in chunks:
        try:
            await update.message.reply_text(chunk, parse_mode="Markdown")
        except Exception:
            await update.message.reply_text(chunk)


async def keep_typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int, stop_event: asyncio.Event):
    while not stop_event.is_set():
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:
            pass
        await asyncio.sleep(4)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conversation_histories[user_id] = []
    await update.message.reply_text(
        "👋 *Cortex — Assistant IA Expert*\n\n"
        "Propulsé par LLaMA 3.3 70B via Groq.\n\n"
        "*Ce que je fais :*\n"
        "• Code & debugging tous langages\n"
        "• Architecture & design patterns\n"
        "• Docker, DevOps, CI/CD\n"
        "• Bases de données & APIs\n"
        "• Recherche web en temps réel\n"
        "• Sécurité & bonnes pratiques\n\n"
        "*Commandes :*\n"
        "`/web` _question_ — recherche sur internet\n"
        "`/model` — changer de modèle IA\n"
        "`/clear` — effacer l'historique\n"
        "`/help` — aide complète",
        parse_mode="Markdown",
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conversation_histories[update.effective_user.id] = []
    await update.message.reply_text("✅ Historique effacé. Nouvelle conversation !")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Cortex — Aide*\n\n"
        "`/start` — redémarrer et réinitialiser\n"
        "`/clear` — effacer l'historique de conversation\n"
        "`/web` _question_ — recherche web + réponse IA\n"
        "`/model` — voir et changer le modèle IA\n"
        "`/help` — cette aide\n\n"
        "💡 *Astuces :*\n"
        "• Je retiens toute la conversation (50 messages)\n"
        "• Envoie du code directement, je l'analyse\n"
        "• `/web` pour les infos récentes ou actualités\n"
        "• `/clear` si le contexte devient confus",
        parse_mode="Markdown",
    )


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current = get_model(user_id)

    if context.args:
        try:
            idx = int(context.args[0]) - 1
            if 0 <= idx < len(MODELS):
                user_models[user_id] = idx
                await update.message.reply_text(
                    f"✅ Modèle changé : `{MODELS[idx]}`", parse_mode="Markdown"
                )
            else:
                await update.message.reply_text(f"❌ Numéro invalide. Choix : 1 à {len(MODELS)}")
        except ValueError:
            await update.message.reply_text("Usage : `/model 2`", parse_mode="Markdown")
    else:
        lines = []
        for i, m in enumerate(MODELS):
            marker = "  ← actuel" if m == current else ""
            lines.append(f"{i+1}. `{m}`{marker}")
        model_list = "\n".join(lines)
        await update.message.reply_text(
            f"*Modèles disponibles :*\n{model_list}\n\nChanger : `/model 2`",
            parse_mode="Markdown",
        )


async def cmd_web(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    query = " ".join(context.args) if context.args else None

    if not query:
        await update.message.reply_text(
            "Usage : `/web votre question`\nExemple : `/web dernière version de Node.js`",
            parse_mode="Markdown",
        )
        return

    stop_event = asyncio.Event()
    asyncio.create_task(keep_typing(context, update.effective_chat.id, stop_event))

    try:
        await update.message.reply_text(f"🔍 Recherche en cours : *{query}*", parse_mode="Markdown")
        web_results = await web_search(query)

        history = get_history(user_id)
        history.append({"role": "user", "content": query})
        trim_history(user_id)

        reply = await call_groq(conversation_histories[user_id], user_id, extra_context=web_results or "")
        history.append({"role": "assistant", "content": reply})
        await send_long(update, reply)
        log.info("Web reply à %d — %d chars", user_id, len(reply))

    except Exception as e:
        log.error("cmd_web error pour %d: %s", user_id, e, exc_info=True)
        await update.message.reply_text("❌ Erreur lors de la recherche. Réessaie.")
    finally:
        stop_event.set()


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id   = update.effective_user.id
    user_name = update.effective_user.first_name or "User"
    text      = update.message.text.strip()

    if not text:
        return

    log.info("Message de %s (%d): %.80s", user_name, user_id, text)

    history = get_history(user_id)
    history.append({"role": "user", "content": text})
    trim_history(user_id)

    stop_event = asyncio.Event()
    asyncio.create_task(keep_typing(context, update.effective_chat.id, stop_event))

    try:
        reply = await call_groq(conversation_histories[user_id], user_id)
        history.append({"role": "assistant", "content": reply})
        await send_long(update, reply)
        log.info("Réponse à %s (%d) — %d chars", user_name, user_id, len(reply))

    except RateLimitError:
        await update.message.reply_text("⚠️ Limite atteinte. Réessaie dans 30 secondes.")
        history.pop()

    except (APIConnectionError, APIStatusError) as e:
        log.error("Groq error pour %d: %s", user_id, e)
        await update.message.reply_text("❌ Erreur IA. Réessaie dans quelques secondes.")
        history.pop()

    except Exception as e:
        log.error("Erreur inattendue pour %d: %s", user_id, e, exc_info=True)
        await update.message.reply_text("❌ Erreur inattendue. Tape /clear puis réessaie.")
        history.pop()

    finally:
        stop_event.set()


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        log.warning("Erreur réseau Telegram: %s", err)
    elif isinstance(err, RetryAfter):
        log.warning("Telegram rate limit — attente %ds", err.retry_after)
        await asyncio.sleep(err.retry_after)
    else:
        log.error("Erreur non gérée: %s", err, exc_info=context.error)


def main():
    log.info("Démarrage Cortex (modèle: %s)", MODELS[0])

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
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("web",   cmd_web))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    log.info("Cortex prêt — polling Telegram")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message"],
    )


if __name__ == "__main__":
    main()
