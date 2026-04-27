#!/usr/bin/env python3
"""
Cortex — Bot Telegram IA Expert
Stack  : python-telegram-bot 21 · AsyncGroq · Upstash Redis · e2b · DuckDuckGo
Mode   : POLLING (deleteWebhook automatique au démarrage)
"""
import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import httpx

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.error import Conflict, NetworkError, RetryAfter, TimedOut
from groq import APIConnectionError, APIStatusError, AsyncGroq, RateLimitError

# ── Dépendances optionnelles ───────────────────────────────────────────────────

try:
    from duckduckgo_search import DDGS
    WEB_SEARCH_AVAILABLE = True
except ImportError:
    WEB_SEARCH_AVAILABLE = False

try:
    _UPSTASH_URL   = os.environ.get("UPSTASH_REDIS_URL", "").strip()
    _UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_TOKEN", "").strip()
    if not (_UPSTASH_URL and _UPSTASH_TOKEN):
        raise ValueError("Credentials Upstash manquantes")
    from upstash_redis import Redis as UpstashRedis
    _redis = UpstashRedis(url=_UPSTASH_URL, token=_UPSTASH_TOKEN)
    REDIS_AVAILABLE = True
except Exception:
    REDIS_AVAILABLE = False
    _redis = None

try:
    from e2b_code_interpreter import Sandbox as E2BSandbox
    E2B_AVAILABLE = True
except ImportError:
    E2B_AVAILABLE = False

# ── Configuration ─────────────────────────────────────────────────────────────

TELEGRAM_TOKEN      = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY        = os.environ["GROQ_API_KEY"]
E2B_API_KEY         = os.environ.get("E2B_API_KEY", "")
FLOWISE_URL         = os.environ.get("FLOWISE_URL", "").rstrip("/")
FLOWISE_CHATFLOW_ID = os.environ.get("FLOWISE_CHATFLOW_ID", "")
N8N_WEBHOOK_URL     = os.environ.get("N8N_WEBHOOK_URL", "")
FLOWISE_AVAILABLE   = bool(FLOWISE_URL and FLOWISE_CHATFLOW_ID)
N8N_AVAILABLE       = bool(N8N_WEBHOOK_URL)

MAX_HISTORY      = 40
MAX_MSG_LEN      = 4000
MAX_SEARCH_CHARS = 3000
MAX_RETRIES      = 3
RETRY_DELAY      = 2
HISTORY_TTL      = 60 * 60 * 24 * 30   # 30 jours
RATE_LIMIT_SEC   = 1.5

