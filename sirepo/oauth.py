# -*- coding: utf-8 -*-
u"""OAUTH support

:copyright: Copyright (c) 2016 RadiaSoft LLC.  All Rights Reserved.
:license: http://www.apache.org/licenses/LICENSE-2.0.html
"""
from __future__ import absolute_import, division, print_function

from flask_sqlalchemy import SQLAlchemy
from pykern import pkconfig
from pykern.pkdebug import pkdc, pkdexc, pkdlog, pkdp
from sirepo import server
from sirepo import simulation_db
import flask
import flask_oauthlib.client
import glob
import os
import os.path
import werkzeug.exceptions
import werkzeug.security

# flask session oauth_login_state values
_ANONYMOUS = 'anonymous'
_LOGGED_IN = 'logged_in'
_LOGGED_OUT = 'logged_out'

_USER_DB_FILE = 'user.db'

_db = None


def authorize(simulation_type, app, oauth_type):
    """Redirects to an OAUTH request for the specified oauth_type ('github').

    If oauth_type is 'anonymous', the current session is cleared.
    """
    oauth_next = '/{}#{}'.format(simulation_type, flask.request.args.get('next') or '')
    if oauth_type == _ANONYMOUS:
        _update_session(_ANONYMOUS)
        _clear_session_user()
        return server.javascript_redirect(oauth_next)
    state = werkzeug.security.gen_salt(64)
    flask.session['oauth_salt'] = state
    flask.session['oauth_next'] = oauth_next
    return _oauth_client(app, oauth_type).authorize(
        callback=flask.url_for('app_oauth_authorized', oauth_type=oauth_type, _external=True),
        state=state,
    )


def authorized_callback(app, oauth_type):
    """Handle a callback from a successful OAUTH request. Tracks oauth
    users in a database.
    """
    oauth = _oauth_client(app, oauth_type)
    resp = oauth.authorized_response()
    if not resp:
        pkdp('missing oauth response')
        werkzeug.exceptions.abort(403)
    state = _remove_session_key('oauth_salt')
    if state != flask.request.args.get('state'):
        pkdp('mismatch oauth state: {} != {}', state, flask.request.args.get('state'))
        werkzeug.exceptions.abort(403)
    # fields: id, login, name
    user_data = oauth.get('user', token=(resp['access_token'], '')).data
    user = User.query.filter_by(oauth_id=user_data['id']).first()
    if user:
        if server.SESSION_KEY_USER in flask.session and flask.session[server.SESSION_KEY_USER] != user.uid:
            _move_user_simulations(server.session_user(), user.uid)
        user.user_name = user_data['login']
        user.display_name = user_data['name']
        server.session_user(user.uid)
    else:
        if not server.session_user(checked=False):
            # ensures the user session (uid) is ready if new user logs in from logged-out session
            pkdp('creating new session for user: {}', user_data['id'])
            simulation_db.simulation_dir('')
        user = User(server.session_user(), user_data['login'], user_data['name'], oauth_type, user_data['id'])
    _db.session.add(user)
    _update_session(_LOGGED_IN, user.user_name)
    return server.javascript_redirect(_remove_session_key('oauth_next'))


def logout(simulation_type):
    """Sets the login_state to logged_out and clears the user session.
    """
    _update_session(_LOGGED_OUT)
    _clear_session_user()
    return flask.redirect('/{}'.format(simulation_type))


def set_default_state(logged_out_as_anonymous=False):
    if 'oauth_login_state' not in flask.session:
        _update_session(_ANONYMOUS)
    elif logged_out_as_anonymous and flask.session['oauth_login_state'] == _LOGGED_OUT:
        _update_session(_ANONYMOUS)
    return {
        'login_state': flask.session['oauth_login_state'],
        'user_name': flask.session['oauth_user_name'],
    }


def _clear_session_user():
    if server.SESSION_KEY_USER in flask.session:
        del flask.session[server.SESSION_KEY_USER]


def _move_user_simulations(from_uid, to_uid):
    # move all non-example simulations for the current session into the target user's dir
    for path in glob.glob(
        str(simulation_db.user_dir(from_uid).join('*', '*', simulation_db.SIMULATION_DATA_FILE)),
    ):
        data = simulation_db.read_json(path)
        sim = data['models']['simulation']
        if 'isExample' in sim and sim['isExample']:
            continue
        dir_path = os.path.dirname(path)
        new_dir_path = dir_path.replace(from_uid, to_uid)
        pkdp('{} -> {}', dir_path, new_dir_path)
        os.rename(dir_path, new_dir_path)


def _db_filename(app):
    return str(app.sirepo_db_dir.join(_USER_DB_FILE))


def _init(app):
    global _db
    app.config.update(
        SQLALCHEMY_DATABASE_URI='sqlite:///{}'.format(_db_filename(app)),
        SQLALCHEMY_COMMIT_ON_TEARDOWN=True,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    _db = SQLAlchemy(app, session_options=dict(autoflush=True))
    global cfg
    cfg = pkconfig.init(
        github_key=(None, str, 'GitHub application key'),
        github_secret=(None, str, 'GitHub application secret'),
    )
    if not cfg.github_key or not cfg.github_secret:
        raise RuntimeError('Missing GitHub oauth config')


def _init_tables(app):
    if not os.path.exists(_db_filename(app)):
        pkdp('creating user oauth database')
        _db.create_all()


def _oauth_client(app, oauth_type):
    if oauth_type == 'github':
        return flask_oauthlib.client.OAuth(app).remote_app(
            'github',
            consumer_key=cfg.github_key,
            consumer_secret=cfg.github_secret,
            base_url='https://api.github.com/',
            request_token_url=None,
            access_token_method='POST',
            access_token_url='https://github.com/login/oauth/access_token',
            authorize_url='https://github.com/login/oauth/authorize',
        )
    raise RuntimeError('Unknown oauth_type: {}'.format(oauth_type))


def _remove_session_key(name):
    value = flask.session[name]
    del flask.session[name]
    return value


def _update_session(login_state, user_name=''):
    flask.session['oauth_login_state'] = login_state
    flask.session['oauth_user_name'] = user_name


_init(server.app)


class User(_db.Model):
    __tablename__ = 'user_t'
    uid = _db.Column(_db.String(8), primary_key=True)
    user_name = _db.Column(_db.String(100), nullable=False)
    display_name = _db.Column(_db.String(100))
    oauth_type = _db.Column(
        _db.Enum('github', 'test', name='oauth_type'),
        nullable=False
    )
    oauth_id = _db.Column(_db.String(100), nullable=False, unique=True)

    def __init__(self, uid, user_name, display_name, oauth_type, oauth_id):
        self.uid = uid
        self.user_name = user_name
        self.display_name = display_name
        self.oauth_type = oauth_type
        self.oauth_id = oauth_id


_init_tables(server.app)