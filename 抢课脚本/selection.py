"""列出实际批次/栏目，让用户选择固定目标。"""
import re
from urllib.parse import urljoin
import config
from qk_login import _extract_urls
from workflow import check_response, retry_read, Cancelled


def discover_entries(session, emit=print, cancel=None):
    def read():
        base = config.BASE_URL
        center = check_response(session.get(base + '/jsxsd/xsxk/xklc_list', timeout=10))
        choices = []
        seen = set()
        for url, label in _extract_urls(center, 'xsxk_index'):
            if cancel and cancel.is_set():
                raise Cancelled()
            match = re.search(r'jx0502zbid=([0-9a-f]+)', url, re.I)
            if not match or match[1] in seen:
                continue
            seen.add(match[1])
            index = base + '/jsxsd/xsxk/xsxk_index?jx0502zbid=' + match[1]
            page = check_response(session.get(index, timeout=10))
            for entry, name in _extract_urls(page, 'comeIn'):
                if not entry.startswith('/jsxsd/xsxkkc/'):
                    continue
                choices.append({'batch_id':match[1], 'entry_url':urljoin(base, entry),
                                'name':name or label or entry.rsplit('/', 1)[-1]})
        return choices
    return retry_read(read, emit, cancel, seconds=30)