MODELS = [
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "qwen/qwen3-32b",
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

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ── État global ───────────────────────────────────────────────────────────────

groq_client:            AsyncGroq            = AsyncGroq(api_key=GROQ_API_KEY)
conversation_histories: dict[int, list]      = {}
user_models:            dict[int, int]       = {}
user_last_msg:          dict[int, float]     = {}
bot_start_time:         float                = time.time()

# ── Redis helpers ─────────────────────────────────────────────────────────────

def _r_get(key: str):
    return _redis.get(key) if _redis else None

def _r_set(key: str, ttl: int, val: str):
    if _redis:
        _redis.setex(key, ttl, val)

def _r_del(key: str):
    if _redis:
        _redis.delete(key)


async def load_history(user_id: int) -> list:
    if user_id in conversation_histories:
        return conversation_histories[user_id]
    if REDIS_AVAILABLE:
        try:
            raw = await asyncio.to_thread(_r_get, f"cortex:h:{user_id}")
            if raw is not None:
                if isinstance(raw, (list, dict)):
                    h = raw
                else:
                    h = json.loads(raw)
                if isinstance(h, list):
                    conversation_histories[user_id] = h
                    return h
        except Exception as e:
            log.warning("Redis load error: %s", e)
    conversation_histories[user_id] = []
    return conversation_histories[user_id]


async def save_history(user_id: int) -> None:
    if not REDIS_AVAILABLE:
        return
    try:
        payload = json.dumps(conversation_histories[user_id], ensure_ascii=False)
        await asyncio.to_thread(_r_set, f"cortex:h:{user_id}", HISTORY_TTL, payload)
    except Exception as e:
        log.warning("Redis save error: %s", e)


async def clear_history(user_id: int) -> None:
    conversation_histories[user_id] = []
    if not REDIS_AVAILABLE:
        return
    try:
        await asyncio.to_thread(_r_del, f"cortex:h:{user_id}")
    except Exception as e:
        log.warning("Redis delete error: %s", e)


def trim_history(user_id: int) -> None:
    h = conversation_histories.get(user_id, [])
    if len(h) > MAX_HISTORY:
        conversation_histories[user_id] = h[-MAX_HISTORY:]


def get_model(user_id: int) -> str:
    return MODELS[user_models.get(user_id, 0) % len(MODELS)]

# ── Recherche web ─────────────────────────────────────────────────────────────

async def web_search(query: str, max_results: int = 5) -> str:
    if not WEB_SEARCH_AVAILABLE:
        return ""
    try:
        def _search():
            with DDGS() as ddgs:
                return list(ddgs.text(query, max_results=max_results))
        results = await asyncio.get_running_loop().run_in_executor(None, _search)
        if not results:
            return "Aucun résultat trouvé."
        out = "\n\n".join(
            f"• {r['title']}\n{r['body']}\n{r['href']}" for r in results
        )
        return out[:MAX_SEARCH_CHARS]
    except Exception as e:
        log.warning("Web search error: %s", e)
        return ""

# ── Exécution de code (e2b) ───────────────────────────────────────────────────

def _extract_python(text: str) -> list[str]:
    return re.findall(r'```(?:python|py)\n(.*?)```', text, re.DOTALL)


async def execute_code(code: str) -> str:
    if not (E2B_AVAILABLE and E2B_API_KEY):
        return "❌ Exécution non disponible (E2B non configuré)."
    try:
        def _run():
            with E2BSandbox(api_key=E2B_API_KEY) as sb:
                ex = sb.run_code(code)
                out = []
                if ex.logs.stdout:
                    out.extend(ex.logs.stdout)
                if ex.logs.stderr:
                    out.append("⚠️ Erreurs:\n" + "\n".join(ex.logs.stderr))
                for r in ex.results:
                    if hasattr(r, "text") and r.text:
                        out.append(r.text)
                return "\n".join(out).strip() or "✅ Code exécuté (aucune sortie)"
        return await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, _run),
            timeout=30,
        )
    except asyncio.TimeoutError:
        return "⏱️ Timeout — exécution trop longue (max 30s)"
    except Exception as e:
        return f"❌ Erreur d'exécution : {e}"

# ── Groq ──────────────────────────────────────────────────────────────────────

async def call_groq(messages: list, user_id: int, extra_context: str = "") -> str:
    system = SYSTEM_PROMPT + (
        f"\n\n## Contexte\nDate : {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M')} UTC"
    )
    if extra_context:
        system += f"\n\n## Résultats de recherche web\n{extra_context}"

    for attempt in range(1, MAX_RETRIES + 1):
        model = get_model(user_id)
        try:
            resp = await groq_client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system}] + messages,
                max_tokens=4096,
                temperature=0.7,
                timeout=60,
            )
            return resp.choices[0].message.content

        except RateLimitError:
            user_models[user_id] = (user_models.get(user_id, 0) + 1) % len(MODELS)
            log.warning("Rate limit %s → switch vers %s", model, get_model(user_id))
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY * attempt)
            else:
                raise

        except APIConnectionError as e:
            log.warning("Connexion Groq (tentative %d/%d): %s", attempt, MAX_RETRIES, e)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY)
            else:
                raise

        except APIStatusError as e:
            log.error("Groq API %s: %s", e.status_code, e.message)
            if e.status_code in (400, 404, 503):
                user_models[user_id] = (user_models.get(user_id, 0) + 1) % len(MODELS)
                log.warning("Modèle %s indisponible → %s", model, get_model(user_id))
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAY)
                    continue
            raise

    raise RuntimeError("Groq : toutes les tentatives ont échoué")

# ── Flowise ───────────────────────────────────────────────────────────────────

async def call_flowise(question: str, session_id: str) -> str:
    url = f"{FLOWISE_URL}/api/v1/prediction/{FLOWISE_CHATFLOW_ID}"
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, json={"question": question, "sessionId": session_id})
            resp.raise_for_status()
            data = resp.json()
            return data.get("text") or data.get("answer") or str(data)
    except httpx.TimeoutException:
        return "⏱️ Flowise timeout — l'agent met trop de temps à répondre."
    except Exception as e:
        log.error("Flowise error: %s", e)
        return f"❌ Erreur Flowise : {e}"


