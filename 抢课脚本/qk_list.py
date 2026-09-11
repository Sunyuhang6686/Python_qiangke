# -*- coding: utf-8 -*-
"""
qk_list.py —— 提前选类别，到点自动抢该类别的网课

用法：
    python qk_list.py

流程：
  1. 输入要抢的类别

（可多个，如 C,E；回车=全部）
  2. 选择模式：
       - 回车/a   = 自动抢该类别的网课：脚本自己找网课，
         按剩余量从多到少逐个抢，一门没抢到就换下一门，
         全部网课都试过还没抢到就停止。
       - m        = 手动模式：列出该类别的课，你输序号自己选
  3. 若 config.py 配了 START_TIME，脚本会等到点再拉列表并开抢
     （选课前列表拉不到也会自动等到点再拉）
"""

import datetime
import json
import re

import config
from workflow import retry_read, check_response, Retryable
import qk_grab
from qk_login import get_session, enter_xk
from limits import SubmissionBudget, per_course

# 拉取列表失败时的重试次数与间隔
FETCH_RETRIES = 3
FETCH_RETRY_WAIT = 2
# 自动模式下，每门网课最多尝试的次数（满员的课试几次就换下一门）
AUTO_TRIES_PER_COURSE = per_course(auto=True)


def _fetch_courses_once(session, cancel=None):
    """调用列表接口，返回所有可选课程的列表（带重试）。"""
    context = getattr(session, "_xk_context", None) or {}
    url = context.get("list_url")
    if not url:
        # 兼容单独调用本模块的旧方式；正常主流程应由 enter_xk 自动提供。
        url = (
            config.BASE_URL + "/jsxsd/xsxkkc/xsxkGgxxkxk"
            "?kcxx=&skls=&skxq=&skjc=&sfym=false&sfct=false&szjylb=&sfxx=true"
        )
    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            all_courses = []
            start = 0
            page_len = 500
            while True:
                if cancel and cancel.is_set():
                    from workflow import Cancelled
                    raise Cancelled()
                post = {
                    "sEcho": "1",
                    "iDisplayStart": str(start),
                    "iDisplayLength": str(page_len),
                    "iColumns": "14",
                    "sColumns": "",
                }
                headers = {"Referer": context.get("referer", config.GRAB_REFERER)}
                r = session.post(url, data=post, headers=headers, timeout=15)
                r.encoding = r.apparent_encoding
                if r.status_code != 200:
                    check_response(r)
                check_response(r)
                try:
                    data = json.loads(r.text)
                except ValueError:
                    raise Retryable("课程列表返回非 JSON，等待服务器恢复")
                if not isinstance(data, dict) or not isinstance(data.get("aaData"), list):
                    raise RuntimeError("返回内容不是课程列表（缺少 aaData 数组）")
                aa = data["aaData"]
                all_courses.extend(aa)
                total = int(data.get("iTotalDisplayRecords") or 0)
                start += len(aa)
                if not aa or start >= total:
                    break
            return all_courses
        except Exception as e:
            raise
    return []


def fetch_courses(session, emit=print, cancel=None, wait_seconds=120):
    return retry_read(lambda: _fetch_courses_once(session, cancel), emit, cancel, wait_seconds)


def remaining_seats(course):
    try:
        value = course.get("syrs")
        return int(value) if value not in (None, "", "&nbsp;") else None
    except (ValueError, TypeError):
        return None


def fetch_with_wait(session):
    """拉列表；拉不到且配了定时时，提示等待。返回课程列表(可能为空)。
       注意：真正等待/进选课在 main 里做，这里只负责拉取。"""
    print("正在拉取可选课程列表...")
    courses = fetch_courses(session)
    if not courses:
        print("当前拉不到课程（可能选课系统还没开放或不在选课时间）。")
    return courses


def get_category(course):
    """从课程名提取类别，如 [C]智能机器人 → 'C'；没有则返回 ''。"""
    m = re.match(r"\[([A-Za-z])\]", course.get("kcmc") or "")
    return m.group(1).upper() if m else ""


