# -*- coding: utf-8 -*-
"""运行 python web_server.py，在浏览器访问 http://127.0.0.1:5000。"""
import base64
import copy
import datetime
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, request, jsonify, render_template
import config
import qk_login
import qk_list
import qk_grab
from selection import discover_entries
from workflow import Cancelled, LoginExpired, Retryable, retry_read
from limits import SubmissionBudget, per_course

app = Flask(__name__)
_sessions = {}
_progress = {}
_guard = threading.RLock()
INSTANCE_ID = uuid.uuid4().hex


class SessionLost(ValueError):
    pass


def holder_for(sid):
    if not sid or sid not in _sessions:
        raise SessionLost("本站会话已丢失（可能是服务重启或请求切换进程），请重新登录")
    return _sessions[sid]


def busy(holder):
    task = _progress.get(holder.get("tid"))
    return holder.get("loading", False) or bool(task and not task["done"])


def log(task, message):
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"[{stamp}] {message}"
    with _guard:
        task["lines"].append(line)
        task["state"] = message
        # 保留原来的账号独立日志，刷新恢复则使用内存中的增量记录。
        if task.get("uid"):
            safe = ''.join(c for c in task['uid'] if c.isalnum() or c in '-_@.')
            folder = Path(__file__).parent / 'logs'
            try:
                folder.mkdir(exist_ok=True)
                with (folder / f"{safe}_{task['sid'][:8]}.log").open('a', encoding='utf-8') as file:
                    file.write(line + '\n')
            except OSError:
                pass


@app.errorhandler(ValueError)
def invalid(exc):
    return jsonify(ok=False, msg=str(exc)), 400


@app.errorhandler(SessionLost)
def session_lost(exc):
    return jsonify(ok=False, code="SESSION_LOST", msg=str(exc)), 409


