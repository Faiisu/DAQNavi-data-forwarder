"""Web control panel and real-time SocketIO dashboard for DAQ Navi."""

from .app import app, socketio, init_application

__all__ = ["app", "socketio", "init_application"]
