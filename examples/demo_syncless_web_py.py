#! /usr/local/bin/stackless2.6
#
# demo_syncless_web_py.py: running a (web.py) application under Syncless WSGI
# by pts@fazekas.hu at Tue Dec 22 12:16:22 CET 2009
#

import web

urls = (
    '/(.*)', 'hello',
)

class hello:
    def GET(self, name):
        if not name:
            name = 'world'
        web.header('Content-Type', 'text/html; charset=UTF-8')
        return 'Hello, <b>' + name + '</b>!'

app = web.application(urls, globals())

if __name__ == '__main__':
  import logging
  import sys
  from syncless import wsgi
  if len(sys.argv) > 1:
    logging.root.setLevel(logging.DEBUG)
  else:
    logging.root.setLevel(logging.INFO)
  wsgi.RunHttpServer(app)