# ── n8n ───────────────────────────────────────────────────────────────────────

async def trigger_n8n(user_id: int, user_name: str, task: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(N8N_WEBHOOK_URL, json={
                "user_id": user_id,
                "user_name": user_name,
                "task": task,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            resp.raise_for_status()
            try:
                data = resp.json()
                return data.get("message") or data.get("result") or "✅ Automation déclenchée."
            except Exception:
                return "✅ Automation déclenchée."
    except httpx.TimeoutException:
        return "⏱️ n8n timeout — le workflow prend trop de temps."
    except Exception as e:
        log.error("n8n error: %s", e)
        return f"❌ Erreur n8n : {e}"


# ── Utilitaires Telegram ──────────────────────────────────────────────────────

async def send_long(update: Update, text: str) -> None:
    """Envoie un texte long en découpant par blocs de 4000 chars."""
    chunks: list[str] = []
    while text:
        if len(text) <= 4000:
            chunks.append(text)
            break
        split = text.rfind('\n\n', 0, 4000)
        if split == -1:
            split = text.rfind('\n', 0, 4000)
        if split == -1:
            split = 4000
        chunks.append(text[:split])
        text = text[split:].lstrip()

    for chunk in chunks:
        try:
            await update.message.reply_text(chunk, parse_mode="Markdown")
        except Exception:
            await update.message.reply_text(chunk)


async def keep_typing(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    stop: asyncio.Event,
) -> None:
    """Envoie 'typing...' toutes les 4s jusqu'à ce que stop soit levé."""
    while not stop.is_set():
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            pass

# ── Commandes ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    await clear_history(user_id)
    await update.message.reply_text(
        "👋 *Cortex — Assistant IA Expert*\n\n"
        f"🤖 Modèle : `{get_model(user_id)}`\n"
        f"🧠 Mémoire : {'✅ Redis' if REDIS_AVAILABLE else '⚠️ locale'}\n"
        f"⚙️ Exécution code : {'✅ active' if E2B_AVAILABLE and E2B_API_KEY else '❌ off'}\n"
        f"🔗 Flowise : {'✅ connecté' if FLOWISE_AVAILABLE else '❌ off'}\n"
        f"⚡ n8n : {'✅ connecté' if N8N_AVAILABLE else '❌ off'}\n\n"
        "*Commandes :*\n"
        "`/expert` _question_ — agent IA Flowise\n"
        "`/auto` _tâche_ — déclencher n8n\n"
        "`/web` _question_ — recherche internet\n"
        "`/run` — exécute du code Python\n"
        "`/model` — changer le modèle IA\n"
        "`/status` — état du bot\n"
        "`/clear` — effacer l'historique\n"
        "`/help` — aide complète",
        parse_mode="Markdown",
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await clear_history(update.effective_user.id)
    await update.message.reply_text("✅ Historique effacé. Nouvelle conversation !")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤖 *Cortex — Aide*\n\n"
        "`/start` — redémarrer\n"
        "`/clear` — effacer l'historique\n"
        "`/web` _question_ — recherche web + réponse IA\n"
        "`/run` — exécuter du code Python\n"
        "`/model` — voir/changer le modèle IA\n"
        "`/status` — santé du bot en temps réel\n"
        "`/help` — cette aide\n\n"
        "💡 *Astuces :*\n"
        "• Mémoire persistante même après redémarrage\n"
        "• `/run` en répondant à un message de code\n"
        "• `/web` pour les infos récentes\n"
        "• `/clear` si le contexte devient confus",
        parse_mode="Markdown",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id  = update.effective_user.id
    elapsed  = int(time.time() - bot_start_time)
    h, m, s  = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60
    hist_len = len(conversation_histories.get(user_id, []))
    await update.message.reply_text(
        "📊 *Cortex — Statut*\n\n"
        f"🟢 Uptime : `{h:02d}h {m:02d}m {s:02d}s`\n"
        f"🤖 Modèle : `{get_model(user_id)}`\n"
        f"🧠 Redis : {'✅ connecté' if REDIS_AVAILABLE else '❌ off'}\n"
        f"🌐 Web search : {'✅' if WEB_SEARCH_AVAILABLE else '❌'}\n"
        f"⚙️ Code exec : {'✅' if E2B_AVAILABLE and E2B_API_KEY else '❌'}\n"
        f"🔗 Flowise : {'✅ connecté' if FLOWISE_AVAILABLE else '❌ off'}\n"
        f"⚡ n8n : {'✅ connecté' if N8N_AVAILABLE else '❌ off'}\n"
        f"💬 Mémoire : `{hist_len}/{MAX_HISTORY}` messages\n"
        f"📅 `{datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M')} UTC`",
        parse_mode="Markdown",
    )


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    current = get_model(user_id)
    if context.args:
        try:
            idx = int(context.args[0]) - 1
            if 0 <= idx < len(MODELS):
                user_models[user_id] = idx
                await update.message.reply_text(
                    f"✅ Modèle changé :\n`{MODELS[idx]}`",
                    parse_mode="Markdown",
                )
            else:
                await update.message.reply_text(f"❌ Choix entre 1 et {len(MODELS)}")
        except ValueError:
            await update.message.reply_text("Usage : `/model 2`", parse_mode="Markdown")
    else:
        lines = [
            f"{i + 1}. `{m}`{'  ← actuel' if m == current else ''}"
            for i, m in enumerate(MODELS)
        ]
        await update.message.reply_text(
            "*Modèles disponibles :*\n" + "\n".join(lines) + "\n\nChanger : `/model 2`",
            parse_mode="Markdown",
        )


async def cmd_expert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not FLOWISE_AVAILABLE:
        await update.message.reply_text("❌ Flowise non configuré (FLOWISE_URL / FLOWISE_CHATFLOW_ID manquants).")
        return
    user_id  = update.effective_user.id
    question = " ".join(context.args).strip() if context.args else ""
    if not question:
        await update.message.reply_text("Usage : `/expert votre question`", parse_mode="Markdown")
        return

    stop   = asyncio.Event()
    typing = asyncio.create_task(keep_typing(context, update.effective_chat.id, stop))
    try:
        await update.message.reply_text("🧠 Agent Flowise en cours...", parse_mode="Markdown")
        reply = await call_flowise(question, session_id=str(user_id))
        await send_long(update, reply)
    except Exception as e:
        log.error("cmd_expert error: %s", e)
        await update.message.reply_text(f"❌ Erreur : {e}")
    finally:
        stop.set()
        typing.cancel()


async def cmd_auto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not N8N_AVAILABLE:
        await update.message.reply_text("❌ n8n non configuré (N8N_WEBHOOK_URL manquant).")
        return
    user    = update.effective_user
    task    = " ".join(context.args).strip() if context.args else ""
    if not task:
        await update.message.reply_text("Usage : `/auto votre tâche à automatiser`", parse_mode="Markdown")
        return

    stop   = asyncio.Event()
    typing = asyncio.create_task(keep_typing(context, update.effective_chat.id, stop))
    try:
        await update.message.reply_text("⚡ Déclenchement n8n...", parse_mode="Markdown")
        result = await trigger_n8n(user.id, user.first_name or "User", task)
        await send_long(update, result)
    except Exception as e:
        log.error("cmd_auto error: %s", e)
        await update.message.reply_text(f"❌ Erreur : {e}")
    finally:
        stop.set()
        typing.cancel()


async def cmd_web(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    query   = " ".join(context.args).strip() if context.args else ""
    if not query:
        await update.message.reply_text("Usage : `/web votre question`", parse_mode="Markdown")
        return

    stop   = asyncio.Event()
    typing = asyncio.create_task(keep_typing(context, update.effective_chat.id, stop))
    try:
        await update.message.reply_text(f"🔍 Recherche : *{query}*", parse_mode="Markdown")
        web_ctx  = await web_search(query)
        history  = await load_history(user_id)
        history.append({"role": "user", "content": query})
        trim_history(user_id)
        reply = await call_groq(conversation_histories[user_id], user_id, extra_context=web_ctx)
        history.append({"role": "assistant", "content": reply})
        await save_history(user_id)
        await send_long(update, reply)
    except Exception as e:
        log.error("cmd_web error: %s", e, exc_info=True)
        await update.message.reply_text("❌ Erreur lors de la recherche.")
    finally:
        stop.set()
        typing.cancel()


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    code: str | None = None
    if context.args:
        code = " ".join(context.args)
    elif update.message.reply_to_message and update.message.reply_to_message.text:
        blocks = _extract_python(update.message.reply_to_message.text)
        code   = blocks[0] if blocks else update.message.reply_to_message.text
    if not code:
        await update.message.reply_text(
            "Usage : réponds à un message de code avec `/run`\n"
            "Ou : `/run print('hello')`",
            parse_mode="Markdown",
        )
        return

    stop   = asyncio.Event()
    typing = asyncio.create_task(keep_typing(context, update.effective_chat.id, stop))
    try:
        await update.message.reply_text("⚙️ Exécution en cours dans le cloud...")
        result = await execute_code(code)
        await update.message.reply_text(f"```\n{result}\n```", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Erreur : {e}")
    finally:
        stop.set()
        typing.cancel()

# ── Handler principal ─────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id   = update.effective_user.id
    user_name = update.effective_user.first_name or "User"
    text      = (update.message.text or "").strip()
    if not text:
        return

    # Rate limiting
    now  = time.time()
    last = user_last_msg.get(user_id, 0.0)
    if now - last < RATE_LIMIT_SEC:
        await update.message.reply_text("⏳ Un instant...")
        return
    user_last_msg[user_id] = now

    # Limite taille
    if len(text) > MAX_MSG_LEN:
        text = text[:MAX_MSG_LEN]
        await update.message.reply_text(f"⚠️ Message tronqué à {MAX_MSG_LEN} caractères.")

    log.info("Message de %s (%d) : %.80s", user_name, user_id, text)

    history = await load_history(user_id)
    history.append({"role": "user", "content": text})
    trim_history(user_id)

    stop   = asyncio.Event()
    typing = asyncio.create_task(keep_typing(context, update.effective_chat.id, stop))
    try:
        reply = await call_groq(conversation_histories[user_id], user_id)
        history.append({"role": "assistant", "content": reply})
        await save_history(user_id)
        await send_long(update, reply)
        log.info(
            "Réponse à %s (%d) — %d chars | modèle : %s",
            user_name, user_id, len(reply), get_model(user_id),
        )
    except RateLimitError:
        await update.message.reply_text("⚠️ Quota Groq atteint. Réessaie dans 30 secondes.")
        if history:
            history.pop()
    except (APIConnectionError, APIStatusError) as e:
        log.error("Groq error (%d) : %s", user_id, e)
        await update.message.reply_text("❌ Erreur IA temporaire. Réessaie dans quelques secondes.")
        if history:
            history.pop()
    except Exception as e:
        log.error("Erreur inattendue (%d) : %s", user_id, e, exc_info=True)
        await update.message.reply_text("❌ Erreur inattendue. Tape /clear puis réessaie.")
        if history:
            history.pop()
    finally:
        stop.set()
        typing.cancel()

# ── Error handler ─────────────────────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        log.warning("Erreur réseau (transitoire) : %s", err)
        await asyncio.sleep(3)
    elif isinstance(err, RetryAfter):
        log.warning("Rate limit Telegram — pause %ds", err.retry_after)
        await asyncio.sleep(err.retry_after)
    elif isinstance(err, Conflict):
        # Pendant le rolling deploy Railway : l'ancienne instance est encore en vie.
        # On attend 30s — Railway la tue via SIGTERM dans ce délai.
        log.warning("409 Conflict — ancienne instance active, pause 30s...")
        await asyncio.sleep(30)
    else:
        log.error("Erreur non gérée : %s", err, exc_info=True)

# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("═" * 60)
    log.info(
        "Cortex démarrage | Redis:%s | WebSearch:%s | E2B:%s",
        REDIS_AVAILABLE, WEB_SEARCH_AVAILABLE, E2B_AVAILABLE,
    )
    log.info("Modèle principal : %s", MODELS[0])
    log.info("═" * 60)

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .connect_timeout(10)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(10)
        .build()
    )

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("clear",  cmd_clear))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("model",  cmd_model))
    app.add_handler(CommandHandler("web",    cmd_web))
    app.add_handler(CommandHandler("run",    cmd_run))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("expert", cmd_expert))
    app.add_handler(CommandHandler("auto",   cmd_auto))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    # Mode POLLING — simple, fiable, aucun webhook à gérer.
    # run_polling() appelle deleteWebhook automatiquement au démarrage.
    # En cas de 409 Conflict (rolling deploy Railway), l'error handler
    # attend 30s que l'ancienne instance soit tuée, puis reprend.
    log.info("Mode POLLING actif")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message"],
        timeout=10,
    )


if __name__ == "__main__":
    main()
