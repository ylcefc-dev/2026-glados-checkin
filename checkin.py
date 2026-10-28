#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
2026 GLaDOS 自动签到 (积分增强版)

v1.3.0 变更（失败可见 + 判定修正 + 重试真正生效）：
- 签到成败以接口返回为准：不再用「g.error 为空即成功」这种假判定
  旧逻辑下，请求彻底失败（返回 None）也会被计为成功，于是 job 永远是绿的
- Cookie 失效单独识别（code == -2 / message 含「没有权限」），
  直接打 error annotation + 写摘要，并退出码 2（致命，重试无意义）
- 可重试类失败（网络、5xx、超时）在脚本内部重试 3 次（20s/40s 退避），
  因此工作流里的 Retry 步骤可以去掉，避免重复推送
- 退出码语义：0 全部成功 / 1 可重试失败 / 2 凭据或配置错误（不可重试）
- 失败时打印接口原始响应（已脱敏、截断），下次出问题不用再猜
- 多账号时任一账号失败即整体判失败，不会出现「1 成功 1 失败却报 2/2」

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
import time
from datetime import datetime, timezone, timedelta

# Fix Windows Unicode Output
if sys.platform.startswith('win'):
    sys.stdout.reconfigure(encoding='utf-8')

# ================= 配置 =================

# 域名优先级：Cloud 第一
# 注：glados.network 在当前网络下存在 DNS 污染，实测经常连不上，仅作最后兜底
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

TG_MAX_LEN = 4000        # Telegram 单条上限 4096，留点余量
RETRY_TIMES = 3          # 可重试失败的尝试次数（含首次）
RETRY_BACKOFF = 20       # 秒，第 n 次失败后等待 n * RETRY_BACKOFF

# 视为「今天已经签过」的返回文案 —— 不算失败
ALREADY_DONE_KEYWORDS = ("Try Tomorrow", "already", "已签到", "明天")

# 视为 Cookie 失效的返回文案
AUTH_FAIL_KEYWORDS = ("没有权限", "无权限", "unauthorized", "forbidden")

# 退出码
EXIT_OK = 0
EXIT_RETRYABLE = 1
EXIT_FATAL = 2

# ================= 工具函数 =================

def log(msg):
    ts = datetime.now(
        timezone(timedelta(hours=8))
    ).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def redact(text):
    """隐藏 bot token 与 Cookie，避免 requests 异常把凭据带进公开日志"""
    text = re.sub(r"(bot\d+:)[A-Za-z0-9_\-]+", r"\1***", str(text))
    text = re.sub(r"(koa[:.]sess[^=]*=)[^;\s\"']+", r"\1***", text)
    return text

def annotate(level, msg):
    """GitHub Actions annotation：在运行页面顶部直接显示错误"""
    print(f"::{level}::{msg}", flush=True)

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

def with_alias(cookie: str) -> str:
    """同一会话值同时以 gladss 与 koa:sess 两种命名发送

    GLaDOS 新版会话 Cookie 名为 gladss / gladss.sig，旧版为 koa:sess / koa:sess.sig。
    两种命名同时带上，服务端认哪个用哪个，Secret 里写哪种格式都能跑。
    """
    if not cookie:
        return cookie
    found = re.findall(r"([A-Za-z0-9_.:]+)\s*=\s*([^;\s]+)", cookie)
    if not found:
        return cookie
    d = {k.replace("koa.sess", "koa:sess"): v for k, v in found}
    sess = d.get("koa:sess") or d.get("gladss")
    sig = d.get("koa:sess.sig") or d.get("gladss.sig")
    if not sess:
        return cookie

    parts = [f"gladss={sess}"]
    if sig:
        parts.append(f"gladss.sig={sig}")
    parts.append(f"koa:sess={sess}")
    if sig:
        parts.append(f"koa:sess.sig={sig}")
    return "; ".join(parts)

def extract_cookie(raw: str):
    """提取 Cookie，支持 Cookie-Editor 冒号格式"""
    if not raw:
        return None
    raw = raw.strip()

    # 已经是 Cookie 串（新旧两种命名都直接放行，并补齐别名）
    if 'koa:sess=' in raw or 'koa:sess.sig=' in raw or 'gladss=' in raw or 'gladss.sig=' in raw:
        return with_alias(raw)

    # JSON
    if raw.startswith('{'):
        try:
            return with_alias('koa:sess=' + json.loads(raw).get('token'))
        except Exception:
            pass

    # JWT Token
    if raw.count('.') == 2 and '=' not in raw and len(raw) > 50:
        return with_alias('koa:sess=' + raw)

    # Standard
    return with_alias(raw)

