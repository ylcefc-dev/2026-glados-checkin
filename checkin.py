#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
2026 GLaDOS 自动签到 (积分增强版)

功能：
- 全自动签到
- 精准获取当前积分 (Points)
- PushPlus 微信推送 / Telegram 推送（包含积分、剩余天数、签到结果）
- 智能多域名切换 (优先 glados.cloud)
- 支持 Cookie-Editor 导出格式

v1.2.0 变更（推送可靠性）：
- 推送结果不再静默：成功/失败都会写入 Actions Step Summary 并生成 error annotation
- 未配置推送渠道、Telegram 只配了一半，都会显式报错，而不是什么都不做
- Telegram 长消息自动分段（>4096 字符会被 API 拒绝）
- 日志中隐藏 bot token，避免泄露到公开仓库的 Actions 日志
- 修正 get_points 中的不可达重复代码与积分非数字时的崩溃
"""

import requests
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

# Fix Windows Unicode Output
if sys.platform.startswith('win'):
    sys.stdout.reconfigure(encoding='utf-8')

# ================= 配置 =================

# 域名优先级：Cloud 第一
DOMAINS = [
    "https://glados.cloud",
    "https://glados.rocks",
    "https://glados.network",
]

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Content-Type': 'application/json;charset=UTF-8',
    'Accept': 'application/json, text/plain, */*',
}

TG_MAX_LEN = 4000  # Telegram 单条上限 4096，留点余量

# ================= 工具函数 =================

def log(msg):
    ts = datetime.now(
        timezone(timedelta(hours=8))
    ).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")

def redact(text):
    """隐藏 bot token，避免 requests 异常把 token 带进公开日志"""
    return re.sub(r"(bot\d+:)[A-Za-z0-9_\-]+", r"\1***", str(text))

def annotate(level, msg):
    """GitHub Actions annotation：在运行页面顶部直接显示错误"""
    print(f"::{level}::{msg}")

def summary(markdown):
    """写入 Actions Step Summary，跑完在运行页首屏可见"""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(markdown + "\n")
    except Exception:
        pass

def extract_cookie(raw: str):
    """提取 Cookie，支持 Cookie-Editor 冒号格式"""
    if not raw: return None
    raw = raw.strip()

    # Cookie-Editor 格式 (koa:sess=xxx; koa:sess.sig=yyy)
    if 'koa:sess=' in raw or 'koa:sess.sig=' in raw:
        return raw

    # JSON
    if raw.startswith('{'):
        try:
            return 'koa.sess=' + json.loads(raw).get('token')
        except: pass

    # JWT Token
    if raw.count('.') == 2 and '=' not in raw and len(raw) > 50:
        return 'koa:sess=' + raw

    # Standard
    return raw

def get_cookies():
    raw = os.environ.get("GLADOS_COOKIE", "")
    if not raw:
        log("❌ 未配置 GLADOS_COOKIE")
        return []

    # Split by enter or &
    sep = '\n' if '\n' in raw else '&'
    return [extract_cookie(c) for c in raw.split(sep) if c.strip()]

# ================= 核心逻辑 =================

class GLaDOS:
    def __init__(self, cookie):
        self.plan = "?"
        self.error = ""
        self.cookie_status = "❓未知"
        self.cookie = cookie
        self.domain = DOMAINS[0]
        self.email = "?"
        self.left_days = "?"
        self.points = "?"
        self.points_change = "?"
        self.exchange_info = ""

    def req(self, method, path, data=None):
        """带自动域名切换的请求"""
        for d in DOMAINS:
            try:
                url = f"{d}{path}"
                h = HEADERS.copy()
                h['Cookie'] = self.cookie
                h['Origin'] = d
                h['Referer'] = f"{d}/console/checkin"

                if method == 'GET':
                    resp = requests.get(url, headers=h, timeout=10)
                else:
                    resp = requests.post(url, headers=h, json=data, timeout=10)

                if resp.status_code == 200:
                    self.domain = d # Remember working domain
                    return resp.json()
            except Exception as e:
                log(f"⚠️ {d} 请求失败: {e}")
                continue
        return None

    def get_status(self):
        """获取状态：天数、邮箱"""
        res = self.req('GET', '/api/user/status')

        if res and res.get("code") == -2:
            self.error = "Cookie无权限，请重新获取Cookie"
            self.cookie_status = "❌失效"
            return False

        if res and 'data' in res:
            self.cookie_status = "✅正常"
            d = res['data']
            self.email = d.get('email', 'Unknown')
            self.left_days = str(d.get('leftDays', '?')).split('.')[0]
            return True

        return False

    def get_points(self):
        """获取积分、变化历史、兑换计划"""
        res = self.req('GET', '/api/user/points')

        if not (res and 'points' in res):
            return False

        # 当前积分
        self.points = str(res.get('points', '0')).split('.')[0]

        # 最近一次积分变化
        history = res.get('history', [])
        if history:
            last = history[0]
            change = str(last.get('change', '0')).split('.')[0]
            if not change.startswith('-'):
                change = '+' + change
            self.points_change = change

        # 兑换计划（兼容 "1,234" 这类千分位格式；确实非数字时按 0 处理，不崩）
        try:
            pts = int(str(self.points).replace(',', '').replace(' ', ''))
        except ValueError:
            pts = 0

        plans = res.get('plans', {})
        exchange_lines = []
        for plan_id, plan_data in plans.items():
            need = plan_data.get('points', 0)
            days = plan_data.get('days', 0)
            if pts >= need:
                exchange_lines.append(f"✅ {need}分→{days}天 (可兑换)")
            else:
                exchange_lines.append(f"❌ {need}分→{days}天 (差{need-pts}分)")

        self.exchange_info = "<br>".join(exchange_lines)
        return True

    def checkin(self):
        """执行签到"""
        return self.req('POST', '/api/user/checkin', {'token': 'glados.cloud'})

# ================= 推送 =================

def pushplus(token, title, content):
    """返回 True/False；None 表示未配置"""
    if not token:
        return None
    try:
        url = "http://www.pushplus.plus/send"
        r = requests.get(url, params={
            'token': token, 'title': title, 'content': content, 'template': 'html'
        }, timeout=15)
        body = (r.text or "")[:300]
        if r.status_code == 200 and '"code":200' in body.replace(" ", ""):
            log("✅ PushPlus 推送成功")
            return True
        log(f"❌ PushPlus 推送失败: HTTP {r.status_code} {body}")
        return False
    except Exception as e:
        log(f"❌ PushPlus 推送失败: {redact(e)}")
        return False

def html_to_tg(title, content):
    """把 PushPlus 用的 HTML 转成 Telegram 支持的 HTML"""
    text = f"<b>{title}</b>\n\n{content}"

    # 1. Block elements replacements (handle tags with attributes)
    text = text.replace("<br>", "\n")
    # Handle H3 tags
    text = re.sub(r"<h3[^>]*>", "<b>", text)
    text = text.replace("</h3>", "</b>\n")

    # 2. Paragraph and Div tags
    text = re.sub(r"<(div|p)[^>]*>", "", text)
    text = re.sub(r"</(div|p)>", "\n", text)

    # 3. Span and small tags
    text = re.sub(r"<(span|small)[^>]*>", "", text)
    text = re.sub(r"</(span|small)>", "", text)

    # 4. Strip all HTML tags EXCEPT the ones supported by Telegram: b, i, u, s, a, code, pre
    text = re.sub(r"<(?!\/?(b|i|u|s|a|code|pre)\b)[^>]+>", "", text)

    # 5. Dedent each line to fix alignment issues caused by HTML template indentation
    lines = [line.strip() for line in text.split('\n')]
    text = "\n".join(lines)

    # 6. Collapse multiple newlines
    return re.sub(r"\n\s*\n", "\n\n", text).strip()

def split_text(text, limit=TG_MAX_LEN):
    """按行切分超长消息，避免 400 message is too long"""
    if len(text) <= limit:
        return [text]

    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{cur}\n{line}" if cur else line
        if len(candidate) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return chunks

def telegram_push(token, chat_id, title, content):
    """返回 True/False；None 表示未配置"""
    if not token or not chat_id:
        return None

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        text = html_to_tg(title, content)
        chunks = split_text(text)
        ok = True

        for idx, chunk in enumerate(chunks, 1):
            resp = requests.post(url, json={
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML"
            }, timeout=20)

            try:
                body = resp.json()
            except Exception:
                body = {"raw": (resp.text or "")[:300]}

            if resp.status_code == 200 and body.get("ok"):
                log(f"✅ Telegram 推送成功 ({idx}/{len(chunks)})")
            else:
                ok = False
                detail = redact(json.dumps(body, ensure_ascii=False))
                log(f"❌ Telegram 推送失败 ({idx}/{len(chunks)}): HTTP {resp.status_code} {detail}")

        return ok
    except Exception as e:
        log(f"❌ Telegram 推送失败: {redact(e)}")
        return False

# ================= 主程序 =================

def main():
    log("🚀 2026 GLaDOS Checkin Starting...")
    cookies = get_cookies()
    if not cookies:
        annotate("error", "未配置 GLADOS_COOKIE，无法签到")
        sys.exit(1)

    results = []
    success_cnt = 0

    for i, cookie in enumerate(cookies, 1):
        g = GLaDOS(cookie)

        # 1. Checkin
        res = g.checkin()
        msg = res.get('message', 'Failure') if res else "Network Error"

        # 2. Get Info (Refresh data)
        g.get_status()
        g.get_points()

        # 3. Log
        log(f"用户: {g.email} | 积分: {g.points} | 天数: {g.left_days} | 结果: {msg}")

        if not g.error:
            success_cnt += 1

        # 4. Result Formatting
        results.append(f"""
