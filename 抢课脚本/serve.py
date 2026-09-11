"""单进程、多线程生产启动器：python serve.py。"""
import os
from waitress import serve
from web_server import app

if __name__ == '__main__':
    print('选课助手采用单进程多线程运行；请勿启动多个副本或自动回收进程。')
    serve(app, host=os.environ.get('HOST', '0.0.0.0'),
          port=int(os.environ.get('PORT', '5000')), threads=16)
