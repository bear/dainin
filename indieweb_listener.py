#!/usr/bin/env python

"""
:copyright: (c) 2013-2014 by Mike Taylor
:license: MIT, see LICENSE for more details.

A simple Flask web service to handle inbound HTML
events that IndieWeb sites require.
"""

import os, sys
import json
import logging
import datetime
import urllib

import redis
import requests
import ronkyuu

from urlparse import urlparse, ParseResult
from mf2py.parser import Parser
from flask import Flask, request, redirect, render_template

from flask.ext.wtf import Form
from wtforms import TextField, HiddenField, BooleanField
from wtforms.validators import Required


class LoginForm(Form):
    domain       = TextField('domain', validators = [ DataRequired() ])
    client_id    = HiddenField('client_id')
    redirect_uri = HiddenField('redirect_uri')

class Events(object):
    def __init__(self, config):
        self.handlers = {}
        self.config   = config

        self.loadHandlers()

    def loadHandlers(self):
        if 'handler_path' in self.config:
            handlerPath = os.path.abspath(
                os.path.expanduser(self.config['handler_path']))

            for (dirpath, dirnames, filenames) in os.walk(handlerPath):
                for filename in filenames:
                    moduleName, moduleExt = os.path.splitext(os.path.basename(filename))
                    if moduleExt == '.py':
                        module = imp.load_source(moduleName, os.path.join(handlerPath, filename))
                        if hasattr(module, 'setup'):
                            self.handlers[moduleName.lower()] = module

    def handle(self, eventClass, eventName, *args):
        eventClass = eventClass.lower()
        if eventClass in self.handlers:
            module = self.handlers[eventClass]
            try:
                if hasattr(module, eventName):
                    getattr(module, eventName)(*args)
            except Exception, e:
                raise Exception('error during call %s.%s(%s)' % (eventClass, eventName, ','.join(args)))


# check for uwsgi, use PWD if present or getcwd() if not
_uwsgi = __name__.startswith('uwsgi')
if _uwsgi:
    _ourPath    = os.getenv('PWD', None)
    _configFile = '/etc/indieweb_listener.cfg'
else:
    _ourPath    = os.getcwd()
    _configFile = os.path.join(_ourPath, 'indieweb_listener.cfg')

app = Flask(__name__)
cfg = None
db  = None
app.config['SECRET_KEY'] = 'foo'


@app.route('/login', methods=['GET', 'POST'])
def handleLogin():
    app.logger.info('handleLogin [%s]' % request.method)
    form = LoginForm(client_id=cfg['client_id'], redirect_uri='%s/success' % cfg['baseurl'])

    if form.validate_on_submit():
        app.logger.info('login domain [%s]' % form.domain.data)
        authEndpoints = ronkyuu.indieauth.discoverAuthEndpoints(form.domain.data)

        if 'authorization_endpoint' in authEndpoints:
            authURL = None
            for url in authEndpoints['authorization_endpoint']:
                authURL = url
                break

            if authURL is not None:
                url = ParseResult(authURL.scheme, 
                                  authURL.netloc,
                                  authURL.path,
                                  authURL.params,
                                  urllib.urlencode({ 'me':            form.domain.data,
                                                     'redirect_uri':  form.redirect_uri.data,
                                                     'client_id':     form.client_id.data,
                                                     'scope':         'post',
                                                     'response_type': 'id'
                                                   }),
                                  authURL.fragment).geturl()

                if db is not None:
                    db.hset(form.domain.data, 'redirect_uri', form.redirect_uri.data)
                    db.hset(form.domain.data, 'client_id',    form.client_id.data)
                    db.hset(form.domain.data, 'scope',        'post')
                    db.hdel(form.domain.data, 'code')  # clear any existing auth code
                    db.expire(form.domain.data, '300') # expire in 5 minutes unless successful

                return redirect(url)
        else:
            return 'insert fancy no auth endpoint found error message here', 403

    return render_template('login.html', title = 'Authenticate', form = form)

@app.route('/success', methods=['GET',])
def handleLoginSuccess():
    app.logger.info('handleLoginSuccess [%s]' % request.method)
    me    = request.args.get('me')
    code  = request.args.get('code')
    scope = None
    if db is not None:
        data = db.hgetall(me)
        if data:
            scope = data['scope']
        db.hset(me, 'code', code)
        db.expire(me, 86400)
        db.set(code, me)
        db.expire(code, 86400)

    return 'authentication for %s with the scope %s was successful' % (me, scope), 200

@app.route('/auth', methods=['GET',])
def handleAuth():
    app.logger.info('handleAuth [%s]' % request.method)
    # r = ronkyuu.indieauth.validateAuthCode(code=request.args.get('code'), client_id=cfg['client_id'], redirect_uri='%s/success' % cfg['baseurl'])
    # if 'response' in r:
    #     app.logger.info('auth code is valid')
    #     return 'code valid', 200
    # else:
    #     return 'code invalid', 403
    result = False
    if db is not None:
        code = request.args.get('code')
        me   = db.get(code)
        if me:
            data = db.hgetall(me)
            if data and data['code'] == code:
                result = True
    if result:
        return 'valid', 200
    else:
        return 'invalid', 403

def validURL(targetURL):
    """Validate the target URL exists by making a HEAD request for it
    """
    r = requests.head(targetURL)
    return r.status_code == requests.codes.ok

noteTemplate = """<span id="%(url)s"><p class="byline h-entry" role="note"> <a href="%(url)s">%(name)s</a> <time datetime="%(date)s">%(date)s</time></p></span>
%(marker)s
"""

