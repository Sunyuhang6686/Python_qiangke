# -*- coding: utf-8 -*-
"""
qk_grab.py —— 抢课核心模块（已完善：自动判断成功/失败 + 日志 + 防封）

用法：在项目目录运行
    python qk_grab.py

直接运行转到 qk_list 的统一登录、栏目发现和课程选择流程。
提交结果不明确时只读核验已选列表，无法核实则暂停整轮。
"""

import datetime
import json
import os
import random
import threading

import config
import re
from urllib.parse import urljoin, urlsplit
from workflow import Cancelled, pause, check_response
from limits import SubmissionBudget, per_course

# 日志锁：多线程同时抢课时，防止打印/写文件内容交错
_LOG_LOCK = threading.Lock()

# 最多抢多少次就自动停（防止挂死一直刷）
MAX_ATTEMPTS = per_course()
# 日志文件
LOG_FILE = "grab.log"
# 日志超过这个大小(字节)就归档轮转，防止无限增长
LOG_MAX_BYTES = 1024 * 1024  # 1MB


def _rotate_log_if_needed():
    """日志超过 1MB 时，把当前日志改名 grab.log.old 重新开始。"""
    try:
        if os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
            old = LOG_FILE + ".old"
            if os.path.exists(old):
                os.remove(old)
            os.rename(LOG_FILE, old)
    except OSError:
        pass  # 文件不存在或正在使用，忽略


def log(msg):
    """把消息同时打印出来 + 写进日志文件（带时间戳）。多线程安全。"""
    line = f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    with _LOG_LOCK:
        _rotate_log_if_needed()
        print(line)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def safe_interval(fail_streak=0):
    """计算下一次请求前的等待秒数（防封）：
       在 config 的随机区间里取值；连续异常达到阈值后指数退避(最多8倍)。"""
    lo = getattr(config, "INTERVAL_MIN", 0.6)
    hi = getattr(config, "INTERVAL_MAX", 1.5)
    base = random.uniform(lo, hi)
    threshold = getattr(config, "BACKOFF_AFTER", 5)
    if fail_streak >= threshold:
        factor = min(2 ** (fail_streak - threshold + 1), 8)  # 2,4,8...
        base *= factor
    return base


def grab_once(session, params):
    """发一次选课请求。返回: (状态码, 返回文本)
       请求头尽量模仿真实浏览器，降低被风控识别的概率。"""
    context = getattr(session, "_xk_context", None) or {}
    grab_url = context.get("grab_url", config.GRAB_URL)
    headers = {
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": context.get("referer", config.GRAB_REFERER),
        "Connection": "keep-alive",
    }
    r = session.get(grab_url, params=params, headers=headers, timeout=10)
    r.encoding = r.apparent_encoding
    return r.status_code, r.text


def parse_result(text):
    """
    把服务器返回解析成 (是否成功, 是否确定性失败, 提示信息)。
    服务器返回 JSON：{"success": true, "message": "选课成功", ...}
    success-数组取最后一个元素判断；ok=True 表示选课成功。
    fatal=True 表示"再刷多少次都不可能成功"(如学分超限、已满)，
    这种不该继续死磕，应直接放弃当前课。
    """
    # 确定性失败关键词：遇到这些提示，再怎么抢都不可能成功
    FATAL_KEYS = ("学分不能超过", "学分已超过", "学分不可超过", "不能超过", "已达选课上限",
                  "选课学分", "不能再选", "已达上限", "超过2学分", "不能超过2",
                  "不开放", "未开放", "选课已结束", "不在选课时间")
    try:
        data = json.loads(text)
        s = data.get("success")
        if isinstance(s, list):
            s = s[-1] if s else False
            ok = s is True or s == 1 or (isinstance(s, str) and s.lower() == 'true')
        else:
            ok = s is True or s == 1 or (isinstance(s, str) and s.lower() == 'true')
        msg = data.get("message", "")
        fatal = any(k in msg for k in FATAL_KEYS)
        # 兜底：即使 success 为真，但 message 明确说不成功也算失败
        if ok and msg and any(k in msg for k in ("失败", "已满", "不能", "不允许")):
            ok = False
        return ok, fatal, msg
    except Exception:
        # 返回的不是 JSON。可能是：Cookie 失效(跳登录页)、服务器繁忙、页面出错。
        snippet = text.strip()[:80]
        lower = text.lower()
        fatal = any(k in snippet for k in FATAL_KEYS)
        return False, fatal, snippet


def wait_until(start_str, cancel=None):
    """定时开抢：一直等到指定时间才开始。"""
    target = datetime.datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S")
    log(f"已设置定时，等待 {start_str} 开抢（Ctrl+C 可中断）...")
    while datetime.datetime.now() < target:
        if (target - datetime.datetime.now()).total_seconds() > 3:
            pause(1, cancel)
        else:
            pause(0.05, cancel)