def is_net_course(course):
    """网课 = 上课时间为空（&nbsp; / 空字符串）。"""
    sksj = (course.get("sksj") or "").strip()
    return bool(course.get("is_net") is True or
                any(word in str(course.get("kcmc", "")) for word in ("网课", "网络课程", "在线课程")))


def is_conflict(course):
    """是否与已选课程时间冲突。
       课程数据里的 ctsm 字段是服务器给的时间冲突说明(可能含HTML)。"""
    ctsm = course.get("ctsm")
    if ctsm is None:
        return False
    txt = str(ctsm)
    # 去掉HTML标签
    txt = re.sub(r"<[^>]+>", "", txt).replace("&nbsp;", " ").strip()
    # 有实际内容(非空/非&nbsp;/非"&nbsp;")说明存在冲突
    return bool(txt)


def conflict_text(course):
    """冲突的显示文本；无冲突返回空串。"""
    ctsm = course.get("ctsm")
    if not ctsm:
        return ""
    txt = re.sub(r"<[^>]+>", "", str(ctsm)).replace("&nbsp;", " ").strip()
    return txt


def clean(txt, as_net=False):
    """清理显示文本；as_net=True 时把空时间显示为"网课"。"""
    if txt is None:
        return "未知"
    s = str(txt).replace("&nbsp;", "").strip()
    if not s:
        return "未知"
    return s


def filter_by_categories(courses, cat_str):
    """按类别过滤，支持多个类别如 'C,E'；空则返回全部。"""
    if not cat_str:
        return courses
    cats = {x.strip().upper() for x in cat_str.replace("，", ",").split(",") if x.strip()}
    return [c for c in courses if get_category(c) in cats]


def show_courses(courses):
    print(f"\n共 {len(courses)} 门课程：")
    print("-" * 100)
    for i, c in enumerate(courses, 1):
        kcmc = clean(c.get("kcmc"))
        skls = clean(c.get("skls"))
        sksj = clean(c.get("sksj"), as_net=True)
        syrs = clean(c.get("syrs"))
        cf = " 冲突!" if is_conflict(c) else ""
        print(f"{i:>3}. {kcmc:<24} 老师:{skls:<8} 时间:{sksj:<14} 剩余:{syrs}{cf}")
    print("-" * 100)


def choose_manually(filtered):
    """手动模式：输序号选择，返回选中的课程列表。"""
    while True:
        raw = input("输入要抢的序号(逗号分隔)，或 q 退出: ").strip()
        if raw.lower() == "q":
            return []
        try:
            indices = [int(x) for x in raw.replace("，", ",").split(",")]
            chosen = []
            for idx in indices:
                if not (1 <= idx <= len(filtered)):
                    print(f"序号 {idx} 超出范围（共 {len(filtered)} 门），请重新输入。")
                    break
                chosen.append(filtered[idx - 1])
            else:
                return chosen
        except ValueError:
            print("输入不合法，请只输入数字和逗号。")


def to_grab_params(course):
    """把列表里的一门课，转成抢课接口需要的参数。
       关键：列表里的 jx02id 就是选课接口的 kcid（已在页面 JS 确认）。"""
    return {
        "kcid": course.get("jx02id"),
        "cfbs": course.get("cfbs") or "null",
        "jx0404id": course.get("jx0404id"),
        "xkzy": "",
        "trjf": "",
    }


