# coding: utf-8

import Cookie
import gevent.monkey; gevent.monkey.patch_all()
import gevent.pywsgi
import json
import pymongo

class IngroundResponse:
	def __init__(self, start_response):
		self._start_response = start_response
	
	def _response(self, status_code, data, header):
		self._start_response(status_code, [
			('Content-type', 'application/json'),
			('Access-Control-Allow-Origin', '*')
		] + header)
		return [json.dumps(data)]

	def login(self, session_id):
		return self.done('login', {}, [
			('Set-Cookie',
			'INGROUND_SESSION_ID=%s' % session_id)
		])

	def done(self, kind, param, header = []):
		param['kind'] = kind
		return self._response('200 OK', param, header)
	
	def error(self, message):
		return self.done('error', {'message': message})

	def fail(self, message):
		return self._response('400 Bad Request', {'message': message})

class Inground:
	def __init__(self, environ, start_response):
		self._environ = environ
		self._response = IngroundResponse(start_response)
		self._routine = {
			'map': self._map
		}

	def _parse(self):
		try:
			content_length = int(self._environ.get('CONTENT_LENGTH', 0))
		except ValueError:
			content_length = 0
		content = self._environ['wsgi.input'].read(content_length)
		self._content = json.loads(content)
	
	def _create_session_id(self):
		import string
		import random
		import time
		bag = string.ascii_uppercase + string.ascii_lowercase + string.digits
		return ''.join(random.sample(bag * 8, 8)) + hex(int(time.time()))[2:]

	def _login(self):
		account = self._content['account']
		if account:
			session_id = self._create_session_id()
			self._session = {
				'session_id': session_id,
				'account': account
			}
			db.session.insert(self._session)
			return self._response.login(session_id)
		else:
			return self._response.fail('empty account')

	def _map(self):
		pass

	def run(self):
		try:
			self._parse()
		except ValueError:
			return self._response.fail('invalid json content')
	
		if 'kind' not in self._content:
			return self._response.fail('no kind')

		kind = self._content['kind']

		self._session = None
		cookie = Cookie.SimpleCookie()
		cookie.load(self._environ.get('HTTP_COOKIE', ''))
		if 'INGROUND_SESSION_ID' in cookie:
			session_id = cookie['INGROUND_SESSION_ID'].value
			sessions = db.session.find({'session_id': session_id})
			if sessions.count() > 0:
				self._session = sessions[0]

		if self._session is None:
			if kind == 'login':
				return self._login()
			else:
				return self._response.fail('login required')

		if kind not in self._routine:
			return self._response.fail('no routine')
		
		return self._routine[kind]()

def application(environ, start_response):
	app = Inground(environ, start_response)
	return app.run()

db = None

if __name__ == '__main__':
	connection = pymongo.Connection()
	connection.drop_database('inground_db')
	db = connection.inground_db
	server = gevent.pywsgi.WSGIServer(('0.0.0.0', 16330), application)
	server.serve_forever()

