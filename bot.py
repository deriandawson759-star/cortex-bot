#!/usr/bin/env python3
import logging
import os
import asyncio
import json
import re
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
from telegram.error import NetworkError, TimedOut, RetryAfter, Conflict
from groq import Groq, APIConnectionError, APIStatusError, RateLimitError

# ── Optional dependencies ─────────────────────────────────────────────────────

try:
    from duckduckgo_search import DDGS
    WEB_SEARCH_AVAILABLE = True
except ImportError:
    WEB_SEARCH_AVAILABLE = False

try:
    from upstash_redis import Redis as UpstashRedis
    _redis = UpstashRedis(
        url=os.environ.get("UPSTASH_REDIS_URL", ""),
        token=os.environ.get("UPSTASH_REDIS_TOKEN", ""),
    )
    REDIS_AVAILABLE = True
except Exception:
    REDIS_AVAILABLE = False
    _redis = None

try:
    from e2b_code_interpreter import Sandbox as E2BSandbox
    E2B_AVAILABLE = True
except ImportError:
    E2B_AVAILABLE = False

# ── Config ────────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY   = os.environ["GROQ_API_KEY"]
E2B_API_KEY    = os.environ.get("E2B_API_KEY", "")

MAX_HISTORY  = 50
MAX_RETRIES  = 3
RETRY_DELAY  = 2
HISTORY_TTL  = 60 * 60 * 24 * 30  # 30 jours

MODELS = [
    "meta-llama/llama-4-scout-17b-16e-instruct",  # Llama 4 Scout — meilleur dispo
    "qwen/qwen3-32b",                              # Qwen 3 32B — très puissant
    "llama-3.3-70b-versatile",                     # Llama 3.3 70B — fiable
    "llama-3.1-8b-instant",                        # Llama 3.1 — ultra-rapide
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

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)
log.info("Redis: %s | Web search: %s | e2b: %s", REDIS_AVAILABLE, WEB_SEARCH_AVAILABLE, E2B_AVAILABLE)

groq_client = Groq(api_key=GROQ_API_KEY)

# In-memory cache (+ Redis pour la persistence)
conversation_histories: dict[int, list] = {}
user_models: dict[int, int] = {}

# ── Redis helpers ─────────────────────────────────────────────────────────────

def load_history(user_id: int) -> list:
    if user_id in conversation_histories:
        return conversation_histories[user_id]
    if REDIS_AVAILABLE:
        try:
            data = _redis.get(f"cortex:h:{user_id}")
            if data:
                h = json.loads(data) if isinstance(data, str) else data
                conversation_histories[user_id] = h
                return h
        except Exception as e:
            log.warning("Redis load error: %s", e)
    conversation_histories[user_id] = []
    return conversation_histories[user_id]


def save_history(user_id: int):
    if REDIS_AVAILABLE:
        try:
            _redis.setex(f"cortex:h:{user_id}", HISTORY_TTL, json.dumps(conversation_histories[user_id]))
        except Exception as e:
            log.warning("Redis save error: %s", e)


def clear_history(user_id: int):
    conversation_histories[user_id] = []
    if REDIS_AVAILABLE:
        try:
            _redis.delete(f"cortex:h:{user_id}")
        except Exception as e:
            log.warning("Redis delete error: %s", e)


def trim_history(user_id: int):
    h = conversation_histories.get(user_id, [])
    if len(h) > MAX_HISTORY:
        conversation_histories[user_id] = h[-MAX_HISTORY:]
        save_history(user_id)


def get_model(user_id: int) -> str:
    return MODELS[user_models.get(user_id, 0) % len(MODELS)]

# ── Web search ────────────────────────────────────────────────────────────────

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
        return "\n\n".join(f"• {r['title']}\n{r['body']}\n{r['href']}" for r in results)
    except Exception as e:
        log.warning("Web search error: %s", e)
        return ""

# ── e2b code execution ────────────────────────────────────────────────────────

def extract_python_blocks(text: str) -> list[str]:
    return re.findall(r'```(?:python|py)\n(.*?)```', text, re.DOTALL)


async def execute_code(code: str) -> str:
    if not E2B_AVAILABLE or not E2B_API_KEY:
        return "❌ Exécution non disponible."
    try:
        loop = asyncio.get_event_loop()
        def _run():
            with E2BSandbox(api_key=E2B_API_KEY) as sandbox:
                execution = sandbox.run_code(code)
                output = []
                if execution.logs.stdout:
                    output.extend(execution.logs.stdout)
                if execution.logs.stderr:
                    output.append("⚠️ Erreurs:\n" + "\n".join(execution.logs.stderr))
                for result in execution.results:
                    if hasattr(result, "text") and result.text:
                        output.append(result.text)
                return "\n".join(output).strip() if output else "✅ Code exécuté (aucune sortie)"
        return await asyncio.wait_for(loop.run_in_executor(None, _run), timeout=30)
    except asyncio.TimeoutError:
        return "⏱️ Timeout : exécution trop longue (max 30s)"
    except Exception as e:
        return f"❌ Erreur d'exécution: {e}"

# ── Groq ──────────────────────────────────────────────────────────────────────

async def call_groq(messages: list, user_id: int, extra_context: str = "") -> str:
    model = get_model(user_id)
    system = SYSTEM_PROMPT
    if extra_context:
        system += f"\n\n## Résultats de recherche web\n{extra_context}"

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
            # Si le modèle n'est plus disponible → essaie le suivant
            if e.status_code in (400, 404, 503):
                next_idx = (user_models.get(user_id, 0) + 1) % len(MODELS)
                user_models[user_id] = next_idx
                log.warning("Modèle indisponible — switch vers %s", MODELS[next_idx])
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAY)
                    continue
            raise

    raise RuntimeError("Groq: toutes les tentatives ont échoué")