def do_test(session):
    """只发一次请求，把解析结果打出来，确认接口行为。"""
    print("\n目标课程参数:", config.GRAB_PARAMS)
    print("正在发送一次选课请求...")
    status, text = grab_once(session, config.GRAB_PARAMS)
    ok, fatal, msg = parse_result(text)
    print(f"\n【状态码】{status}")
    print(f"【解析结果】成功={ok}  确定性失败={fatal}  提示={msg}")
    print("【原始返回】")
    print(text)


def verify_selected(session, params, cancel=None):
    """从已选页面声明的列表只读核验。缺少证据时返回 None。"""
    try:
        base = config.BASE_URL
        page_url = base + "/jsxsd/xsxkjg/comeXkjglb"
        page = session.get(page_url, timeout=10)
        text = check_response(page)
        sources = re.findall(r"""["']sAjaxSource["']\s*:\s*["']([^"']+)["']""", text)
        urls = list(dict.fromkeys(urljoin(page_url, item) for item in sources))
        urls = [url for url in urls if urlsplit(url).netloc == urlsplit(base).netloc
                and urlsplit(url).path.startswith("/jsxsd/xsxkjg/")
                and not re.search(r"oper|delete|exit|tk", urlsplit(url).path, re.I)]
        if len(urls) != 1:
            return None
        start = 0
        for _ in range(20):
            if cancel and cancel.is_set():
                return None
            r = session.post(urls[0], data={"sEcho":"1", "iDisplayStart":str(start),
                "iDisplayLength":"100"}, headers={"Referer":page_url}, timeout=10)
            data = json.loads(check_response(r))
            rows = data.get("aaData")
            if not isinstance(rows, list):
                return None
            if any(isinstance(row, dict) and str(row.get("jx0404id")) == str(params["jx0404id"])
                   and str(row.get("jx02id", row.get("kcid"))) == str(params["kcid"])
                   for row in rows):
                return True
            if not rows:
                return None
            start += len(rows)
        return None
    except Exception:
        return None


def do_grab(session, params=None, max_attempts=None, emit=None, cancel=None):
    """只有明确失败才重试；提交结果不确定时核验，无法确认就暂停整轮。"""
    params = params or dict(config.GRAB_PARAMS)
    if not params.get("kcid") or not params.get("jx0404id"):
        raise ValueError("课程或教学班 ID 缺失，请重新加载课程")
    if not getattr(session, "_xk_context", None):
        raise ValueError("尚未确认选课入口，请从统一入口加载课程")
    def out(message):
        log(message)
        if emit:
            emit(message)
    def uncertain():
        out("提交结果待确认，正在只读核对已选课程…")
        if cancel and cancel.is_set():
            return "uncertain"
        if verify_selected(session, params, cancel) is True:
            out("已在选课结果中核实该教学班，选课成功")
            return True
        out("暂时无法确认是否选上，本轮暂停。请在教务系统核对后再开始，避免重复提交。")
        return "uncertain"
    attempts = max(1, min(int(max_attempts if max_attempts is not None else per_course()), 30))
    budget = getattr(session, '_grab_budget', None)
    if not isinstance(budget, SubmissionBudget):
        budget = SubmissionBudget()
        session._grab_budget = budget
    out(f"本课程最多 {attempts} 次；整轮额度 {budget.used}/{budget.limit} 次已使用")
    for i in range(1, attempts + 1):
        if cancel and cancel.is_set():
            return "cancelled"
        if not budget.consume():
            out(f"整轮已达 {budget.limit} 次提交上限，停止所有后续课程")
            return "budget_exhausted"
        try:
            status, text = grab_once(session, params)
        except Exception:
            return uncertain()
        if status != 200:
            return uncertain()
        try:
            data = json.loads(text)
            if not isinstance(data, dict) or "success" not in data:
                return uncertain()
        except ValueError:
            return uncertain()
        ok, fatal, msg = parse_result(text)
        if ok:
            if "还有" in msg:
                out("服务器要求继续选择关联教学班，请到教务页面完成。本轮暂停。")
                return "uncertain"
            out(f"选课成功：{msg}")
            return True
        if fatal:
            out(f"本课程停止尝试：{msg}")
            return "skip"
        if "登录" in msg or "login" in msg.lower():
            out("登录状态失效，本轮停止，请重新登录")
            return "uncertain"
        out(f"第 {i}/{attempts} 次未成功：{msg}")
        if i < attempts:
            try:
                pause(safe_interval(), cancel)
            except Cancelled:
                return "cancelled"
    out(f"已达到 {attempts} 次尝试，本课程结束")
    return False


def main():
    # 所有启动路径统一经过登录、类型发现、课程选择。
    from qk_list import main as list_main
    list_main()


if __name__ == "__main__":
    main()
