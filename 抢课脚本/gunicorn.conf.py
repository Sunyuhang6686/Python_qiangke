# Linux 替代启动方式：gunicorn -c gunicorn.conf.py web_server:app
# Session 和任务在进程内，不能配置多个 worker/副本。
bind = '0.0.0.0:5000'
workers = 1
worker_class = 'gthread'
threads = 16
timeout = 180
max_requests = 0
reload = False
pidfile = '/opt/qk/qk.pid'
accesslog = '-'
errorlog = '-'