def extractHCard(mf2Data):
    result = { 'name': '', 
               'url':  '',
             }
    if 'items' in mf2Data:
        for item in p['items']:
            if 'type' in item and 'h-card' in item['type']:
                hcard['name'] = item['properties']['name']
                if 'url' in item['properties']:
                    hcard['url'] = item['properties']['url']
    return result

def generateSafeName(sourceURL):
    urlData = urlparse(sourceURL)
    result  = '%s_%s.mention' % (urlData.netloc, urlData.path.replace('/', '_'))
    result  = os.path.join(cfg['basepath'], result)
    return result

def processWebmention(sourceURL, targetURL):
    h = open(os.path.join(cfg['logpath'], 'mentions.log', 'w+'))
    h.write('target=%s source=%s' % (targetURL, sourceURL))
    h.close()

    r = requests.get(sourceURL, verify=False)
    if r.status_code == requests.codes.ok:
        mentionData = { 'url':       sourceURL,
                        'targetURL': targetURL,
                        'recvDate':  datetime.date.today().strftime('%d %b %Y %H:%M')
                      }
        if 'charset' in r.headers.get('content-type', ''):
            mentionData['content'] = r.text
        else:
            mentionData['content'] = r.content

        mf2Data = Parser(doc=mentionData['content']).to_dict()
        hcard   = extractHCard(mf2Data)

        mentionData['hcardName'] = hcard['name']
        mentionData['hcardURL']  = hcard['url']
        mentionData['mf2data']   = mf2Data

        targetFile = generateSafeName(sourceURL)

        open(targetFile, 'w').write(json.dumps(mentionData))

def mention(sourceURL, targetURL):
    """Process the Webmention of the targetURL from the sourceURL.

    To verify that the sourceURL has indeed referenced our targetURL
    we run findMentions() at it and scan the resulting href list.
    """
    app.logger.info('discovering Webmention endpoint for %s' % sourceURL)

    mentions = ronkyuu.findMentions(sourceURL)

    for href in mentions['refs']:
        if href != sourceURL and href == targetURL:
            app.logger.info('post at %s was referenced by %s' % (targetURL, sourceURL))

            # event.inboundWebmention(sourceURL, targetURL, mentions=mentions)
            processWebmention(sourceUrl, targetURL)

@app.route('/webmention', methods=['POST'])
def handleWebmention():
    app.logger.info('handleWebmention [%s]' % request.method)
    if request.method == 'POST':
        valid  = False
        source = None
        target = None

        if 'source' in request.form:
            source = request.form['source']
        if 'target' in request.form:
            target = request.form['target']

        valid = validURL(target)

        app.logger.info('source: %s target: %s valid? %s' % (source, target, valid))

        if valid:
            mention(source, target)

        if valid:
            return 'done'
        else:
            return 'invalid post', 404

def initLogging(logger, logpath=None, echo=False):
    logFormatter = logging.Formatter("%(asctime)s %(levelname)-9s %(message)s", "%Y-%m-%d %H:%M:%S")

    if logpath is not None:
        from logging.handlers import RotatingFileHandler

        logfilename = os.path.join(logpath, 'webmentions.log')
        logHandler  = logging.handlers.RotatingFileHandler(logfilename, maxBytes=1024 * 1024 * 100, backupCount=7)
        logHandler.setFormatter(logFormatter)
        logger.addHandler(logHandler)

    if echo:
        echoHandler = logging.StreamHandler()
        echoHandler.setFormatter(logFormatter)
        logger.addHandler(echoHandler)

    logger.setLevel(logging.INFO)
    logger.info('starting Webmention App')

def loadConfig(configFilename, host=None, port=None, basepath=None, logpath=None):
    filename = os.path.abspath(configFilename)

    if os.path.exists(filename):
        result = json.load(open(filename, 'r'))
    else:
        result = {}

    if host is not None and 'host' not in result:
        result['host'] = host
    if port is not None and 'port' not in result:
        result['port'] = port
    if basepath is not None and 'basepath' not in result:
        result['basepath'] = basepath
    if logpath is not None and 'logpath' not in result:
        result['logpath'] = logpath

    return result

def getRedis(cfgRedis):
    if 'host' not in cfgRedis:
        cfgRedis['host'] = '127.0.0.1'
    if 'port' not in cfgRedis:
        cfgRedis['port'] = 6379
    if 'db' not in cfgRedis:
        cfgRedis['db'] = 0

    return redis.StrictRedis(host=cfgRedis['host'], port=cfgRedis['port'], db=cfgRedis['db'])

# event = events.Events(config={ "handler_path": os.path.join(_ourPath, "handlers") })

if _uwsgi:
    cfg = loadConfig(_configFile, logpath=_ourPath)
    initLogging(app.logger, cfg['logpath'])
    if 'redis' in cfg:
        db = getRedis(cfg['redis'])

#
# None of the below will be run for nginx + uwsgi
#
if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--host',     default='0.0.0.0')
    parser.add_argument('--port',     default=5000, type=int)
    parser.add_argument('--logpath',  default='/srv/webmention')
    parser.add_argument('--basepath', default='/opt/bearlog/')
    parser.add_argument('--config',   default='/etc/indieweb_listener.cfg')

    args = parser.parse_args()

    cfg = loadConfig(args.config, args.host, args.port, args.basepath, args.logpath)

    if 'redis' in cfg:
        db = getRedis(cfg['redis'])

    initLogging(app.logger, cfg['logpath'], echo=True)

    app.run(host=cfg['host'], port=cfg['port'], debug=True)
