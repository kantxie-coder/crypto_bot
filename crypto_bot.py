import os
import asyncio
import logging
import json
from datetime import datetime
from typing import Optional
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from dotenv import load_dotenv

load_dotenv()

# ─── 配置 ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "8598007801:AAEclZ2Zzd25t2zR3O1QGwAWfRR5p5t4t1I")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "sk-027fdaa728a64d379f42917c62ff9697")
WHALE_ALERT_KEY  = os.getenv("WHALE_ALERT_API_KEY", "") 
COINGECKO_KEY    = os.getenv("COINGECKO_API_KEY", "")    

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
COINGECKO_BASE    = "https://api.coingecko.com/api/v3"
WHALE_ALERT_BASE  = "https://api.whale-alert.io/v1"
CRYPTOCOMPARE_BASE= "https://min-api.cryptocompare.com/data"

# 对话历史 (per user)
user_conversations: dict[int, list] = {}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# ─── API 工具函数 ─────────────────────────────────────────────────────────────

async def fetch_json(url: str, params: dict = None, headers: dict = None) -> Optional[dict]:
    """通用 HTTP GET JSON 请求"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return await resp.json()
    except Exception as e:
        logger.error(f"Fetch error {url}: {e}")
    return None


async def get_price(coin_id: str) -> Optional[dict]:
    """
    获取单个或多个代币价格
    coin_id: bitcoin | ethereum | solana 等 CoinGecko ID
    """
    data = await fetch_json(
        f"{COINGECKO_BASE}/simple/price",
        params={
            "ids": coin_id,
            "vs_currencies": "usd,cny",
            "include_24hr_change": "true",
            "include_market_cap": "true",
            "include_24hr_vol": "true",
        }
    )
    return data


async def get_market_overview(limit: int = 10) -> Optional[list]:
    """获取市场 Top N 概览"""
    data = await fetch_json(
        f"{COINGECKO_BASE}/coins/markets",
        params={
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": limit,
            "page": 1,
            "sparkline": "false",
            "price_change_percentage": "1h,24h,7d",
        }
    )
    return data


async def get_trending() -> Optional[dict]:
    """获取 CoinGecko 热门搜索榜"""
    return await fetch_json(f"{COINGECKO_BASE}/search/trending")


async def get_fear_greed() -> Optional[dict]:
    """获取恐惧贪婪指数"""
    return await fetch_json("https://api.alternative.me/fng/?limit=1")


async def get_crypto_news(limit: int = 5) -> Optional[list]:
    """获取最新加密货币新闻"""
    data = await fetch_json(
        f"{CRYPTOCOMPARE_BASE}/v2/news/",
        params={"lang": "EN", "sortOrder": "latest", "limit": limit}
    )
    return data.get("Data") if data else None


async def get_whale_transactions(min_value: int = 1_000_000) -> Optional[dict]:
    """
    获取巨鲸大额转账记录
    需要 Whale Alert API Key (免费注册 whales.io)
    无 key 时返回模拟数据
    """
    if not WHALE_ALERT_KEY:
        # 无 API Key 时使用 CoinGecko 大额交易所流量作为替代
        return {"status": "no_key", "transactions": []}

    data = await fetch_json(
        f"{WHALE_ALERT_BASE}/transactions",
        params={
            "api_key": WHALE_ALERT_KEY,
            "min_value": min_value,
            "limit": 10,
        }
    )
    return data


async def get_exchange_flows(coin: str = "bitcoin") -> Optional[dict]:
    """
    获取交易所资金流入/流出 (CoinGecko Pro 特性的免费替代)
    使用全球交易所成交量数据推断
    """
    data = await fetch_json(
        f"{COINGECKO_BASE}/coins/{coin}/market_chart",
        params={"vs_currency": "usd", "days": "1", "interval": "hourly"}
    )
    return data


# ─── DeepSeek AI 对话 ─────────────────────────────────────────────────────────

SYSTEM_PROMPT = """你是一位专业的加密货币市场分析师助手，名为「CryptoSage」。