@app.after_request
def response_headers(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-App-Instance'] = INSTANCE_ID
    return response


@app.route("/")
def index():
    return render_template("index.html")


@app.post("/state")
def state():
    data = request.get_json() or {}
    with _guard:
        sid = data.get("sid")
        reset = bool(sid and sid not in _sessions)
        if sid not in _sessions:
            sid = uuid.uuid4().hex
            _sessions[sid] = {"logged":False}
        h = _sessions[sid]
        # 完成任务保留一天以供刷新恢复，之后清理。
        for tid, task in list(_progress.items()):
            if task["done"] and time.time() - task.get("end", time.time()) > 86400:
                _progress.pop(tid, None)
        return jsonify(ok=True, sid=sid, reset=reset, instance=INSTANCE_ID, logged=h.get("logged",False),
            tid=h.get("tid") if h.get("tid") in _progress else None,
            choices=h.get("choices",[]), courses=h.get("public_courses",[]),
            context=h.get("context"), snapshot=h.get("snapshot"), busy=busy(h))


@app.post("/captcha")
def captcha():
    data = request.get_json() or {}
    sid = data.get("sid")
    with _guard:
        h = holder_for(sid)
        if busy(h):
            raise ValueError("请先停止当前任务")
        h["loading"] = True
    try:
        s = qk_login._new_session()
        s.get(config.BASE_URL + "/jsxsd/", timeout=10)
        r = s.get(config.BASE_URL + "/jsxsd/verifycode.servlet", timeout=10)
        r.raise_for_status()
        with _guard:
            h.clear()
            h.update(session=s, logged=False)
        return jsonify(ok=True, img="data:image/png;base64," + base64.b64encode(r.content).decode())
    except Exception as exc:
        return jsonify(ok=False, msg=f"获取验证码失败：{type(exc).__name__}")
    finally:
        h["loading"] = False


@app.post("/login")
def login():
    data = request.get_json() or {}
    with _guard:
        h = holder_for(data.get("sid"))
        if busy(h):
            raise ValueError("正在处理，请稍候")
        if "session" not in h:
            raise ValueError("请先获取验证码")
        h["loading"] = True
    try:
        u, p = data.get("username","").strip(), data.get("password","")
        if not u or not p:
            raise ValueError("请输入账号和密码")
        encoded = base64.b64encode(u.encode()).decode() + "%%%" + base64.b64encode(p.encode()).decode()
        r = h["session"].post(config.BASE_URL + "/jsxsd/xk/LoginToXk", data={
            "userAccount":u, "userPassword":p, "encoded":encoded, "RANDOMCODE":data.get("code","").strip()}, timeout=10)
        r.encoding = r.apparent_encoding
        h["logged"] = r.status_code == 200 and "xsMain" in r.url
        if h["logged"]:
            h["username"] = u
        return jsonify(ok=h["logged"], msg="登录成功" if h["logged"] else "登录失败，请检查账号、密码或刷新验证码")
    except ValueError:
        raise
    except Exception as exc:
        return jsonify(ok=False, msg=f"登录请求失败：{type(exc).__name__}")
    finally:
        h["loading"] = False


@app.post("/entries")
def entries():
    h = holder_for((request.get_json() or {}).get("sid"))
    with _guard:
        if not h.get("logged") or busy(h):
            raise ValueError("请先登录，并等待当前操作结束")
        h["loading"] = True
    try:
        choices = discover_entries(h["session"])
        h["choices"] = choices
        return jsonify(ok=True, choices=choices)
    except Exception as exc:
        return jsonify(ok=False, msg=str(exc))
    finally:
        h["loading"] = False


@app.post("/relogin")
def relogin():
    with _guard:
        h = holder_for((request.get_json() or {}).get("sid"))
        if busy(h):
            raise ValueError("请先停止当前任务")
        h.clear()
        h["logged"] = False
    return jsonify(ok=True)


def public_course(course, token):
    return dict(token=token, name=qk_list.clean(course.get("kcmc")),
        teacher=qk_list.clean(course.get("skls")), time=qk_list.clean(course.get("sksj")),
        seats=qk_list.remaining_seats(course), conflict=qk_list.conflict_text(course),
        category=qk_list.get_category(course))


@app.post("/start")
def start():
    data = request.get_json() or {}
    sid = data.get("sid")
    with _guard:
        h = holder_for(sid)
        if not h.get("logged"):
            raise ValueError("请先登录")
        if busy(h):
            raise ValueError("已有任务正在运行，请勿重复启动")
        mode = data.get("mode", "load")
        if mode not in ("load", "manual", "auto"):
            raise ValueError("不支持的操作")
        scheduled = data.get("start_time", "").strip()
        if scheduled:
            try:
                datetime.datetime.fromisoformat(scheduled)
            except ValueError:
                raise ValueError("定时时间格式无效")
        targets = []
        context = None
        if mode in ("load", "auto"):
            choice = data.get("choice")
            if choice is not None:
                if not isinstance(choice, int) or not 0 <= choice < len(h.get("choices",[])):
                    raise ValueError("请重新选择批次")
                context = copy.deepcopy(h["choices"][choice])
            if mode == "auto":
                cats = {c.strip().upper() for c in data.get("cat", "").replace("，", ",").split(",") if c.strip()}
                if not cats or any(len(c) != 1 or not c.isascii() or not c.isalpha() for c in cats):
                    raise ValueError("请填写通选课类别，例如 C,E")
                if context and 'ggxxkxk' not in context['entry_url'].lower():
                    raise ValueError("自动网课请选通选课栏目，或选择自动发现")
        else:
            if not h.get("snapshot") or data.get("snapshot") != h["snapshot"]:
                raise ValueError("列表已更新，请重新勾选课程")
            context = copy.deepcopy(h["context"])
            if mode == "manual":
                tokens = data.get("tokens", [])
                if not tokens or len(set(tokens)) != len(tokens):
                    raise ValueError("请选择课程且不要重复")
                for token in tokens:
                    if token not in h["courses"]:
                        raise ValueError("课程已失效，请重新加载")
                    targets.append(copy.deepcopy(h["courses"][token]))
        tid = uuid.uuid4().hex
        task = dict(sid=sid, uid=h.get("username"), lines=[], done=False, cancel=threading.Event(),
                    state="准备中", start_time=scheduled, result=None, mode=mode)
        if mode == 'auto':
            task['categories'] = sorted(cats)
            task['allow_empty_time'] = data.get('allow_empty_time', True) is True
        _progress[tid] = task
        h["tid"] = tid
        thread = threading.Thread(target=run_task, args=(h, task, mode, context, targets, scheduled), daemon=True)
        thread.start()
    return jsonify(ok=True, tid=tid)


def run_task(h, task, mode, context, targets, scheduled):
    emit = lambda message: log(task, message)
    cancel = task["cancel"]
    try:
        if scheduled:
            target = datetime.datetime.fromisoformat(scheduled)
            emit(f"等待至 {target:%Y-%m-%d %H:%M:%S}")
            qk_grab.wait_until(target.strftime("%Y-%m-%d %H:%M:%S"), cancel)
        if cancel.is_set():
            raise Cancelled()
        emit("正在验证批次和课程栏目")
        s = h["session"]
        s._grab_budget = SubmissionBudget()
        if mode != 'load':
            emit(f"每门最多 {per_course(mode == 'auto')} 次，整轮最多 {s._grab_budget.limit} 次提交")
        if mode in ("load", "auto") and context is None:
            def find_choices():
                items = discover_entries(s, emit, cancel)
                if mode == 'auto':
                    items = [item for item in items if 'ggxxkxk' in item['entry_url'].lower()]
                if not items:
                    raise Retryable("批次暂未出现，等待开放")
                return items
            choices = retry_read(find_choices, emit, cancel)
            h["choices"] = choices
            if len(choices) > 1:
                raise ValueError("发现多个栏目，请先选择目标栏目再加载")
            if len(choices) == 1:
                context = choices[0]
        qk_login.enter_xk(s, batch_id=context.get("batch_id") if context else None,
            entry_url_target=context.get("entry_url") if context else None,
            emit=emit, cancel=cancel)
        actual = copy.deepcopy(s._xk_context)
        if mode == "load":
            emit("正在加载课程")
            courses = qk_list.fetch_courses(s, emit, cancel)
            mapping = {uuid.uuid4().hex:c for c in courses if c.get("jx02id") and c.get("jx0404id")}
            public = [public_course(c,t) for t,c in mapping.items()]
            with _guard:
                h.update(courses=mapping, public_courses=public, context=actual, snapshot=uuid.uuid4().hex)
            task["result"] = "loaded"
            emit(f"已加载 {len(public)} 门课程，请勾选目标；空列表表示当前未返回课程")
        else:
            # 重新验证固定批次/栏目，提交的课程仍使用原快照 ID，绝不按新列表序号选。
            if actual["batch_id"] != context["batch_id"] or actual["entry_url"] != context["entry_url"]:
                raise ValueError("批次或栏目发生变化，请重新加载课程")
            done_categories = set()
            if mode == "auto":
                emit("到点重新加载通选课列表，目标类别：" + ','.join(task['categories']))
                courses = qk_list.fetch_courses(s, emit, cancel)
                targets = [copy.deepcopy(c) for c in courses
                           if qk_list.get_category(c) in task['categories']]
                if task['allow_empty_time']:
                    for c in targets:
                        if (c.get('sksj') or '').strip() in ('', '&nbsp;'):
                            c['is_net'] = True
                targets = [c for c in targets if qk_list.is_net_course(c) and
                    qk_list.remaining_seats(c) is not None and qk_list.remaining_seats(c) > 0
                    and not qk_list.is_conflict(c)]
                targets.sort(key=lambda c: qk_list.remaining_seats(c), reverse=True)
            if not targets:
                emit("没有符合条件的课程；时间或余量未知的课程请手动选择")
            for c in targets:
                if cancel.is_set():
                    raise Cancelled()
                category = qk_list.get_category(c)
                if mode == "auto" and category in done_categories:
                    continue
                emit(f"目标：{qk_list.clean(c.get('kcmc'))} / {qk_list.clean(c.get('skls'))}")
                result = qk_grab.do_grab(s, qk_list.to_grab_params(c),
                    max_attempts=per_course(mode == "auto"), emit=emit, cancel=cancel)
                if result in ("uncertain", "cancelled", "budget_exhausted"):
                    task["result"] = result
                    break
                if result is True:
                    done_categories.add(category)
            else:
                task["result"] = "finished"
    except Cancelled:
        task["result"] = "cancelled"
        emit("任务已停止")
    except LoginExpired as exc:
        h["logged"] = False
        task["result"] = "error"
        emit(str(exc))
    except Exception as exc:
        task["result"] = "error"
        emit(str(exc))
    finally:
        with _guard:
            task["done"] = True
            task["end"] = time.time()
        emit("本轮结束")


@app.post("/stop")
def stop():
    data = request.get_json() or {}
    with _guard:
        task = _progress.get(data.get("tid"))
        if not task or task["sid"] != data.get("sid"):
            raise ValueError("任务不存在")
        task["cancel"].set()
    return jsonify(ok=True, msg="已请求停止；正在发送的请求会在返回或超时后结束")


@app.get("/progress")
def progress():
    with _guard:
        task = _progress.get(request.args.get("tid"))
        if not task or task["sid"] != request.args.get("sid"):
            return jsonify(ok=False, code="TASK_LOST", msg="任务记录已丢失或过期，请核对服务状态；不会自动重启任务"), 404
        try:
            offset = max(0, int(request.args.get("offset",0)))
        except ValueError:
            offset = 0
        return jsonify(ok=True, lines=task["lines"][offset:], offset=len(task["lines"]),
            done=task["done"], state=task["state"], result=task["result"],
            start_time=task["start_time"])


if __name__ == "__main__":
    print("打开 http://127.0.0.1:5000；同局域网可使用本机 IP。")
    app.run(host="0.0.0.0", port=5000, debug=False)
