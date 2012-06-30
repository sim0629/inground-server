# coding: utf-8

import Cookie
import gevent.coros
import gevent.monkey; gevent.monkey.patch_all()
import gevent.pywsgi
import json
import math
import os.path
import pymongo
import Queue

class Response:
	def __init__(self, start_response):
		self._start_response = start_response
	
	def _response(self, status_code, data, header):
		self._start_response(status_code, [
			('Content-type', 'application/json'),
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
		return self._response('400 Bad Request', {'message': message}, [])
	
	def test(self):
		self._start_response('200 OK', [
			('Content-type', 'text/html; charset=utf-8')
		])
		return [open(os.path.join(os.path.dirname(os.path.realpath(__file__)), 'test.html'), 'r').read()]

class Inground:
	def __init__(self, environ, start_response):
		self._environ = environ
		self._response = Response(start_response)
		self._routine = {
			'map': self._map,
			'start': self._start,
			'grab': self._grab,
			'throw': self._throw,
			'poll': self._poll
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
		if not account:
			return self._response.fail('empty account')
		if account == 'inground':
			return self._response.fail('invalid account')
		if inground_db.session.find({'account': account}).count() > 0:
			return self._response.fail('exist account')
		session_id = self._create_session_id()
		self._session = {
			'session_id': session_id,
			'account': account
		}
		inground_db.session.insert(self._session)
		return self._response.login(session_id)

	def _enqueue_ground(self, area_index):
		sessions = inground_db.session.find()
		for session in sessions:
			account = session['account']
			inground_db.poll.insert({
				'account': account,
				'kind': 'ground',
				'data': {
					'account': self._session['account'],
					'ground': area_index
				}
			})

	def _enqueue_finish(self, area_index):
		sessions = inground_db.session.find()
		for session in sessions:
			account = session['account']
			inground_db.poll.insert({
				'account': account,
				'kind': 'finish',
				'data': {
					'result': inground_map.get_result()
				}
			})

	def _map(self):
		return self._response.done('map', {'map': inground_map.info()})

	def _start(self):
		if 'location' not in self._content:
			return self._response.fail('no location')
		initial_area_index = inground_map.start(self._session['account'], self._content['location'])
		if not initial_area_index:
			return self._response.done('start', {'success': False})

		self._enqueue_ground(initial_area_index)

		return self._response.done('start', {'success': True})

	def _grab(self):
		if 'location' not in self._content:
			return self._response.fail('no location')
		location = self._content['location']
		account = self._session['account']
		stones = inground_db.stone.find({'account': account})
		if stones.count() == 0: # 첫번째라서 자기땅이면 됨
			if inground_map.is_mine(account, location):
				inground_db.stone.insert({
					'account': account,
					'location': location,
					'onground': False
				})
				return self._response.done('grab', {'success': True})
			else:
				return self._response.done('grab', {'success': False})
		else: # 직전 위치에서 됨
			stone = stones[stones.count() - 1]
			if not stone['onground']:
				return self._response.fail('throw first')
			success = inground_map.is_same(
						stone['location'],
						location)
			if success:
				inground_db.stone.update(stone, {'$set': {'onground': False}})
			return self._response.done('grab', {
				'success': success,
				'location': location
			})

	def _throw(self):
		if 'velocity' not in self._content:
			return self._response.fail('no velocity')
		velocity = self._content['velocity']
		account = self._session['account']
		stones = inground_db.stone.find({'account': account})
		stone = stones[stones.count() - 1]
		if stones.count() == 0 or stone['onground']:
			return self._response.fail('grab first')
		(success, location) = inground_map.try_throw(
			stone['location'],
			velocity)
		if success:
			if stones.count() == 3:
				inground_semaphore.acquire()
				changed_area_index = inground_map.invade(account, [
					s['location'] for s in list(stones)
				] + [location])
				if changed_area_index:
					self._enqueue_ground(changed_area_index)
					inground_db.stone.remove({'account': account})
					if inground_map.is_finished():
						self._enqueue_finish()
				else:
					success = False
				inground_semaphore.release()
			else:
				inground_db.stone.insert({
					'account': account,
					'location': location,
					'onground': True
				})
		return self._response.done('throw', {
			'success': success,
			'location': location
		})

	def _poll(self):
		for trial in xrange(30):
			poll = inground_db.poll.find_one({'account': self._session['account']})
			if poll is None:
				gevent.sleep(1)
			else:
				inground_db.poll.remove(poll)
				return self._response.done(poll['kind'], poll['data'])
		return self._response.done('poll', {})

	def run(self):
		path = self._environ.get('PATH_INFO', '')
		if path == '/test.html':
			return self._response.test()

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
			sessions = inground_db.session.find({'session_id': session_id})
			if sessions.count() > 0:
				self._session = sessions[0]

		if self._session is None:
			if kind == 'login':
				return self._login()
			else:
				return self._response.fail('login required')
		elif kind == 'login':
			return self._response.fail('already logged in')

		if kind not in self._routine:
			return self._response.fail('no routine')
		
		return self._routine[kind]()

class CoordHelper:
	def __init__(self, bound):
		self._precision = 0.00003
		
		bound_lat = [v[0] for v in bound]
		bound_lng = [v[1] for v in bound]

		center_lat = reduce(lambda x, y : x * y, bound_lat) ** (1.0 / len(bound_lat)) # geomean
		self._lng_factor = math.cos(center_lat / 180 * math.pi) # 위도에 따른 경도 보정치
		
		[self._lat_min_v, self._lng_min_v] = self._real2virtual([min(bound_lat), min(bound_lng)])

	def _real2virtual(self, real):
		return [
			int((real[0] + self._precision / 2) / self._precision),
			int((real[1] * self._lng_factor + self._precision / 2) / self._precision)
		]
	
	def real2virtual(self, real):
		v = self._real2virtual(real)
		return [
			v[0] - self._lat_min_v,
			v[1] - self._lng_min_v
		]
	
	def virtual2real(self, virtual):
		return [
			(virtual[0] + self._lat_min_v) * self._precision,
			(virtual[1] + self._lng_min_v) * self._precision / self._lng_factor
		]

class Map:
	def __init__(self, bound):
		if len(bound) < 3:
			raise ValueError('invalid bound')

		# Constants
		self.NONE = 0
		self.MINE = 10 # 제일 커야 함
		self.PATH = 2
		self.FLAG = 3

		self._coord_helper = CoordHelper(bound)
		
		bound_v = [self._coord_helper.real2virtual(x) for x in bound]
		self._x = max([v[0] for v in bound_v]) + 1
		self._y = max([v[1] for v in bound_v]) + 1
		
		self._map = [[{'index':-1, 'account':''} for y in xrange(self._y)] for x in xrange(self._x)]

		self._nofcells = {}
		self._set('inground', bound_v[0])
		self._info = self._invade('inground', bound_v + [bound_v[0]])
		self._totalcells = len(self._info)
		i = 0
		for v in self._info:
			self._map[v[0]][v[1]]['index'] = i
			i = i + 1

	def _get(self, v):
		return self._map[v[0]][v[1]]['account']

	def _set(self, who, v):
		if who not in self._nofcells:
			self._nofcells[who] = 0
		cell = self._map[v[0]][v[1]]
		if cell['account']:
			prev_who = cell['account']
			self._nofcells[prev_who] = self._nofcells[prev_who] - 1
		cell['account'] = who
		self._nofcells[who] = self._nofcells[who] + 1

	def _path_one(self, temp_map, from_v, to_v, by_x = True): # Bresenham
		if by_x:
			X = 0
			Y = 1
		else:
			X = 1
			Y = 0

		dx = to_v[X] - from_v[X]
		dy = to_v[Y] - from_v[Y]

		if abs(dy) > abs(dx):
			return self._path_one(temp_map, from_v, to_v, False)

		if dx < 0:
			return self._path_one(temp_map, to_v, from_v, by_x)

		step = 1
		if dy < 0:
			step = -1
			dy = -dy

		changed = []

		p = 2 * dy - dx
		x = from_v[X] + 1
		y = from_v[Y]
		while x <= to_v[X]:
			if p < 0:
				p = p + 2 * dy
			else:
				p = p + 2 * dy - 2 * dx
				y = y + step
			if by_x:
				if temp_map[x][y] == self.NONE:
					temp_map[x][y] = self.PATH
					changed.append([x, y])
			else:
				if temp_map[y][x] == self.NONE:
					temp_map[y][x] = self.PATH
					changed.append([y, x])
			x = x + 1

		return changed

	def _path(self, temp_map, path):
		changed = []
		for i in xrange(1, len(path)):
			changed = changed + self._path_one(temp_map, path[i - 1], path[i])
		return changed

	def info(self):
		return [self._coord_helper.virtual2real(v) for v in self._info]

	def is_mine(self, who, v):
		return self._is_mine(who, self._coord_helper.real2virtual(v))
	def _is_mine(self, who, v):
		if v[0] < 0 or v[0] >= self._x or\
			v[1] < 0 or v[1] >= self._y:
			return False
		return self._get(v) == who
	
	def is_same(self, v, w):
		return self._coord_helper.real2virtual(v) ==\
				self._coord_helper.real2virtual(w)

	def is_finished(self):
		return float(self._nofcells['inground']) / self._totalcells < 0.4
	
	def get_result(self):
		result = []
		for key in self._nofcells.keys():
			if key == 'inground':
				continue
			result.append({'account': key, 'nofcells': self._nofcells[key]})
		return result

	def start(self, who, v):
		return self._start(who, self._coord_helper.real2virtual(v))
	def _start(self, who, v):
		x = v[0]
		y = v[1]
		if x < 1 or x >= self._x - 1 or\
			y < 1 or y >= self._y - 1:
			return []
		initial_area_v = []
		initial_area_index = []
		for i in xrange(-1, 2):
			for j in xrange(-1, 2):
				m = self._map[x + i][y + j]
				if 'account' not in m or\
					m['account'] != 'inground':
					return []
				initial_area_v.append([x + i, y + j])
				initial_area_index.append(m['index'])
		for v in initial_area_v:
			self._set(who, v)
		return initial_area_index

	def try_throw(self, v, d): # v에서 d로 던짐
		return self._try_throw(self._coord_helper.real2virtual(v), d)
	def _try_throw(self, v, d):
		w = [int(v[0] + d[0]), int(v[1] + d[1])] # TODO: 임시 공식임
		return (w[0] >= 0 and w[0] < self._x and\
				w[1] >= 0 and w[1] < self._y and\
				self._map[w[0]][w[1]]['index'] >= 0,
				self._coord_helper.virtual2real(w))

	def invade(self, who, path):
		valid_changed_area_index = []
		for w in self._invade(who,
			[self._coord_helper.real2virtual(v) for v in path]):
			i = self._map[w[0]][w[1]]['index']
			if i >= 0:
				valid_changed_area_index.append(i)
		return valid_changed_area_index
	def _invade(self, who, path):
		if len(path) < 1:
			raise ValueError('invalid path')

		if self._get(path[0]) != who or\
			self._get(path[-1]) != who:
			return []

		temp_map = [[self.NONE for v in l] for l in self._map]
	
		mine_plus = 0
		q = Queue.Queue()
		for x in xrange(self._x):
			for y in xrange(self._y):
				if self._get([x, y]) == who and temp_map[x][y] == self.NONE:
					q.put([x, y])
					while not q.empty():
						v = q.get()
						if v[0] < 0 or v[0] >= self._x or\
							v[1] < 0 or v[1] >= self._y or\
							self._get([v[0], v[1]]) != who or\
							temp_map[v[0]][v[1]] != self.NONE:
							continue
						temp_map[v[0]][v[1]] = self.MINE + mine_plus
						q.put([v[0] - 1, v[1]])
						q.put([v[0] + 1, v[1]])
						q.put([v[0], v[1] + 1])
						q.put([v[0], v[1] - 1])
					mine_plus = mine_plus + 1

		if temp_map[path[0][0]][path[0][1]] !=\
			temp_map[path[-1][0]][path[-1][1]]:
			return []

		changed_path = self._path(temp_map, path)
		
		changed_area = []
		q = Queue.Queue()
		for x in xrange(self._x):
			for y in xrange(self._y):
				if temp_map[x][y] == self.NONE:
					is_outside = False
					q.put([x, y])
					temp_map[x][y] = self.FLAG
					flagged = [[x, y]]
					while not q.empty():
						v = q.get()
						if v[0] > 0:
							nx = v[0] - 1
							ny = v[1]
							if temp_map[nx][ny] == self.NONE:
								q.put([nx, ny])
								temp_map[nx][ny] = self.FLAG
								flagged.append([nx, ny])
						else:
							is_outside = True
						if v[0] < self._x - 1:
							nx = v[0] + 1
							ny = v[1]
							if temp_map[nx][ny] == self.NONE:
								q.put([nx, ny])
								temp_map[nx][ny] = self.FLAG
								flagged.append([nx, ny])
						else:
							is_outside = True
						if v[1] > 0:
							nx = v[0]
							ny = v[1] - 1
							if temp_map[nx][ny] == self.NONE:
								q.put([nx, ny])
								temp_map[nx][ny] = self.FLAG
								flagged.append([nx, ny])
						else:
							is_outside = True
						if v[1] < self._y - 1:
							nx = v[0]
							ny = v[1] + 1
							if temp_map[nx][ny] == self.NONE:
								q.put([nx, ny])
								temp_map[nx][ny] = self.FLAG
								flagged.append([nx, ny])
						else:
							is_outside = True
					if not is_outside:
						changed_area = changed_area + flagged

		changed_area = changed_path + changed_area

		for v in changed_area:
			self._set(who, v)

		return changed_area

def application(environ, start_response):
	app = Inground(environ, start_response)
	return app.run()

inground_db = None
'''
inground_map = Map([
	[37.45800, 126.95510],
	[37.45820, 126.95500],
	[37.45840, 126.95530],
	[37.45820, 126.95540]
]) # 테스트
'''
inground_map = Map([
	[37.45827401699613, 126.95541143417358],
	[37.45875306926878, 126.95516735315323],
	[37.460264725208205, 126.9568544626236],
	[37.45958980654367, 126.95714950561523]
]) # 버들골

inground_semaphore = gevent.coros.BoundedSemaphore()

if __name__ == '__main__':
	connection = pymongo.Connection()
	connection.drop_database('inground_db')
	inground_db = connection.inground_db

	server = gevent.pywsgi.WSGIServer(('0.0.0.0', 16330), application)
	server.serve_forever()

