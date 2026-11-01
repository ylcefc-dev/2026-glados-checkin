# -*- coding: utf-8 -*-
"""P003 网络诊断 v2：在 GitHub runner 上复刻 checkin.py 的真实请求路径

目的：区分「网络不可达」与「HTTP/1.1 被边缘节点拖慢/挂起」
输出：每个用例的耗时、状态码、异常类型
"""
import os
import sys
import time
import json

import requests

sys.stdout.reconfigure(encoding="utf-8")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

COOKIE_RAW = os.environ.get("GLADOS_COOKIE", "").strip()


def base_headers(cookie=None):
    h = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if cookie:
        h["Cookie"] = cookie
    return h


def probe(label, method, url, headers, timeout=15, data=None):
    t0 = time.time()
    try:
        if method == "GET":
            r = requests.get(url, headers=headers, timeout=timeout)
        else:
            r = requests.post(url, headers=headers, json=data, timeout=timeout)
        cost = time.time() - t0
        body = r.text[:110].replace("\n", " ")
        print(f"[{label:38s}] {r.status_code} {cost:6.2f}s  {body}")
        return cost, r.status_code, body
    except Exception as e:
        cost = time.time() - t0
        print(f"[{label:38s}] ERR {cost:6.2f}s  {type(e).__name__}: {str(e)[:90]}")
        return cost, None, str(e)


print("=" * 78)
print("ENV: requests", requests.__version__, "| cookie_len:", len(COOKIE_RAW))
print("=" * 78)

D = "https://glados.cloud"

# 用例 1：无 Cookie，纯 GET（对照基准）
for i in range(3):
    probe(f"GET status 无Cookie #{i+1}", "GET", f"{D}/api/user/status",
          base_headers())

# 用例 2：带 Cookie，GET（脚本真实路径第一步）
for i in range(3):
    probe(f"GET status 带Cookie #{i+1}", "GET", f"{D}/api/user/status",
          {**base_headers(COOKIE_RAW), "Referer": f"{D}/console/checkin",
           "Origin": D})

# 用例 3：带 Cookie，POST checkin（脚本真实路径第二步）
for i in range(2):
    probe(f"POST checkin 带Cookie #{i+1}", "POST", f"{D}/api/user/checkin",
          {**base_headers(COOKIE_RAW), "Referer": f"{D}/console/checkin",
           "Origin": D}, data={"token": "glados.cloud"})

# 用例 4：超时放宽到 40s，看是否是「慢」而非「不可达」
probe("GET status 带Cookie timeout=40", "GET", f"{D}/api/user/status",
      {**base_headers(COOKIE_RAW), "Referer": f"{D}/console/checkin",
       "Origin": D}, timeout=40)

# 用例 5：备用域名
for dom in ("https://glados.rocks", "https://glados.network"):
    probe(f"GET status {dom.split('//')[1]}", "GET", f"{dom}/api/user/status",
          {**base_headers(COOKIE_RAW), "Referer": f"{dom}/console/checkin",
           "Origin": dom})

print("=" * 78)
print("说明：若『无Cookie』全部快速返回而『带Cookie』超时，")
print("      则瓶颈在带凭据的请求链路，而非网络不可达。")