# ── Telegram helpers ──────────────────────────────────────────────────────────

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

# ── Commands ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    clear_history(user_id)
    mem = "✅ activée (Redis)" if REDIS_AVAILABLE else "❌ désactivée"
    e2b = "✅ activée" if E2B_AVAILABLE and E2B_API_KEY else "❌ désactivée"
    await update.message.reply_text(
        "👋 *Cortex — Assistant IA Expert*\n\n"
        "Propulsé par LLaMA 4 Scout & Qwen 3 via Groq.\n\n"
        f"🧠 Mémoire persistante : {mem}\n"
        f"⚙️ Exécution de code : {e2b}\n\n"
        "*Commandes :*\n"
        "`/web` _question_ — recherche internet\n"
        "`/run` — exécute du code Python\n"
        "`/model` — changer le modèle IA\n"
        "`/clear` — effacer l'historique\n"
        "`/help` — aide complète",
        parse_mode="Markdown",
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_history(update.effective_user.id)
    await update.message.reply_text("✅ Historique effacé (Redis inclus). Nouvelle conversation !")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Cortex — Aide*\n\n"
        "`/start` — redémarrer\n"
        "`/clear` — effacer l'historique\n"
        "`/web` _question_ — recherche web + réponse IA\n"
        "`/run` — exécuter du code Python (réponds à un message)\n"
        "`/model` — voir/changer le modèle IA\n"
        "`/help` — cette aide\n\n"
        "💡 *Astuces :*\n"
        "• Je me souviens de tout même après redémarrage\n"
        "• `/run` en répondant à mon message pour tester le code\n"
        "• `/web` pour les infos récentes\n"
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
                await update.message.reply_text(f"✅ Modèle : `{MODELS[idx]}`", parse_mode="Markdown")
            else:
                await update.message.reply_text(f"❌ Choix : 1 à {len(MODELS)}")
        except ValueError:
            await update.message.reply_text("Usage : `/model 2`", parse_mode="Markdown")
    else:
        lines = [f"{i+1}. `{m}`{'  ← actuel' if m == current else ''}" for i, m in enumerate(MODELS)]
        await update.message.reply_text(
            "*Modèles :*\n" + "\n".join(lines) + "\n\nChanger : `/model 2`",
            parse_mode="Markdown",
        )


async def cmd_web(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    query = " ".join(context.args) if context.args else None
    if not query:
        await update.message.reply_text("Usage : `/web votre question`", parse_mode="Markdown")
        return

    stop_event = asyncio.Event()
    asyncio.create_task(keep_typing(context, update.effective_chat.id, stop_event))
    try:
        await update.message.reply_text(f"🔍 Recherche : *{query}*", parse_mode="Markdown")
        web_results = await web_search(query)
        history = load_history(user_id)
        history.append({"role": "user", "content": query})
        trim_history(user_id)
        reply = await call_groq(conversation_histories[user_id], user_id, extra_context=web_results)
        history.append({"role": "assistant", "content": reply})
        save_history(user_id)
        await send_long(update, reply)
    except Exception as e:
        log.error("cmd_web error: %s", e, exc_info=True)
        await update.message.reply_text("❌ Erreur lors de la recherche.")
    finally:
        stop_event.set()


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = None
    if context.args:
        code = " ".join(context.args)
    elif update.message.reply_to_message and update.message.reply_to_message.text:
        blocks = extract_python_blocks(update.message.reply_to_message.text)
        code = blocks[0] if blocks else update.message.reply_to_message.text

    if not code:
        await update.message.reply_text(
            "Usage : réponds à un message contenant du code Python avec `/run`\n"
            "Ou : `/run print('hello')`",
            parse_mode="Markdown",
        )
        return

    stop_event = asyncio.Event()
    asyncio.create_task(keep_typing(context, update.effective_chat.id, stop_event))
    try:
        await update.message.reply_text("⚙️ Exécution en cours dans le cloud...")
        result = await execute_code(code)
        await update.message.reply_text(f"```\n{result}\n```", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Erreur: {e}")
    finally:
        stop_event.set()

# ── Main handler ──────────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id   = update.effective_user.id
    user_name = update.effective_user.first_name or "User"
    text      = update.message.text.strip()
    if not text:
        return

    log.info("Message de %s (%d): %.80s", user_name, user_id, text)

    history = load_history(user_id)
    history.append({"role": "user", "content": text})
    trim_history(user_id)

    stop_event = asyncio.Event()
    asyncio.create_task(keep_typing(context, update.effective_chat.id, stop_event))
    try:
        reply = await call_groq(conversation_histories[user_id], user_id)
        history.append({"role": "assistant", "content": reply})
        save_history(user_id)
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
        log.warning("Erreur réseau (transitoire): %s", err)
        await asyncio.sleep(2)
    elif isinstance(err, RetryAfter):
        log.warning("Rate limit Telegram — attente %ds", err.retry_after)
        await asyncio.sleep(err.retry_after)
    elif isinstance(err, Conflict):
        # 409 : deux instances tournent (ex: redéploiement Railway)
        # On attend et Railway tuera l'ancienne instance automatiquement
        log.warning("409 Conflict — autre instance détectée, pause 15s...")
        await asyncio.sleep(15)
    else:
        log.error("Erreur non gérée: %s", err, exc_info=context.error)

# ── Main ──────────────────────────────────────────────────────────────────────

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
    app.add_handler(CommandHandler("run",   cmd_run))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    log.info("Cortex prêt — polling Telegram")
    app.run_polling(drop_pending_updates=True, allowed_updates=["message"])


if __name__ == "__main__":
    main()
