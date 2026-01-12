# app.py
"""
Vercel WSGI entrypoint.

Vercel looks for a variable named `app`
that is a valid WSGI application.
"""

from api.webhook import flask_app as app
