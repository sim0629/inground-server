# coding: utf-8

import gevent.monkey; gevent.monkey.patch_all()
import gevent.pywsgi

import json

def application(environ, start_response):
	try:
		request_body_size = int(environ.get('CONTENT_LENGTH', 0))
	except (ValueError):
		request_body_size = 0
	request_body = environ['wsgi.input'].read(request_body_size)

	try:
		request_json = json.loads(request_body)
	except (ValueError):
		return error(start_response)
	response_body = json.dumps(request_json)
	print response_body

	start_response('200 OK', [('Content-type', 'application/json')])
	return [response_body]

def error(start_response):
	start_response('400 Bad Request')
	return []

if __name__ == '__main__':
	server = gevent.pywsgi.WSGIServer(('0.0.0.0', 16330), application)
	server.serve_forever()