你的能力：
1. 分析实时市场数据、价格趋势
2. 解读巨鲸动向和资金流向
3. 提供技术分析和市场情绪分析
4. 回答加密货币相关问题
5. 风险提示和投资建议（附免责声明）

回答风格：
- 专业但易懂，适当使用表情符号增加可读性
- 数据分析要有逻辑，结论要有依据
- 始终在投资建议后附加风险免责声明
- 回复控制在 500 字内，避免冗长

重要提示：本机器人提供的所有信息仅供参考，不构成投资建议。"""


async def chat_with_deepseek(user_id: int, user_message: str, context_data: str = "") -> str:
    """
    调用 DeepSeek API 进行对话，保持上下文
    """
    # 初始化对话历史
    if user_id not in user_conversations:
        user_conversations[user_id] = []

    # 拼装带市场数据的用户消息
    full_message = user_message
    if context_data:
        full_message = f"[实时市场数据]\n{context_data}\n\n[用户问题]\n{user_message}"

    # 添加到历史
    user_conversations[user_id].append({"role": "user", "content": full_message})

    # 保持最近 10 轮对话（避免超 token）
    if len(user_conversations[user_id]) > 20:
        user_conversations[user_id] = user_conversations[user_id][-20:]

    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            *user_conversations[user_id]
        ],
        "temperature": 0.7,
        "max_tokens": 800,
        "stream": False
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{DEEPSEEK_BASE_URL}/v1/chat/completions",
                json=payload,
                headers={
                    "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json"
                },
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    assistant_msg = result["choices"][0]["message"]["content"]
                    # 记录 AI 回复到历史
                    user_conversations[user_id].append({
                        "role": "assistant",
                        "content": assistant_msg
                    })
                    return assistant_msg
                else:
                    error = await resp.text()
                    logger.error(f"DeepSeek error {resp.status}: {error}")
                    return "⚠️ AI 服务暂时不可用，请稍后再试。"
    except asyncio.TimeoutError:
        return "⏱️ AI 响应超时，请稍后再试。"
    except Exception as e:
        logger.error(f"DeepSeek exception: {e}")
        return "❌ 连接 AI 服务失败，请检查网络。"


# ─── 数据格式化函数 ───────────────────────────────────────────────────────────

def format_price_message(coin_id: str, data: dict) -> str:
    """格式化价格信息"""
    if coin_id not in data:
        return f"❌ 未找到 `{coin_id}` 的价格数据"
    
    d = data[coin_id]
    price_usd = d.get("usd", 0)
    price_cny = d.get("cny", 0)
    change_24h = d.get("usd_24h_change", 0) or 0
    vol_24h = d.get("usd_24h_vol", 0) or 0
    mktcap = d.get("usd_market_cap", 0) or 0

    emoji = "🟢" if change_24h >= 0 else "🔴"
    arrow = "↑" if change_24h >= 0 else "↓"

    return (
        f"💰 *{coin_id.upper()}* 实时价格\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💵 USD: `${price_usd:,.4f}`\n"
        f"🇨🇳 CNY: `¥{price_cny:,.2f}`\n"
        f"{emoji} 24h涨跌: `{arrow}{abs(change_24h):.2f}%`\n"
        f"📊 24h成交量: `${vol_24h:,.0f}`\n"
        f"🏦 市值: `${mktcap:,.0f}`\n"
        f"⏰ 更新: {datetime.now().strftime('%H:%M:%S')}"
    )


def format_market_overview(coins: list) -> str:
    """格式化市场概览"""
    lines = ["📊 *市场总览 Top 10*\n━━━━━━━━━━━━━━━"]
    for i, coin in enumerate(coins, 1):
        change = coin.get("price_change_percentage_24h") or 0
        emoji = "🟢" if change >= 0 else "🔴"
        lines.append(
            f"{i:2}. *{coin['symbol'].upper()}* {emoji} `{change:+.2f}%`\n"
            f"    `${coin['current_price']:,.4f}` | 市值: `${coin['market_cap']/1e9:.1f}B`"
        )
    return "\n".join(lines)


def format_trending(data: dict) -> str:
    """格式化热门趋势"""
    if not data or "coins" not in data:
        return "❌ 无法获取趋势数据"
    lines = ["🔥 *CoinGecko 热搜榜*\n━━━━━━━━━━━━━━━"]
    for i, item in enumerate(data["coins"][:7], 1):
        coin = item["item"]
        lines.append(
            f"{i}. *{coin['name']}* (`{coin['symbol']}`)\n"
            f"   市值排名: #{coin.get('market_cap_rank', 'N/A')}"
        )
    return "\n".join(lines)


def format_fear_greed(data: dict) -> str:
    """格式化恐惧贪婪指数"""
    if not data or "data" not in data:
        return "❌ 无法获取恐惧贪婪指数"
    d = data["data"][0]
    value = int(d["value"])
    classification = d["value_classification"]
    
    if value < 25:
        emoji = "😱"
    elif value < 45:
        emoji = "😰"
    elif value < 55:
        emoji = "😐"
    elif value < 75:
        emoji = "😊"
    else:
        emoji = "🤑"

    bar_filled = int(value / 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)

    return (
        f"{emoji} *恐惧贪婪指数*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"数值: `{value}/100`\n"
        f"状态: `{classification}`\n"
        f"进度: `[{bar}]`\n"
        f"时间: {datetime.now().strftime('%Y-%m-%d')}"
    )


def format_news(news_list: list) -> str:
    """格式化新闻"""
    if not news_list:
        return "❌ 无法获取新闻"
    lines = ["📰 *最新加密货币资讯*\n━━━━━━━━━━━━━━━"]
    for news in news_list[:5]:
        title = news.get("title", "")[:60]
        source = news.get("source", "")
        ts = datetime.fromtimestamp(news.get("published_on", 0)).strftime("%m/%d %H:%M")
        url = news.get("url", "")
        lines.append(f"• [{title}...]({url})\n  📌 {source} | {ts}")
    return "\n\n".join(lines)


# ─── Telegram 命令处理 ────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """欢迎命令"""
    keyboard = [
        [
            InlineKeyboardButton("💰 市场概览", callback_data="market"),
            InlineKeyboardButton("🔥 热门趋势", callback_data="trending"),
        ],
        [
            InlineKeyboardButton("😱 恐惧贪婪", callback_data="feargreed"),
            InlineKeyboardButton("📰 最新资讯", callback_data="news"),
        ],
        [
            InlineKeyboardButton("🐋 巨鲸动向", callback_data="whale"),
            InlineKeyboardButton("❓ 帮助", callback_data="help"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🤖 *CryptoSage - 加密市场智能助手*\n\n"
        "我可以帮你：\n"
        "• 📊 实时查询价格和市场数据\n"
        "• 🐋 追踪巨鲸大额转账\n"
        "• 📰 推送最新市场资讯\n"
        "• 🤖 AI 分析市场行情\n\n"
        "直接发消息给我，或点击下方按钮：",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )


async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /price bitcoin
    /price ethereum solana
    """
    if not context.args:
        await update.message.reply_text(
            "用法: `/price <币种ID>`\n例如: `/price bitcoin` 或 `/price ethereum solana`",
            parse_mode="Markdown"
        )
        return

    coin_ids = " ".join(context.args).lower().replace(",", " ").split()
    coin_str = ",".join(coin_ids)

    msg = await update.message.reply_text("⏳ 查询中...")
    data = await get_price(coin_str)

    if not data:
        await msg.edit_text("❌ 获取价格失败，请检查币种名称是否正确（使用 CoinGecko ID）")
        return

    results = []
    for coin_id in coin_ids:
        results.append(format_price_message(coin_id, data))

    keyboard = [[InlineKeyboardButton("🔄 刷新", callback_data=f"refresh_price_{coin_str}")]]
    await msg.edit_text(
        "\n\n".join(results),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def cmd_market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """市场概览"""
    msg = await update.message.reply_text("⏳ 加载市场数据...")
    coins = await get_market_overview(10)
    if not coins:
        await msg.edit_text("❌ 无法获取市场数据")
        return
    await msg.edit_text(format_market_overview(coins), parse_mode="Markdown")


async def cmd_whale(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /whale         → 查看巨鲸动向
    /whale 5000000 → 最低 500万 USD 的转账
    """
    min_val = 1_000_000
    if context.args:
        try:
            min_val = int(context.args[0])
        except ValueError:
            pass

    msg = await update.message.reply_text("🐋 查询巨鲸动向...")
    whale_data = await get_whale_transactions(min_val)

    if not whale_data:
        await msg.edit_text("❌ 无法获取巨鲸数据")
        return

    if whale_data.get("status") == "no_key":
        # 无 Whale Alert API Key，用 AI + 交易所流量数据分析
        btc_flows = await get_exchange_flows("bitcoin")
        eth_flows = await get_exchange_flows("ethereum")
        
        context_str = ""
        if btc_flows and "volumes" in btc_flows:
            context_str += f"BTC 近24h链上成交量数据点数: {len(btc_flows.get('total_volumes', []))}\n"
        
        ai_analysis = await chat_with_deepseek(
            update.effective_user.id,
            "请基于当前市场状况，分析可能的巨鲸行为模式和资金流向趋势，并给出注意事项。",
            context_str
        )
        await msg.edit_text(
            "🐋 *巨鲸动向分析*\n━━━━━━━━━━━━━━━\n"
            "⚠️ 未配置 Whale Alert API，以下为 AI 分析：\n\n"
            f"{ai_analysis}\n\n"
            "💡 提示: 在 `.env` 中配置 `WHALE_ALERT_API_KEY` 获取实时巨鲸数据\n"
            "注册地址: https://whale-alert.io",
            parse_mode="Markdown"
        )
        return

    transactions = whale_data.get("transactions", [])
    if not transactions:
        await msg.edit_text("⚠️ 最近没有符合条件的巨鲸转账记录")
        return

    lines = [f"🐋 *巨鲸动向 (最低 ${min_val:,})*\n━━━━━━━━━━━━━━━"]
    for tx in transactions[:8]:
        amount = tx.get("amount", 0)
        symbol = tx.get("symbol", "").upper()
        amount_usd = tx.get("amount_usd", 0)
        from_owner = tx.get("from", {}).get("owner", "未知地址")
        to_owner = tx.get("to", {}).get("owner", "未知地址")
        tx_type = "➡️" if "unknown" not in from_owner.lower() else "📤"
        
        lines.append(
            f"{tx_type} `{amount:,.0f} {symbol}` (≈`${amount_usd:,.0f}`)\n"
            f"   {from_owner} → {to_owner}"
        )

    await msg.edit_text("\n\n".join(lines), parse_mode="Markdown")


async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """最新资讯"""
    msg = await update.message.reply_text("📰 获取最新资讯...")
    news = await get_crypto_news(5)
    await msg.edit_text(format_news(news), parse_mode="Markdown", disable_web_page_preview=True)


async def cmd_fear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """恐惧贪婪指数"""
    msg = await update.message.reply_text("⏳ 查询中...")
    data = await get_fear_greed()
    await msg.edit_text(format_fear_greed(data), parse_mode="Markdown")


async def cmd_trending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """热门趋势"""
    msg = await update.message.reply_text("🔥 获取热搜榜...")
    data = await get_trending()
    await msg.edit_text(format_trending(data), parse_mode="Markdown")


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """清除对话历史"""
    user_id = update.effective_user.id
    user_conversations[user_id] = []
    await update.message.reply_text("✅ 对话历史已清除")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """帮助信息"""
    await update.message.reply_text(
        "📖 *命令列表*\n━━━━━━━━━━━━━━━\n"
        "`/start` - 主菜单\n"
        "`/price <ID>` - 查询价格 (支持多个)\n"
        "  例: `/price bitcoin ethereum solana`\n\n"
        "`/market` - 市场 Top 10 概览\n"
        "`/whale [最低金额]` - 巨鲸动向\n"
        "  例: `/whale 5000000`\n\n"
        "`/news` - 最新资讯\n"
        "`/fear` - 恐惧贪婪指数\n"
        "`/trending` - 热门趋势\n"
        "`/clear` - 清除 AI 对话历史\n\n"
        "💬 *直接发消息* - 与 AI 对话，自动获取市场数据辅助分析\n\n"
        "📌 币种 ID 参考 CoinGecko: https://coingecko.com",
        parse_mode="Markdown"
    )


# ─── 自然语言消息处理 (AI 对话) ───────────────────────────────────────────────

PRICE_KEYWORDS = ["价格", "多少钱", "price", "怎么样", "行情", "最新", "现在"]
WHALE_KEYWORDS = ["巨鲸", "whale", "大户", "转账", "资金流"]
MARKET_KEYWORDS = ["市场", "大盘", "market", "趋势", "overview"]
NEWS_KEYWORDS = ["新闻", "资讯", "news", "最新消息"]

# 常见币种别名映射 → CoinGecko ID
COIN_ALIAS = {
    "btc": "bitcoin", "比特币": "bitcoin",
    "eth": "ethereum", "以太坊": "ethereum", "以太": "ethereum",
    "sol": "solana", "索拉纳": "solana",
    "bnb": "binancecoin",
    "xrp": "ripple", "瑞波币": "ripple",
    "ada": "cardano",
    "doge": "dogecoin", "狗狗币": "dogecoin",
    "ton": "the-open-network",
    "dot": "polkadot", "波卡": "polkadot",
    "avax": "avalanche-2", "雪崩": "avalanche-2",
    "link": "chainlink",
    "uni": "uniswap",
    "ltc": "litecoin", "莱特币": "litecoin",
    "shib": "shiba-inu",
    "sui": "sui",
    "trx": "tron", "波场": "tron",
}


def detect_coins_in_message(text: str) -> list[str]:
    """从消息中识别提到的币种"""
    text_lower = text.lower()
    found = []
    for alias, coin_id in COIN_ALIAS.items():
        if alias in text_lower and coin_id not in found:
            found.append(coin_id)
    return found[:3]  # 最多3个


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理普通消息 - AI 对话 + 自动注入市场数据"""
    user_id = update.effective_user.id
    text = update.message.text

    # 发送 typing 状态
    await context.bot.send_chat_action(update.effective_chat.id, "typing")

    # 自动检测消息中提到的币种，注入实时数据
    context_parts = []
    
    # 检测币种并获取价格
    coins = detect_coins_in_message(text)
    if coins or any(kw in text for kw in PRICE_KEYWORDS):
        if not coins:
            coins = ["bitcoin", "ethereum"]  # 默认查询主流币
        price_data = await get_price(",".join(coins))
        if price_data:
            price_str = json.dumps(price_data, ensure_ascii=False)
            context_parts.append(f"实时价格数据: {price_str}")

    # 检测是否需要市场数据
    if any(kw in text for kw in MARKET_KEYWORDS):
        market_data = await get_market_overview(5)
        if market_data:
            summary = [f"{c['symbol'].upper()}: ${c['current_price']:,.2f} ({c['price_change_percentage_24h']:+.2f}%)" 
                      for c in market_data]
            context_parts.append("市场Top5: " + " | ".join(summary))

    # 检测是否需要恐惧贪婪指数
    if any(kw in text for kw in ["恐惧", "贪婪", "情绪", "fear", "greed"]):
        fg = await get_fear_greed()
        if fg and "data" in fg:
            d = fg["data"][0]
            context_parts.append(f"恐惧贪婪指数: {d['value']}/100 ({d['value_classification']})")

    context_data = "\n".join(context_parts)
    
    # 调用 DeepSeek AI
    response = await chat_with_deepseek(user_id, text, context_data)
    
    await update.message.reply_text(response, parse_mode="Markdown")


# ─── 按钮回调处理 ─────────────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理内联键盘按钮点击"""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "market":
        coins = await get_market_overview(10)
        if coins:
            await query.edit_message_text(format_market_overview(coins), parse_mode="Markdown")
    
    elif data == "trending":
        trend = await get_trending()
        await query.edit_message_text(format_trending(trend), parse_mode="Markdown")
    
    elif data == "feargreed":
        fg = await get_fear_greed()
        await query.edit_message_text(format_fear_greed(fg), parse_mode="Markdown")
    
    elif data == "news":
        news = await get_crypto_news(5)
        await query.edit_message_text(
            format_news(news), parse_mode="Markdown", disable_web_page_preview=True
        )
    
    elif data == "whale":
        whale_data = await get_whale_transactions()
        if not WHALE_ALERT_KEY:
            ai_text = await chat_with_deepseek(
                query.from_user.id,
                "请分析当前加密货币市场的巨鲸资金动向和链上数据特征。"
            )
            await query.edit_message_text(
                f"🐋 *巨鲸动向 AI 分析*\n━━━━━━━━━━━━━━━\n{ai_text}",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text(format_trending({}), parse_mode="Markdown")
    
    elif data == "help":
        await query.edit_message_text(
            "📖 直接发消息与 AI 对话，或使用命令:\n"
            "/price /market /whale /news /fear /trending",
            parse_mode="Markdown"
        )
    
    elif data.startswith("refresh_price_"):
        coin_str = data.replace("refresh_price_", "")
        price_data = await get_price(coin_str)
        if price_data:
            results = [format_price_message(cid, price_data) for cid in coin_str.split(",")]
            keyboard = [[InlineKeyboardButton("🔄 刷新", callback_data=f"refresh_price_{coin_str}")]]
            await query.edit_message_text(
                "\n\n".join(results),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )


# ─── 定时任务 ─────────────────────────────────────────────────────────────────

async def scheduled_market_update(context: ContextTypes.DEFAULT_TYPE):
    """
    定时推送市场更新 (需要配置 ALERT_CHAT_ID)
    可在 main() 中启用
    """
    chat_id = os.getenv("ALERT_CHAT_ID")
    if not chat_id:
        return

    coins = await get_market_overview(5)
    fg = await get_fear_greed()
    
    if coins:
        msg = format_market_overview(coins)
        if fg:
            msg += "\n\n" + format_fear_greed(fg)
        await context.bot.send_message(chat_id, msg, parse_mode="Markdown")


# ─── 主程序 ───────────────────────────────────────────────────────────────────

def main():
    print("🚀 CryptoSage Bot 启动中...")
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # 注册命令
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("market", cmd_market))
    app.add_handler(CommandHandler("whale", cmd_whale))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("fear", cmd_fear))
    app.add_handler(CommandHandler("trending", cmd_trending))
    app.add_handler(CommandHandler("clear", cmd_clear))
    
    # 注册按钮回调
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    # 注册普通消息处理 (AI 对话)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # 可选: 定时推送 (每小时)
    # alert_chat_id = os.getenv("ALERT_CHAT_ID")
    # if alert_chat_id:
    #     app.job_queue.run_repeating(scheduled_market_update, interval=3600, first=10)
    #     print(f"✅ 定时推送已启用，目标群组: {alert_chat_id}")

    print("✅ Bot 已就绪，按 Ctrl+C 停止")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()