def get_cookies():
    raw = os.environ.get("GLADOS_COOKIE", "")
    if not raw:
        log("❌ 未配置 GLADOS_COOKIE")
        return []

    # 多账号分隔：优先按 & 分隔；仅当没有 & 且存在换行时才按换行拆
    if '&' in raw:
        parts = raw.split('&')
    else:
        parts = raw.split('\n')
    return [extract_cookie(c) for c in parts if c.strip()]

# ================= 核心逻辑 =================

class GLaDOS:
    def __init__(self, cookie):
        self.plan = "?"
        self.error = ""
        self.auth_failed = False       # Cookie 失效（致命，重试无意义）
        self.cookie_status = "❓未知"
        self.cookie = cookie
        self.domain = DOMAINS[0]
        self.email = "?"
        self.left_days = "?"
        self.points = "?"
        self.points_change = "?"
        self.exchange_info = ""
        self.last_raw = ""

    def req(self, method, path, data=None):
        """带自动域名切换的请求；返回 (ok, body)。网络/5xx 视为可重试"""
        # 上次成功的域名优先，减少无谓超时
        ordered = [self.domain] + [d for d in DOMAINS if d != self.domain]
        for d in ordered:
            try:
                url = f"{d}{path}"
                h = HEADERS.copy()
                h['Cookie'] = self.cookie
                h['Origin'] = d
                h['Referer'] = f"{d}/console/checkin"

                if method == 'GET':
                    resp = requests.get(url, headers=h, timeout=15)
                else:
                    resp = requests.post(url, headers=h, json=data, timeout=15)

                if resp.status_code == 200:
                    try:
                        body = resp.json()
                    except Exception:
                        log(f"⚠️ {d} 返回非 JSON: {redact(resp.text)[:120]}")
                        continue
                    self.domain = d
                    self.last_raw = json.dumps(body, ensure_ascii=False)[:300]
                    return True, body

                log(f"⚠️ {d} HTTP {resp.status_code}: {redact(resp.text)[:120]}")
            except Exception as e:
                log(f"⚠️ {d} 请求失败: {redact(e)}")
                continue
        return False, None

    def get_status(self):
        """获取状态：天数、邮箱"""
        ok, res = self.req('GET', '/api/user/status')
        if not ok or not isinstance(res, dict):
            return False

        if res.get("code") == -2:
            self.error = "Cookie无权限，请重新获取Cookie"
            self.cookie_status = "❌失效"
            self.auth_failed = True
            return False

        if 'data' in res:
            self.cookie_status = "✅正常"
            d = res['data']
            self.email = d.get('email', 'Unknown')
            self.left_days = str(d.get('leftDays', '?')).split('.')[0]
            return True

        return False

    def get_points(self):
        """获取积分、变化历史、兑换计划"""
        ok, res = self.req('GET', '/api/user/points')
        if not ok or not isinstance(res, dict):
            return False

        if res.get("code") == -2:
            self.auth_failed = True
            return False

        if 'points' not in res:
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
        """执行签到；返回 (state, msg)
        state: 'ok' | 'already' | 'auth' | 'fail'
        """
        ok, res = self.req('POST', '/api/user/checkin', {'token': 'glados.cloud'})
        if not ok or not isinstance(res, dict):
            return 'fail', 'Network Error'

        msg = str(res.get('message', '') or '')
        code = res.get('code')

        # 1) 凭据失效：任何后续重试都不会成功
        if code == -2 or any(k.lower() in msg.lower() for k in AUTH_FAIL_KEYWORDS):
            return 'auth', msg or '没有权限'

        # 2) 今天已签过：视为成功，不是故障
        if any(k.lower() in msg.lower() for k in ALREADY_DONE_KEYWORDS):
            return 'already', msg or 'Please Try Tomorrow'

        # 3) 正常成功：GLaDOS 成功时 code 为 0
        if code == 0:
            return 'ok', msg or 'Checkin OK'

        # 4) 其余一律算失败，把原始 message 带出来
        return 'fail', msg or f'Failure (code={code})'

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

COOKIE_FIX_HINT = (
    "Cookie 已失效，需要人工更新："
    "浏览器登录 https://glados.cloud/console/checkin（先确认页面能正常显示账号，若跳回首页说明会话已失效需重新登录）"
    "→ F12 → Network → 刷新 → 点任意 api 请求 → Request Headers → 复制 Cookie 整行"
    "（新版 Cookie 名为 gladss / gladss.sig，旧版为 koa:sess / koa:sess.sig，两种都支持）"
    "→ 更新仓库 Secret GLADOS_COOKIE → 手动运行一次 checkin 工作流验证"
)


