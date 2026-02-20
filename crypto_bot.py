import os
import asyncio
import logging
import json
import time
from datetime import datetime
from typing import Optional
from aiohttp import web
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from dotenv import load_dotenv

load_dotenv()

# ─── 配置 ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
DEEPSEEK_API_KEY  = os.getenv("DEEPSEEK_API_KEY", "")
WHALE_ALERT_KEY   = os.getenv("WHALE_ALERT_API_KEY", "")
COINGECKO_KEY     = os.getenv("COINGECKO_API_KEY", "")

# Render 自动注入此变量，格式如 https://your-app.onrender.com
RENDER_URL        = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT              = int(os.getenv("PORT", 8080))

DEEPSEEK_BASE_URL  = "https://api.deepseek.com"
COINGECKO_BASE     = "https://api.coingecko.com/api/v3"
WHALE_ALERT_BASE   = "https://api.whale-alert.io/v1"
CRYPTOCOMPARE_BASE = "https://min-api.cryptocompare.com/data"

# ── 交易所直连 API（无需注册，真正实时，无严格速率限制）───────────────────────
BINANCE_BASE = "https://api.binance.com/api/v3"
OKX_BASE     = "https://www.okx.com/api/v5/market"

# CoinGecko ID → Binance symbol 映射
COINGECKO_TO_BINANCE: dict[str, str] = {
    "bitcoin": "BTCUSDT", "ethereum": "ETHUSDT", "solana": "SOLUSDT",
    "binancecoin": "BNBUSDT", "ripple": "XRPUSDT", "cardano": "ADAUSDT",
    "dogecoin": "DOGEUSDT", "the-open-network": "TONUSDT", "polkadot": "DOTUSDT",
    "avalanche-2": "AVAXUSDT", "chainlink": "LINKUSDT", "uniswap": "UNIUSDT",
    "litecoin": "LTCUSDT", "shiba-inu": "SHIBUSDT", "sui": "SUIUSDT",
    "tron": "TRXUSDT", "pepe": "PEPEUSDT", "aptos": "APTUSDT",
    "arbitrum": "ARBUSDT", "optimism": "OPUSDT",
}

# CoinGecko ID → OKX instId 映射（Binance 失败时备用）
COINGECKO_TO_OKX: dict[str, str] = {
    "bitcoin": "BTC-USDT", "ethereum": "ETH-USDT", "solana": "SOL-USDT",
    "binancecoin": "BNB-USDT", "ripple": "XRP-USDT", "cardano": "ADA-USDT",
    "dogecoin": "DOGE-USDT", "the-open-network": "TON-USDT", "polkadot": "DOT-USDT",
    "avalanche-2": "AVAX-USDT", "chainlink": "LINK-USDT", "uniswap": "UNI-USDT",
    "litecoin": "LTC-USDT", "shiba-inu": "SHIB-USDT", "sui": "SUI-USDT",
    "tron": "TRX-USDT", "pepe": "PEPE-USDT", "aptos": "APT-USDT",
    "arbitrum": "ARB-USDT", "optimism": "OP-USDT",
}

# ─── 请求头：强制禁用缓存（关键修复）────────────────────────────────────────
NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma":        "no-cache",
    "Expires":       "0",
    "User-Agent":    "CryptoSageBot/2.0",
}

# 对话历史 (per user)
user_conversations: dict[int, list] = {}

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# ─── 工具：带缓存破坏的 HTTP 请求 ────────────────────────────────────────────

async def fetch_json(
    url: str,
    params: dict = None,
    headers: dict = None,
    timeout: int = 10
) -> Optional[dict]:
    """
    通用 HTTP GET，强制加时间戳参数防止任何层级缓存。
    """
    _params = params.copy() if params else {}
    # 时间戳参数让每次请求 URL 都不同，彻底破坏 CDN/代理缓存
    _params["_t"] = int(time.time())

    _headers = {**NO_CACHE_HEADERS, **(headers or {})}
    if COINGECKO_KEY:
        _headers["x-cg-demo-api-key"] = COINGECKO_KEY

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                params=_params,
                headers=_headers,
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                if resp.status == 200:
                    return await resp.json(content_type=None)
                elif resp.status == 429:
                    logger.warning(f"Rate limited: {url}")
                    return None
                else:
                    logger.error(f"HTTP {resp.status}: {url}")
    except asyncio.TimeoutError:
        logger.error(f"Timeout: {url}")
    except Exception as e:
        logger.error(f"Fetch error {url}: {e}")
    return None


