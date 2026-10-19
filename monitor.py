from flask import Blueprint, abort

monitor_bp = Blueprint('monitor', __name__)


@monitor_bp.route('/monitor/<int:token_id>')
def monitor(token_id):
    """Retired: loaded an arbitrary Trade by sequential, guessable ID with
    no auth and no ownership check (same class of gap as /trade_history/<id>
    before it was fixed), rendering it into monitor.html. No template or
    script anywhere links to this route (monitor.html's own CSS/JS assets
    don't exist either), so it's dead from the UI's side - closing it here
    removes the one place it was still live.
    """
    abort(404)