<div style="border:2px solid #333; padding:15px; margin-bottom:15px; border-radius:10px; background:#fff;">
    <h3 style="margin:0 0 15px 0; color:#333; border-bottom:2px solid #333; padding-bottom:8px;">👤 {g.email}</h3>
    <p style="margin:8px 0; color:#000; font-size:16px;"><b>🔐 Cookie状态:</b> {g.cookie_status}</p>
    <p style="margin:8px 0; color:#000; font-size:16px;"><b>当前积分:</b> <span style="color:#e74c3c; font-size:22px; font-weight:bold;">{g.points}</span> <span style="color:#27ae60; font-weight:bold;">({g.points_change})</span></p>
    <p style="margin:8px 0; color:#000; font-size:16px;"><b>剩余天数:</b> <span style="font-weight:bold;">{g.left_days} 天</span></p>
    <p style="margin:8px 0; color:#000; font-size:16px;"><b>签到结果:</b> {msg}</p>
    <div style="margin-top:15px; padding:12px; background:#f0f0f0; border-radius:8px; border:1px solid #ccc;">
        <p style="margin:0 0 8px 0; color:#333; font-weight:bold; font-size:15px;">🎁 兑换选项:</p>
        <p style="margin:0; color:#000; font-size:14px; line-height:1.8;">
{g.exchange_info}</p>
    </div>