# ─── 数据获取函数 ─────────────────────────────────────────────────────────────

async def _price_from_binance(coin_id: str) -> Optional[dict]:
    """
    从 Binance 获取单个币种实时价格。
    返回统一格式 {usd, usd_24h_change, usd_24h_vol} 供上层复用。
    """
    symbol = COINGECKO_TO_BINANCE.get(coin_id)
    if not symbol:
        return None
    # ticker/24hr 包含现价、24h涨跌幅、24h成交量，一次请求全拿
    data = await fetch_json(f"{BINANCE_BASE}/ticker/24hr", params={"symbol": symbol})
    if not data:
        return None
    price = float(data.get("lastPrice", 0))
    change_pct = float(data.get("priceChangePercent", 0))
    vol_usdt = float(data.get("quoteVolume", 0))   # USDT 计价成交量
    # CNY 汇率用固定近似值（~7.25），避免再多一次 API 请求
    CNY_RATE = 7.25
    return {
        "usd":            price,
        "cny":            price * CNY_RATE,
        "usd_24h_change": change_pct,
        "usd_24h_vol":    vol_usdt,
        "usd_market_cap": 0,         # Binance 不提供市值，留给 CoinGecko 补充
        "_source":        "Binance",
    }


async def _price_from_okx(coin_id: str) -> Optional[dict]:
    """OKX 备用价格源，格式与 Binance 层统一。"""
    inst_id = COINGECKO_TO_OKX.get(coin_id)
    if not inst_id:
        return None
    data = await fetch_json(f"{OKX_BASE}/ticker", params={"instId": inst_id})
    if not data or data.get("code") != "0":
        return None
    d = data["data"][0]
    price = float(d.get("last", 0))
    open24 = float(d.get("open24h", price) or price)
    change_pct = ((price - open24) / open24 * 100) if open24 else 0
    vol_usdt = float(d.get("volCcy24h", 0))
    CNY_RATE = 7.25
    return {
        "usd":            price,
        "cny":            price * CNY_RATE,
        "usd_24h_change": change_pct,
        "usd_24h_vol":    vol_usdt,
        "usd_market_cap": 0,
        "_source":        "OKX",
    }


async def _price_from_coingecko(coin_ids: str) -> Optional[dict]:
    """CoinGecko 兜底，提供市值等 Binance 缺失的数据。"""
    return await fetch_json(
        f"{COINGECKO_BASE}/simple/price",
        params={
            "ids": coin_ids,
            "vs_currencies": "usd,cny",
            "include_24hr_change": "true",
            "include_market_cap":  "true",
            "include_24hr_vol":    "true",
            "precision":           "full",
        }
    )


async def get_price(coin_ids: str) -> Optional[dict]:
    """
    三层兜底价格查询：Binance → OKX → CoinGecko
    返回格式与原 CoinGecko 格式兼容，key 为 coin_id。
    """
    ids = [c.strip() for c in coin_ids.split(",") if c.strip()]
    result = {}

    # 并发向 Binance 查所有币种
    binance_tasks = [_price_from_binance(cid) for cid in ids]
    binance_results = await asyncio.gather(*binance_tasks, return_exceptions=True)

    need_okx = []
    need_cg  = []

    for cid, br in zip(ids, binance_results):
        if isinstance(br, dict) and br.get("usd"):
            result[cid] = br
            logger.info(f"Price source: Binance → {cid} = ${br['usd']:,.2f}")
        else:
            need_okx.append(cid)

    # OKX 补救
    if need_okx:
        okx_tasks = [_price_from_okx(cid) for cid in need_okx]
        okx_results = await asyncio.gather(*okx_tasks, return_exceptions=True)
        for cid, okr in zip(need_okx, okx_results):
            if isinstance(okr, dict) and okr.get("usd"):
                result[cid] = okr
                logger.info(f"Price source: OKX → {cid} = ${okr['usd']:,.2f}")
            else:
                need_cg.append(cid)

    # CoinGecko 最后兜底
    if need_cg:
        cg_data = await _price_from_coingecko(",".join(need_cg))
        if cg_data:
            for cid in need_cg:
                if cid in cg_data:
                    cg_data[cid]["_source"] = "CoinGecko"
                    result[cid] = cg_data[cid]
                    logger.info(f"Price source: CoinGecko → {cid}")

    return result if result else None