def checkin_with_retry(g):
    """单账号签到，可重试类失败自动重试；返回 (state, msg)"""
    state, msg = 'fail', 'Network Error'
    for attempt in range(1, RETRY_TIMES + 1):
        state, msg = g.checkin()
        if state in ('ok', 'already', 'auth'):
            return state, msg
        if attempt < RETRY_TIMES:
            wait = RETRY_BACKOFF * attempt
            log(f"🔁 第 {attempt} 次签到失败（{msg}），{wait}s 后重试")
            time.sleep(wait)
    return state, msg


def main():
    log("🚀 2026 GLaDOS Checkin Starting... (v1.3.0)")
    cookies = get_cookies()
    if not cookies:
        annotate("error", "未配置 GLADOS_COOKIE，无法签到")
        summary("## GLaDOS 签到结果\n\n- ❌ 未配置 `GLADOS_COOKIE`，无法签到")
        return EXIT_FATAL

    results = []
    ok_cnt = 0
    auth_failed = False
    retryable = False

    for i, cookie in enumerate(cookies, 1):
        g = GLaDOS(cookie)

        # 1. Checkin（含内部重试）
        state, msg = checkin_with_retry(g)

        # 2. Get Info (Refresh data)
        g.get_status()
        g.get_points()

        # 3. Log
        log(f"用户: {g.email} | 积分: {g.points} | 天数: {g.left_days} | 结果: {state}/{msg}")

        # 4. 统计
        if state in ('ok', 'already'):
            ok_cnt += 1
        elif state == 'auth' or g.auth_failed:
            auth_failed = True
            g.cookie_status = "❌失效"
        else:
            retryable = True

        if state == 'auth' or g.auth_failed:
            result_line = f"{msg}（Cookie 失效）"
        elif state == 'already':
            result_line = f"{msg}（今日已签到）"
        else:
            result_line = msg

        # 5. Result Formatting
        results.append(f"""
<div style="border:2px solid #333; padding:15px; margin-bottom:15px; border-radius:10px; background:#fff;">
    <h3 style="margin:0 0 15px 0; color:#333; border-bottom:2px solid #333; padding-bottom:8px;">👤 {g.email}</h3>
    <p style="margin:8px 0; color:#000; font-size:16px;"><b>🔐 Cookie状态:</b> {g.cookie_status}</p>
    <p style="margin:8px 0; color:#000; font-size:16px;"><b>当前积分:</b> <span style="color:#e74c3c; font-size:22px; font-weight:bold;">{g.points}</span> <span style="color:#27ae60; font-weight:bold;">({g.points_change})</span></p>
    <p style="margin:8px 0; color:#000; font-size:16px;"><b>剩余天数:</b> <span style="font-weight:bold;">{g.left_days} 天</span></p>
    <p style="margin:8px 0; color:#000; font-size:16px;"><b>签到结果:</b> {result_line}</p>
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

    if auth_failed:
        annotate("error", COOKIE_FIX_HINT)

    push_level = os.environ.get("PUSH_LEVEL", "all").lower()
    all_ok = (ok_cnt == len(cookies))

    if push_level == "fail_only" and all_ok:
        log("⏭️ 根据 PUSH_LEVEL=fail_only 设置，所有账号签到成功，跳过推送")
        summary(f"## GLaDOS 签到\n\n- 结果：**{ok_cnt}/{len(cookies)}** 全部成功\n- 推送：按 `PUSH_LEVEL=fail_only` 跳过")
        return EXIT_OK

    title = f"GLaDOS签到: 成功{ok_cnt}/{len(cookies)}"
    if auth_failed:
        title += " · Cookie失效"
    content = "".join(results)
    if auth_failed:
        content += ("<br><b>⚠️ Cookie 已失效</b>：请重新登录 glados.cloud 导出 koa:sess / koa:sess.sig，"
                    "更新仓库 Secret GLADOS_COOKIE 后手动运行一次。")
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
        f"- 账号：**{ok_cnt}/{len(cookies)}** 成功",
    ]
    if auth_failed:
        lines.append("- ❌ **Cookie 已失效**，本次签到无效，需人工更新 Secret `GLADOS_COOKIE`")
    for name, ok in outcome.items():
        lines.append(f"- {name} 推送：{'✅ 成功' if ok else '❌ 失败'}")
        if not ok:
            annotate("error", f"{name} 推送失败，请检查 Secrets 配置与机器人状态")
    summary("\n".join(lines))

    # ===== 退出码：让 job 真实反映结果 =====
    if auth_failed:
        log("❌ 退出码 2：Cookie 失效，重试无意义")
        return EXIT_FATAL
    if not all_ok:
        log("❌ 退出码 1：签到未全部成功，可重试")
        return EXIT_RETRYABLE
    log("✅ 退出码 0：全部成功")
    return EXIT_OK


if __name__ == '__main__':
    sys.exit(main())