def auto_grab_net_courses(session, filtered, cat="", emit=None):
    """自动模式：找该类别的所有网课，按剩余量从多到少逐个抢。
       每门最多试 AUTO_TRIES_PER_COURSE 次，一门没抢到就换下一门，
       全部网课都试过还没抢到就停止。cat 用于日志显示类别。
       emit: 可选回调(每行进度)，默认 None=打印到控制台。"""
    def out(m):
        print(m)
        if emit:
            try:
                emit(m)
            except Exception:
                pass
    # 只看网课，剩余量>0，且不冲突的才抢
    nets = [c for c in filtered
            if is_net_course(c) and remaining_seats(c) is not None and remaining_seats(c) > 0
            and not is_conflict(c)]
    if not nets:
        out((f"[类别{cat}] 该类别的网课都已满员/冲突，全部跳过，停止。"
             if cat else "该类别的网课都已满员/冲突，全部跳过，停止。"))
        return
    # 剩余量多的优先（机会更大）
    nets.sort(key=lambda c: remaining_seats(c), reverse=True)

    tag = f"[类别{cat}] " if cat else ""
    out(f"{tag}共 {len(nets)} 门可抢网课(已排除满员与时间冲突)，按剩余量从多到少逐个抢；")
    out(f"{tag}每门最多试 {AUTO_TRIES_PER_COURSE} 次，一门没抢到就换下一门，全都没有就停止。\n")

    for c in nets:
        kcmc = clean(c.get("kcmc"))
        syrs = clean(c.get("syrs"))
        out(f"{tag}===== 尝试《{kcmc}》(剩余 {syrs}) =====")
        res = qk_grab.do_grab(session, to_grab_params(c),
                              max_attempts=AUTO_TRIES_PER_COURSE, emit=emit)
        if res is True:
            out(f"{tag}《{kcmc}》🎉 已抢到网课，停止。\n")
            return
        if res in ("uncertain", "cancelled", "budget_exhausted"):
            return res
        if res == "skip":
            out(f"{tag}《{kcmc}》确定性失败(如学分超限)，跳过，换下一门...\n")
        else:
            out(f"{tag}《{kcmc}》没抢到，换下一门...\n")
    out(f"{tag}所有网课都试过了，均未成功，停止。")


def run_auto_concurrent(session, groups):
    """同一账号顺序提交，结果不确定时停止所有后续类别。"""
    if not groups:
        print("没有可抢的类别。")
        return
    for cat, group in groups.items():
        result = auto_grab_net_courses(session, group, cat)
        if result in ("uncertain", "cancelled", "budget_exhausted"):
            return result


def _preselect():
    """预选：输入类别 + 模式。返回 (cat_input, mode)。"""
    cat_input = input(
        "输入要抢的类别(可多个用逗号，如 C,E；回车=全部): "
    ).strip().upper()
    mode = input(
        "回车/a=按类别自动选网课 | 输入 m=手动列课: "
    ).strip().lower()
    return cat_input, "m" if mode == "m" else "a"


def _enter_or_fail(session, auto=False):
    from selection import discover_entries
    try:
        def find():
            items = discover_entries(session)
            if auto:
                items = [item for item in items if 'ggxxkxk' in item['entry_url'].lower()]
                if not items:
                    raise Retryable("通选课栏目尚未出现，等待开放")
            return items
        choices = retry_read(find)
        if auto and len(choices) > 1:
            raise ValueError("存在多个通选课批次，请使用网页版提前选定目标批次")
        chosen = choices[0] if len(choices) == 1 else None
        if len(choices) > 1:
            for i, item in enumerate(choices, 1):
                print(f"{i}. {item['name']} / 批次 {item['batch_id']}")
            while True:
                raw = input("请选择目标栏目序号（q 退出）: ").strip()
                if raw.lower() == "q":
                    return False
                if raw.isdigit() and 1 <= int(raw) <= len(choices):
                    chosen = choices[int(raw) - 1]
                    break
        enter_xk(session, batch_id=chosen['batch_id'] if chosen else None,
                 entry_url_target=chosen['entry_url'] if chosen else None)
    except Exception as exc:
        print(f"无法建立选课会话：{exc}")
        return False
    return True