async def get_market_overview(limit: int = 10) -> Optional[list]:
    return await fetch_json(
        f"{COINGECKO_BASE}/coins/markets",
        params={
            "vs_currency":             "usd",
            "order":                   "market_cap_desc",
            "per_page":                limit,
            "page":                    1,
            "sparkline":               "false",
            "price_change_percentage": "1h,24h,7d",
        }
    )


async def get_trending() -> Optional[dict]:
    return await fetch_json(f"{COINGECKO_BASE}/search/trending")


async def get_fear_greed() -> Optional[dict]:
    return await fetch_json("https://api.alternative.me/fng/?limit=1")


async def get_crypto_news(limit: int = 5) -> Optional[list]:
    data = await fetch_json(
        f"{CRYPTOCOMPARE_BASE}/v2/news/",
        params={"lang": "EN", "sortOrder": "latest", "limit": limit}
    )
    return data.get("Data") if data else None


async def get_whale_transactions(min_value: int = 1_000_000) -> Optional[dict]:
    if not WHALE_ALERT_KEY:
        return {"status": "no_key", "transactions": []}
    return await fetch_json(
        f"{WHALE_ALERT_BASE}/transactions",
        params={"api_key": WHALE_ALERT_KEY, "min_value": min_value, "limit": 10},
    )


async def get_global_stats() -> Optional[dict]:
    """获取全球加密市场总市值、BTC 占比等"""
    return await fetch_json(f"{COINGECKO_BASE}/global")


# ─── DeepSeek AI ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """你是专业加密货币市场分析师「CryptoSage」。

