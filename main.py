from flask import Flask, request, make_response
import xml.etree.ElementTree as ET
import json
import time
from datetime import datetime, timedelta, date
import os
import random
import string
import threading, requests, time

app = Flask(__name__)

# ---------------------
# 文件与配置
# ---------------------
GAMES_FILE = "games.json"
VERIFIED_FILE = "verified_users.json"
ADMIN_FILE = "admin_users.json"
SUPER_ADMIN_FILE = "super_admins.json"
FAILED_FILE = "failed_attempts.json"
ONE_TIME_FILE = "one_time_codes.json"
DAILY_COUNT_FILE = "user_daily_count.json"

MAX_DAILY = 10
ADMIN_BIND_CODE = "asdfg123456"
SUPER_ADMIN_CODES = {"super123456"}

# ---------------------
# 文件持久化
# ---------------------
def load_json_file(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"⚠️ load {path} failed:", e)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return default

def save_json_file(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ save {path} failed:", e)

# ---------------------
# 加载数据
# ---------------------
GAMES = load_json_file(GAMES_FILE, [])
verified_users = set(load_json_file(VERIFIED_FILE, []))
ADMIN_USERS = set(load_json_file(ADMIN_FILE, []))
SUPER_ADMINS = set(load_json_file(SUPER_ADMIN_FILE, []))
failed_attempts = load_json_file(FAILED_FILE, {})
one_time_codes = load_json_file(ONE_TIME_FILE, {})
user_daily_count = load_json_file(DAILY_COUNT_FILE, {})
first_visit_users = set()  # 新增集合，用于标记第一次访问的普通用户

# ---------------------
# 时间与锁定管理
# ---------------------
def get_now_ts():
    return int(time.time())

def seconds_to_readable(s):
    if s is None:
        return "永久"
    s = int(s)
    if s < 60:
        return f"{s} 秒"
    if s < 3600:
        return f"{s//60} 分钟"
    if s < 86400:
        return f"{s//3600} 小时"
    return f"{s//86400} 天"

LOCK_LEVELS = [10, 30, 5*60, 2*60*60, None]  # 秒数，最后永久

def get_lock_info(openid):
    info = failed_attempts.get(openid)
    if not info:
        return {"fail_count": 0, "lock_until": None, "blocked": False}
    return {
        "fail_count": info.get("fail_count", 0),
        "lock_until": info.get("lock_until"),
        "blocked": info.get("blocked", False)
    }

def save_failed_info(openid, info):
    failed_attempts[openid] = info
    save_json_file(FAILED_FILE, failed_attempts)

def record_failed_attempt(openid):
    info = get_lock_info(openid)
    fail_count = info["fail_count"] + 1
    level_index = min(fail_count - 1, len(LOCK_LEVELS)-1)
    lock_seconds = LOCK_LEVELS[level_index]
    if lock_seconds is None:
        info = {"fail_count": fail_count, "lock_until": None, "blocked": True}
    else:
        info = {"fail_count": fail_count, "lock_until": get_now_ts()+lock_seconds, "blocked": False}
    save_failed_info(openid, info)
    return info

def clear_failed_attempts(openid):
    if openid in failed_attempts:
        failed_attempts.pop(openid, None)
        save_json_file(FAILED_FILE, failed_attempts)
    # 重置每日计数
    reset_daily_if_needed(openid)

def is_locked(openid):
    info = get_lock_info(openid)
    if info["blocked"]:
        return True, "永久封禁"
    lock_until = info["lock_until"]
    if lock_until:
        now = get_now_ts()
        if now < lock_until:
            remaining = lock_until - now
            return True, seconds_to_readable(remaining)
    return False, None

# ---------------------
# 计数管理
# ---------------------
def today_str_beijing():
    return (datetime.utcnow() + timedelta(hours=8)).strftime("%Y%m%d")

def reset_daily_if_needed(openid):
    today = today_str_beijing()
    info = user_daily_count.get(openid)
    if not info or info.get("date") != today:
        user_daily_count[openid] = {"date": today, "count": 0}
        save_json_file(DAILY_COUNT_FILE, user_daily_count)

def increment_daily(openid):
    reset_daily_if_needed(openid)
    user_daily_count[openid]["count"] += 1
    save_json_file(DAILY_COUNT_FILE, user_daily_count)

def get_daily_count(openid):
    reset_daily_if_needed(openid)
    return user_daily_count[openid]["count"]

def remaining_quota(openid):
    reset_daily_if_needed(openid)
    used = user_daily_count[openid]["count"]
    return max(0, MAX_DAILY - used)

# ---------------------
# 验证码生成
# ---------------------
def generate_date_code(for_date: date = None):
    if not for_date:
        now_utc = datetime.utcnow()
        now = now_utc + timedelta(hours=8)
        d = now.date()
    else:
        d = for_date
    yy = f"{d.year:04d}"[-2:]
    mm = f"{d.month:02d}"
    dd = f"{d.day + 1:02d}"
    return f"15{yy}{mm}{dd}"

def generate_one_time_code(length=12):
    alphabet = string.ascii_letters + string.digits
    return ''.join(random.choice(alphabet) for _ in range(length))

# ---------------------
# 游戏搜索
# ---------------------
def search_game(keyword):
    results = []
    for g in GAMES:
        if keyword in g["name"]:
            results.append(g)
    return results if results else None

# ---------------------
# 微信 XML 回复
# ---------------------
def reply_xml(to_user, from_user, content):
    return f"""
<xml>
    <ToUserName><![CDATA[{to_user}]]></ToUserName>
    <FromUserName><![CDATA[{from_user}]]></FromUserName>
    <CreateTime>{int(time.time())}</CreateTime>
    <MsgType><![CDATA[text]]></MsgType>
    <Content><![CDATA[{content}]]></Content>
</xml>
"""

# ---------------------
# 帮助文本
# ---------------------
ADMIN_HELP_TEXT = """🎯 管理员操作指南：
1. 生成一次性验证码：发送 “生成验证码”
2. 查询游戏：直接发送游戏名
3. 新增管理员（超级管理员专用）：发送 “新增管理员<openid>”
4. 删除管理员（超级管理员专用）：发送 “删除管理员<openid>”
5. 解封用户（超级管理员专用）：发送 “解封<openid>”
6. 查询自己的 ID：发送 “查询 ID”
"""

# ---------------------
# 超级管理员绑定
# ---------------------
def handle_super_admin_bind(openid, text):
    if text in SUPER_ADMIN_CODES:
        if openid not in SUPER_ADMINS:
            SUPER_ADMINS.add(openid)
            save_json_file(SUPER_ADMIN_FILE, list(SUPER_ADMINS))
            return "✅ 您已绑定为超级管理员，可增删管理员"
        else:
            return "ℹ️ 您已是超级管理员"
    return None

# ---------------------
# 超级管理员命令
# ---------------------
def handle_super_admin_commands(openid, text):
    if openid not in SUPER_ADMINS:
        return None
    text = text.strip()
    if text.startswith("新增管理员"):
        target = text[5:].strip()
        if target:
            ADMIN_USERS.add(target)
            save_json_file(ADMIN_FILE, list(ADMIN_USERS))
            return f"✅ {target} 已被添加为管理员"
        return "⚠️ 请提供管理员 OpenID"
    elif text.startswith("删除管理员"):
        target = text[5:].strip()
        if target:
            ADMIN_USERS.discard(target)
            save_json_file(ADMIN_FILE, list(ADMIN_USERS))
            return f"✅ {target} 已被移除管理员"
        return "⚠️ 请提供管理员 OpenID"
    elif text.startswith("解封"):
        target = text[2:].strip()
        if target:
            clear_failed_attempts(target)
            return f"✅ {target} 已被解封"
        return "⚠️ 请提供要解封的 OpenID"
    return None

# ---------------------
# 微信入口
# ---------------------
@app.route("/", methods=["GET", "POST"])
def wechat():
    if request.method == "GET":
        return request.args.get("echostr", "")

    xml_data = request.data
    xml = ET.fromstring(xml_data)

    from_user = xml.find("FromUserName").text
    to_user = xml.find("ToUserName").text
    content = xml.find("Content").text.strip()

    reply = handle_message(from_user, content)
    return make_response(reply_xml(from_user, to_user, reply))

# ---------------------
# 核心处理逻辑
# ---------------------
def handle_message(openid, text):
    text = text.strip()

    # 查询自己的 ID（普通用户/管理员/超级管理员都可）
    if text == "查询 ID":
        locked, reason = is_locked(openid)
        if locked:
            return f"您的 OpenID：{openid}（锁定中，原因：{reason}）"
        return f"您的 OpenID：{openid}"

    # 超级管理员绑定
    reply = handle_super_admin_bind(openid, text)
    if reply: return reply

    # 超级管理员命令
    reply = handle_super_admin_commands(openid, text)
    if reply: return reply

    # 管理员帮助
    if text == "帮助":
        if openid in ADMIN_USERS or openid in SUPER_ADMINS:
            return ADMIN_HELP_TEXT
        return ""  # 普通用户无任何提示

    # 检查锁定状态
    locked, reason = is_locked(openid)
    if locked:
        # 普通用户被锁定，除了查询 ID，其余不回复
        if text != "查询 ID":
            return ""
        return f"您的 OpenID：{openid}（锁定中，原因：{reason}）"

    # 管理员绑定
    if text == ADMIN_BIND_CODE:
        if openid not in ADMIN_USERS:
            ADMIN_USERS.add(openid)
            save_json_file(ADMIN_FILE, list(ADMIN_USERS))
            return "✅ 管理员绑定成功"
        else:
            return "ℹ️ 您已是管理员"

    if openid in ADMIN_USERS:
        # 生成一次性验证码
        if text.lower() == "生成验证码":
            code = generate_one_time_code()
            ts = get_now_ts()
            one_time_codes[code] = {"creator": openid, "created_at": ts, "used": False, "used_by": None}
            save_json_file(ONE_TIME_FILE, one_time_codes)
            return f"🔑 管理员生成的一次性验证码：{code} （24小时内有效）"

        # 管理员解封
        elif text.startswith("解封"):
            target = text[2:].strip()
            if target:
                clear_failed_attempts(target)
                return f"✅ {target} 已被解封"
            return "⚠️ 请提供要解封的 OpenID"

        # 查询游戏
        else:
            results = search_game(text)
            if results:
                msg_list = [f"{r['name']}\n下载链接：{r['url']}\n提取码：{r.get('password','无')}" for r in results[:5]]
                return "🎮 管理员模式：找到以下内容：\n\n" + "\n\n".join(msg_list)
            return "❌ 未找到匹配游戏（管理员模式）"

    # 普通用户第一次访问
    if openid not in verified_users and openid not in first_visit_users:
        first_visit_users.add(openid)
        return "感谢您的关注，无自动回复功能，请输入验证码以继续"

    # 普通用户验证码验证
    if openid not in verified_users:
        now_ts = get_now_ts()
        date_code = generate_date_code()

        # 检查一次性码
        for code, info in one_time_codes.items():
            if info["used"]:
                continue
            if code == text:
                if now_ts - info["created_at"] > 86400:
                    info["used"] = True
                    save_json_file(ONE_TIME_FILE, one_time_codes)
                    info_lock = record_failed_attempt(openid)
                    return f"❌ 验证码已过期，锁定 {seconds_to_readable(info_lock['lock_until'] - now_ts) if info_lock['lock_until'] else '永久'}"
                else:
                    info["used"] = True
                    info["used_by"] = openid
                    save_json_file(ONE_TIME_FILE, one_time_codes)
                    verified_users.add(openid)
                    save_json_file(VERIFIED_FILE, list(verified_users))
                    clear_failed_attempts(openid)
                    return "✅ 验证成功（一次性码）！您现在可以发送游戏名查询"

        # 日期码验证
        if text == date_code:
            verified_users.add(openid)
            save_json_file(VERIFIED_FILE, list(verified_users))
            clear_failed_attempts(openid)
            return "✅ 验证成功（日期码）！您现在可以发送游戏名查询"

        # 验证错误，记录失败
        info_lock = record_failed_attempt(openid)
        locked_msg = seconds_to_readable((info_lock['lock_until'] - now_ts) if info_lock['lock_until'] else None)
        return f"❌ 验证码错误，锁定 {seconds_to_readable(info_lock['lock_until'] - now_ts) if info_lock['lock_until'] else '永久'}"

    # 普通用户已验证，查询游戏
    if get_daily_count(openid) >= MAX_DAILY:
        return f"⚠️ 每日查询次数已达 {MAX_DAILY} 次，请明天再来"

    results = search_game(text)
    increment_daily(openid)
    remaining = remaining_quota(openid)
    if results:
        msg_list = [f"{r['name']}\n下载链接：{r['url']}\n提取码：{r.get('password','无')}" for r in results[:5]]
        return ("资源来源于网络，仅作整理学习使用！\n\n"
                "🎮 找到以下内容：\n\n" + "\n\n".join(msg_list) +
                f"\n\n💡 今日剩余查询次数：{remaining}")
    else:
        return f"❌ 未找到匹配游戏，请检查名称。\n💡 今日剩余查询次数：{remaining}"

def keep_alive():
    url = "https://e9f4873b-46c2-47b1-ad31-29637f6f8916-00-3luzrqbq9s2vt.pike.replit.dev/"
    def ping():
        while True:
            try:
                requests.get(url)
            except:
                pass
            time.sleep(300)  # 每5分钟访问一次
    threading.Thread(target=ping, daemon=True).start()
# ---------------------
# 启动
# ---------------------
if __name__ == "__main__":
    keep_alive()
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)