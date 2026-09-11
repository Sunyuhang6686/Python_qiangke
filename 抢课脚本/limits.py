"""每门及整轮提交额度；读取列表不计入选课提交次数。"""
import threading
import config


def setting(name, default, ceiling):
    try:
        return max(1, min(int(getattr(config, name, default)), ceiling))
    except (TypeError, ValueError):
        return default


def per_course(auto=False):
    return setting('AUTO_COURSE_ATTEMPTS' if auto else 'MANUAL_COURSE_ATTEMPTS',
                   5 if auto else 10, 30)


class SubmissionBudget:
    def __init__(self):
        self.limit = setting('ROUND_SUBMIT_LIMIT', 30, 100)
        self.used = 0
        self.lock = threading.Lock()

    def consume(self):
        with self.lock:
            if self.used >= self.limit:
                return False
            self.used += 1
            return True