能力：分析实时价格趋势、解读巨鲸动向、市场情绪分析、技术面解读。
风格：专业简洁，适当用 emoji，数据要引用具体数字，结论有依据。
限制：回复≤500字；投资建议必须附免责声明。
数据：用户消息中若有[实时市场数据]标记，优先基于该数据分析。"""


async def chat_with_deepseek(user_id: int, user_message: str, context_data: str = "") -> str:
    if user_id not in user_conversations:
        user_conversations[user_id] = []

    full_message = (
        f"[实时市场数据 {datetime.utcnow().strftime('%H:%M UTC')}]\n{context_data}\n\n"
        f"[用户问题]\n{user_message}"
        if context_data else user_message
    )

    user_conversations[user_id].append({"role": "user", "content": full_message})
    if len(user_conversations[user_id]) > 20:
        user_conversations[user_id] = user_conversations[user_id][-20:]

    payload = {
        "model":       "deepseek-chat",
        "messages":    [{"role": "system", "content": SYSTEM_PROMPT}, *user_conversations[user_id]],
        "temperature": 0.7,
        "max_tokens":  800,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{DEEPSEEK_BASE_URL}/v1/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    msg = result["choices"][0]["message"]["content"]
                    user_conversations[user_id].append({"role": "assistant", "content": msg})
                    return msg
                logger.error(f"DeepSeek {resp.status}: {await resp.text()}")
                return "⚠️ AI 服务暂时不可用，请稍后再试。"
    except asyncio.TimeoutError:
        return "⏱️ AI 响应超时，请稍后再试。"
    except Exception as e:
        logger.error(f"DeepSeek error: {e}")
        return "❌ 连接 AI 失败，请检查网络。"


# ─── 格式化函数 ───────────────────────────────────────────────────────────────

def format_price_message(coin_id: str, data: dict) -> str:
    if coin_id not in data:
        return f"❌ 未找到 `{coin_id}`（请使用 CoinGecko ID）"
    d = data[coin_id]
    price_usd  = d.get("usd", 0)
    price_cny  = d.get("cny", 0)
    change_24h = d.get("usd_24h_change") or 0
    vol_24h    = d.get("usd_24h_vol") or 0
    mktcap     = d.get("usd_market_cap") or 0
    source     = d.get("_source", "?")
    emoji = "🟢" if change_24h >= 0 else "🔴"
    arrow = "▲" if change_24h >= 0 else "▼"
    mktcap_str = f"`${mktcap:,.0f}`" if mktcap else "`N/A`"
    return (
        f"💰 *{coin_id.upper()}*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💵 `${price_usd:,.6g}`  🇨🇳 `¥{price_cny:,.2f}`\n"
        f"{emoji} 24h: `{arrow}{abs(change_24h):.2f}%`\n"
        f"📊 成交量: `${vol_24h:,.0f}`\n"
        f"🏦 市值: {mktcap_str}\n"
        f"🔌 数据源: `{source}`\n"
        f"🕐 `{datetime.utcnow().strftime('%H:%M:%S')} UTC`"
    )


def format_market_overview(coins: list) -> str:
    lines = [f"📊 *市场 Top {len(coins)}*  `{datetime.utcnow().strftime('%H:%M UTC')}`\n━━━━━━━━━━━━━━━"]
    for i, c in enumerate(coins, 1):
        ch24 = c.get("price_change_percentage_24h") or 0
        e = "🟢" if ch24 >= 0 else "🔴"
        lines.append(
            f"`{i:2}.` *{c['symbol'].upper()}* {e}`{ch24:+.2f}%`\n"
            f"     `${c['current_price']:,.4g}` | 市值`${c['market_cap']/1e9:.1f}B`"
        )
    return "\n".join(lines)


def format_trending(data: dict) -> str:
    if not data or "coins" not in data:
        return "❌ 无法获取趋势数据"
    lines = ["🔥 *CoinGecko 热搜榜*\n━━━━━━━━━━━━━━━"]
    for i, item in enumerate(data["coins"][:7], 1):
        c = item["item"]
        lines.append(f"`{i}.` *{c['name']}* (`{c['symbol']}`)\n   市值排名 #{c.get('market_cap_rank','N/A')}")
    return "\n".join(lines)


def format_fear_greed(data: dict) -> str:
    if not data or "data" not in data:
        return "❌ 无法获取恐惧贪婪指数"
    d = data["data"][0]
    v = int(d["value"])
    label = d["value_classification"]
    emoji = "😱" if v < 25 else "😰" if v < 45 else "😐" if v < 55 else "😊" if v < 75 else "🤑"
    bar = "█" * (v // 10) + "░" * (10 - v // 10)
    return (
        f"{emoji} *恐惧贪婪指数*\n━━━━━━━━━━━━━━━\n"
        f"数值 `{v}/100` | 状态 `{label}`\n"
        f"`[{bar}]`"
    )


def format_news(news_list: list) -> str:
    if not news_list:
        return "❌ 无法获取新闻"
    lines = ["📰 *最新加密资讯*\n━━━━━━━━━━━━━━━"]
    for n in news_list[:5]:
        title  = (n.get("title") or "")[:60]
        source = n.get("source", "")
        ts     = datetime.fromtimestamp(n.get("published_on", 0)).strftime("%m/%d %H:%M")
        url    = n.get("url", "")
        lines.append(f"• [{title}...]({url})\n  📌 {source} `{ts}`")
    return "\n\n".join(lines)


# ─── Telegram 命令 ────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("📊 市场概览", callback_data="market"),
         InlineKeyboardButton("🔥 热门趋势", callback_data="trending")],
        [InlineKeyboardButton("😱 恐惧贪婪", callback_data="feargreed"),
         InlineKeyboardButton("📰 最新资讯", callback_data="news")],
        [InlineKeyboardButton("🐋 巨鲸动向", callback_data="whale"),
         InlineKeyboardButton("🌐 全球数据", callback_data="global")],
    ]
    await update.message.reply_text(
        "🤖 *CryptoSage v2* — 实时加密市场助手\n\n"
        "直接发消息 → AI 自动注入实时数据分析\n"
        "或点击按钮快速查询：",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )


async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "用法: `/price bitcoin` 或 `/price bitcoin ethereum solana`",
            parse_mode="Markdown"
        )
        return
    coin_ids = ",".join([a.lower() for a in context.args])
    msg = await update.message.reply_text("⏳ 实时查询中...")
    data = await get_price(coin_ids)
    if not data:
        await msg.edit_text("❌ 查询失败，请检查币种 ID")
        return
    results = [format_price_message(c, data) for c in coin_ids.split(",")]
    kb = [[InlineKeyboardButton("🔄 刷新", callback_data=f"rprice_{coin_ids}")]]
    await msg.edit_text("\n\n".join(results), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))


async def cmd_market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ 加载中...")
    coins = await get_market_overview(10)
    if not coins:
        await msg.edit_text("❌ 无法获取市场数据")
        return
    kb = [[InlineKeyboardButton("🔄 刷新", callback_data="market")]]
    await msg.edit_text(format_market_overview(coins), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))


async def cmd_whale(update: Update, context: ContextTypes.DEFAULT_TYPE):
    min_val = int(context.args[0]) if context.args else 1_000_000
    msg = await update.message.reply_text("🐋 查询中...")
    data = await get_whale_transactions(min_val)

    if not data or data.get("status") == "no_key":
        ai = await chat_with_deepseek(
            update.effective_user.id,
            "请分析当前加密货币市场巨鲸资金动向特征，并给出链上信号解读。"
        )
        await msg.edit_text(
            f"🐋 *巨鲸动向 AI 分析*\n━━━━━━━━━━━━━━━\n{ai}\n\n"
            "💡 配置 `WHALE_ALERT_API_KEY` 可获取实时巨鲸转账数据",
            parse_mode="Markdown"
        )
        return

    txs = data.get("transactions", [])
    if not txs:
        await msg.edit_text("⚠️ 最近无符合条件的大额转账")
        return

    lines = [f"🐋 *巨鲸动向 (>=${min_val:,})*\n━━━━━━━━━━━━━━━"]
    for tx in txs[:8]:
        amt   = tx.get("amount", 0)
        sym   = tx.get("symbol", "").upper()
        usd   = tx.get("amount_usd", 0)
        frm   = tx.get("from", {}).get("owner", "匿名")
        to    = tx.get("to",   {}).get("owner", "匿名")
        lines.append(f"💸 `{amt:,.0f} {sym}` ≈ `${usd:,.0f}`\n   {frm} ➜ {to}")

    await msg.edit_text("\n\n".join(lines), parse_mode="Markdown")


async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("📰 获取资讯...")
    news = await get_crypto_news(5)
    await msg.edit_text(format_news(news), parse_mode="Markdown", disable_web_page_preview=True)


async def cmd_fear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ 查询中...")
    data = await get_fear_greed()
    await msg.edit_text(format_fear_greed(data), parse_mode="Markdown")


async def cmd_trending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔥 获取热搜榜...")
    data = await get_trending()
    await msg.edit_text(format_trending(data), parse_mode="Markdown")


async def cmd_global(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🌐 获取全球数据...")
    data = await get_global_stats()
    if not data or "data" not in data:
        await msg.edit_text("❌ 无法获取全球数据")
        return
    d = data["data"]
    mktcap = d.get("total_market_cap", {}).get("usd", 0)
    vol    = d.get("total_volume", {}).get("usd", 0)
    btc_d  = d.get("market_cap_percentage", {}).get("btc", 0)
    eth_d  = d.get("market_cap_percentage", {}).get("eth", 0)
    await msg.edit_text(
        f"🌐 *全球市场数据*\n━━━━━━━━━━━━━━━\n"
        f"💰 总市值: `${mktcap/1e12:.2f}T`\n"
        f"📊 24h 成交量: `${vol/1e9:.1f}B`\n"
        f"₿ BTC 占比: `{btc_d:.1f}%`\n"
        f"Ξ ETH 占比: `{eth_d:.1f}%`\n"
        f"🕐 `{datetime.utcnow().strftime('%H:%M:%S UTC')}`",
        parse_mode="Markdown"
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_conversations.pop(update.effective_user.id, None)
    await update.message.reply_text("✅ 对话历史已清除")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *命令列表*\n━━━━━━━━━━━━━━━\n"
        "`/price <ID...>` 实时价格\n"
        "`/market` Top10 市场\n"
        "`/whale [最低金额]` 巨鲸动向\n"
        "`/news` 最新资讯\n"
        "`/fear` 恐惧贪婪指数\n"
        "`/trending` 热搜榜\n"
        "`/global` 全球市场数据\n"
        "`/clear` 清除 AI 对话历史\n\n"
        "💬 直接发消息 → AI + 实时数据\n"
        "🔗 币种ID查询: coingecko.com",
        parse_mode="Markdown"
    )


# ─── AI 消息处理 ──────────────────────────────────────────────────────────────

COIN_ALIAS = {
    "btc":"bitcoin","比特币":"bitcoin",
    "eth":"ethereum","以太坊":"ethereum","以太":"ethereum",
    "sol":"solana","索拉纳":"solana",
    "bnb":"binancecoin",
    "xrp":"ripple","瑞波":"ripple",
    "ada":"cardano",
    "doge":"dogecoin","狗狗币":"dogecoin",
    "ton":"the-open-network",
    "dot":"polkadot","波卡":"polkadot",
    "avax":"avalanche-2",
    "link":"chainlink",
    "uni":"uniswap",
    "ltc":"litecoin","莱特币":"litecoin",
    "shib":"shiba-inu",
    "sui":"sui",
    "trx":"tron","波场":"tron",
    "pepe":"pepe",
    "apt":"aptos",
    "arb":"arbitrum",
    "op":"optimism",
}

PRICE_KW  = ["价格","多少","price","行情","最新","现在","涨","跌","点位"]
MARKET_KW = ["市场","大盘","market","趋势","概况"]
FEAR_KW   = ["恐惧","贪婪","情绪","fear","greed","指数"]


def detect_coins(text: str) -> list[str]:
    t = text.lower()
    return list(dict.fromkeys(v for k, v in COIN_ALIAS.items() if k in t))[:4]


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text or ""
    await context.bot.send_chat_action(update.effective_chat.id, "typing")

    ctx_parts = []

    # 检测币种 → 并发获取价格
    coins = detect_coins(text)
    if coins or any(k in text for k in PRICE_KW):
        targets = coins or ["bitcoin", "ethereum"]
        price_data = await get_price(",".join(targets))
        if price_data:
            ctx_parts.append("实时价格: " + json.dumps(price_data, ensure_ascii=False))

    # 市场数据
    if any(k in text for k in MARKET_KW):
        md = await get_market_overview(5)
        if md:
            ctx_parts.append("Top5市场: " + " | ".join(
                f"{c['symbol'].upper()} ${c['current_price']:,.2f}({c['price_change_percentage_24h']:+.2f}%)"
                for c in md
            ))

    # 恐惧贪婪
    if any(k in text for k in FEAR_KW):
        fg = await get_fear_greed()
        if fg and "data" in fg:
            d = fg["data"][0]
            ctx_parts.append(f"恐惧贪婪: {d['value']}/100 ({d['value_classification']})")

    response = await chat_with_deepseek(user_id, text, "\n".join(ctx_parts))
    await update.message.reply_text(response, parse_mode="Markdown")


# ─── 按钮回调 ─────────────────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = q.data

    if d == "market":
        coins = await get_market_overview(10)
        if coins:
            kb = [[InlineKeyboardButton("🔄 刷新", callback_data="market")]]
            await q.edit_message_text(format_market_overview(coins), parse_mode="Markdown",
                                      reply_markup=InlineKeyboardMarkup(kb))
    elif d == "trending":
        await q.edit_message_text(format_trending(await get_trending()), parse_mode="Markdown")
    elif d == "feargreed":
        await q.edit_message_text(format_fear_greed(await get_fear_greed()), parse_mode="Markdown")
    elif d == "news":
        await q.edit_message_text(format_news(await get_crypto_news(5)),
                                  parse_mode="Markdown", disable_web_page_preview=True)
    elif d == "global":
        data = await get_global_stats()
        if data and "data" in data:
            dd = data["data"]
            mktcap = dd.get("total_market_cap", {}).get("usd", 0)
            vol    = dd.get("total_volume", {}).get("usd", 0)
            btc_d  = dd.get("market_cap_percentage", {}).get("btc", 0)
            await q.edit_message_text(
                f"🌐 *全球市场*\n💰 总市值 `${mktcap/1e12:.2f}T`\n"
                f"📊 24h量 `${vol/1e9:.1f}B`\n₿ BTC占比 `{btc_d:.1f}%`",
                parse_mode="Markdown"
            )
    elif d == "whale":
        data = await get_whale_transactions()
        if not WHALE_ALERT_KEY or data.get("status") == "no_key":
            ai = await chat_with_deepseek(q.from_user.id, "分析当前加密市场巨鲸资金动向。")
            await q.edit_message_text(f"🐋 *AI巨鲸分析*\n━━━━━━━━━━━━━━━\n{ai}", parse_mode="Markdown")
    elif d.startswith("rprice_"):
        coin_ids = d[7:]
        data = await get_price(coin_ids)
        if data:
            results = [format_price_message(c, data) for c in coin_ids.split(",")]
            kb = [[InlineKeyboardButton("🔄 刷新", callback_data=f"rprice_{coin_ids}")]]
            await q.edit_message_text("\n\n".join(results), parse_mode="Markdown",
                                      reply_markup=InlineKeyboardMarkup(kb))


# ─── HTTP 服务（Render 必须监听端口）+ 自 ping 防休眠 ─────────────────────────

async def health_handler(request):
    return web.Response(text="OK", status=200)


async def webhook_handler(request, application):
    """接收 Telegram Webhook 请求"""
    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
    except Exception as e:
        logger.error(f"Webhook error: {e}")
    return web.Response(status=200)


async def self_ping_task():
    """
    每 10 分钟 ping 自身健康接口，防止 Render 免费版休眠。
    Render 免费版 15 分钟无请求就会休眠，所以间隔要 < 14 分钟。
    """
    if not RENDER_URL:
        logger.info("RENDER_EXTERNAL_URL 未设置，跳过自 ping")
        return
    url = f"{RENDER_URL}/health"
    logger.info(f"自 ping 启动，目标: {url}")
    while True:
        await asyncio.sleep(600)  # 10 分钟
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    logger.info(f"Self-ping: {r.status}")
        except Exception as e:
            logger.warning(f"Self-ping failed: {e}")


# ─── 主程序 ───────────────────────────────────────────────────────────────────

async def main():
    if not TELEGRAM_TOKEN:
        raise ValueError("❌ TELEGRAM_BOT_TOKEN 未设置")
    if not DEEPSEEK_API_KEY:
        raise ValueError("❌ DEEPSEEK_API_KEY 未设置")

    print("🚀 CryptoSage v3 启动（三层实时价格）...")

    # 构建 Application（不启动 polling）
    application = Application.builder().token(TELEGRAM_TOKEN).updater(None).build()

    # 注册命令
    for cmd, fn in [
        ("start",    cmd_start),
        ("help",     cmd_help),
        ("price",    cmd_price),
        ("market",   cmd_market),
        ("whale",    cmd_whale),
        ("news",     cmd_news),
        ("fear",     cmd_fear),
        ("trending", cmd_trending),
        ("global",   cmd_global),
        ("clear",    cmd_clear),
    ]:
        application.add_handler(CommandHandler(cmd, fn))

    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await application.initialize()
    await application.start()

    # 设置 Webhook
    if RENDER_URL:
        webhook_url = f"{RENDER_URL}/webhook/{TELEGRAM_TOKEN}"
        await application.bot.set_webhook(
            url=webhook_url,
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True      # 丢弃积压消息，避免收到旧数据
        )
        logger.info(f"✅ Webhook 已设置: {webhook_url}")
    else:
        logger.warning("⚠️ RENDER_EXTERNAL_URL 未设置，Webhook 未注册（本地测试模式）")

    # 构建 aiohttp Web 服务
    app_web = web.Application()
    app_web.router.add_get("/health", health_handler)
    app_web.router.add_post(
        f"/webhook/{TELEGRAM_TOKEN}",
        lambda req: webhook_handler(req, application)
    )

    runner = web.AppRunner(app_web)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"✅ HTTP 服务已启动，端口 {PORT}")

    # 启动自 ping 防休眠
    asyncio.create_task(self_ping_task())

    logger.info("🎯 Bot 运行中（Webhook 模式）...")
    # 保持运行
    try:
        await asyncio.Event().wait()
    finally:
        await application.stop()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