</div>
""")

    # ===== 推送渠道配置检查（避免静默不推送）=====
    ptoken = os.environ.get("PUSHPLUS_TOKEN") or ""
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN") or ""
    tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID") or ""

    has_pp = bool(ptoken)
    has_tg = bool(tg_token and tg_chat_id)

    if bool(tg_token) != bool(tg_chat_id):
        annotate("error", "Telegram 配置不完整：TELEGRAM_BOT_TOKEN 与 TELEGRAM_CHAT_ID 必须同时设置")
    if not has_pp and not has_tg:
        annotate("error", "未配置任何推送渠道（TELEGRAM_BOT_TOKEN+TELEGRAM_CHAT_ID 或 PUSHPLUS_TOKEN），本次结果不会推送")

    push_level = os.environ.get("PUSH_LEVEL", "all").lower()

    if push_level == "fail_only" and success_cnt == len(cookies):
        log("⏭️ 根据 PUSH_LEVEL=fail_only 设置，所有账号签到成功，跳过推送")
        summary(f"## GLaDOS 签到\n\n- 结果：**{success_cnt}/{len(cookies)}** 全部成功\n- 推送：按 `PUSH_LEVEL=fail_only` 跳过")
        return

    title = f"GLaDOS签到: 成功{success_cnt}/{len(cookies)}"
    content = "".join(results)
    content += f"<br><small>时间: {datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')}</small>"

    outcome = {}
    if has_pp:
        outcome["PushPlus"] = pushplus(ptoken, title, content)
    if has_tg:
        outcome["Telegram"] = telegram_push(tg_token, tg_chat_id, title, content)

    # ===== 结果汇总：写进 Actions 摘要，失败直接报错 =====
    lines = [
        "## GLaDOS 签到结果",
        "",
        f"- 账号：**{success_cnt}/{len(cookies)}** 正常",
    ]
    for name, ok in outcome.items():
        lines.append(f"- {name} 推送：{'✅ 成功' if ok else '❌ 失败'}")
        if not ok:
            annotate("error", f"{name} 推送失败，请检查 Secrets 配置与机器人状态")
    summary("\n".join(lines))

if __name__ == '__main__':
    main()