def main():
    # 获取会话：有有效 Cookie 用 Cookie，否则账号+密码+验证码完整登录
    print("正在登录教务系统...")
    try:
        session = get_session()
    except Exception as e:
        print(f"登录失败: {e}")
        return

    # 判断是否配了定时、且还没到点
    now = datetime.datetime.now()
    has_timer = bool(config.START_TIME)
    not_yet = False
    if has_timer:
        try:
            target = datetime.datetime.strptime(config.START_TIME, "%Y-%m-%d %H:%M:%S")
            not_yet = now < target
        except ValueError:
            print("START_TIME 格式不对，应为 2026-01-01 12:00:00。请修正后再启动。")
            return

    # ============ 有定时且未到点：先预选 → 等待 → 到点才进选课 ============
    if has_timer and not_yet:
        print(f"⏰ 已设置 {config.START_TIME} 开抢，当前 {now}，先预选再等待。")
        cat_input, mode = _preselect()
        print(f"\n已记录预选：类别={cat_input or '全部'}，模式={'自动抢网课' if mode != 'm' else '手动选课'}")
        print(f"等待到 {config.START_TIME} 再进入选课系统开抢（Ctrl+C 中断）...")
        qk_grab.wait_until(config.START_TIME)
        print("⏰ 时间到！正在进入选课系统...")
        if not _enter_or_fail(session, auto=mode != 'm'):
            return
    else:
        # ============ 立即抢：先进选课系统，再预选 ============
        cat_input, mode = _preselect()
        if not _enter_or_fail(session, auto=mode != 'm'):
            return

    # ============ 到点后：拉列表 → 按预选抢 ============
    while True:
        session._grab_budget = SubmissionBudget()
        try:
            courses = fetch_with_wait(session)
        except Exception as exc:
            print(f"列表加载停止：{exc}")
            return
        if not courses:
            print("拉不到课程：可能选课系统刚开放还没数据，稍等再试。")
            return

        if "ggxxkxk" not in session._xk_context["entry_url"].lower():
            cat_input = ""
        filtered = filter_by_categories(courses, cat_input)
        if not filtered:
            print("所选类别下没有课程。")
            return

        context = session._xk_context
        if "ggxxkxk" not in context["entry_url"].lower():
            mode = "m"
            filtered = courses
            print("当前为必修等栏目，使用手动选课；忽略通选课类别过滤。")
        if mode == "m":
            show_courses(filtered)
            chosen = choose_manually(filtered)
            if not chosen:
                print("已退出。")
                return
            print(f"\n你选择了 {len(chosen)} 门课，开始抢课：")
            for c in chosen:
                kcmc = clean(c.get("kcmc"))
                print(f"\n===== 开始抢《{kcmc}》 =====")
                res = qk_grab.do_grab(session, to_grab_params(c))
                if res is True:
                    print(f"《{kcmc}》已抢到 ✔")
                elif res in ("uncertain", "cancelled", "budget_exhausted"):
                    print("本轮已停止，请查看上方原因。")
                    return
                elif res == "skip":
                    print(f"《{kcmc}》确定性失败(如学分超限)，放弃此课")
                else:
                    print(f"《{kcmc}》未抢到 ✘")
        else:
            # 沿用原自动网课规则，到点后不再等待用户输入。
            for c in filtered:
                if (c.get("sksj") or "").strip() in ("", "&nbsp;"):
                    c["is_net"] = True
            # 自动模式：同一账号按类别顺序执行
            if cat_input:
                cats = {x.strip().upper()
                        for x in cat_input.replace("，", ",").split(",") if x.strip()}
            else:
                cats = {get_category(c) for c in filtered if get_category(c)}
            groups = {cat: [c for c in filtered if get_category(c) == cat]
                      for cat in cats}
            if run_auto_concurrent(session, groups) in ("uncertain", "cancelled", "budget_exhausted"):
                return

        # 本轮结束：询问是否继续抢课（继续则重新预选）
        again = input("\n本轮抢课结束。还要继续抢课吗？(回车=继续，输入 n/q=退出): ").strip().lower()
        if again in ("n", "q", "exit", "退出"):
            break
        print()  # 空一行，开始新一轮
        cat_input, mode = _preselect()  # 重新预选

    print("\n全部完成。")


if __name__ == "__main__":
    main()
