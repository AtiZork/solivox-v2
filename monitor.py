from flask import Blueprint, render_template
from models import Trade

monitor_bp = Blueprint('monitor', __name__)


@monitor_bp.route('/monitor/<int:token_id>')
def monitor(token_id):
    token = Trade.query.get(token_id)
    return render_template('monitor.html', token=token)
