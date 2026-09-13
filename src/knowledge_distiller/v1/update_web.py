from urllib.parse import urlparse
from flask import jsonify, request, send_file
from .updates import Updates, UpdateError, bundle_info


def register_updates(app, data_root):
    info = bundle_info()
    if info.get('component_updates'):
        from .component_updates import ComponentUpdates
        updates = ComponentUpdates(data_root, info=info)
    else:
        updates = Updates(data_root, info=info)
    app.extensions['updates'] = updates

    @app.context_processor
    def update_context():
        return {'update': updates.snapshot()}

    @app.before_request
    def update_install_gate():
        if request.method == 'POST' and updates.phase == 'installing':
            return '正在安装更新，请等待应用重新打开。', 503

    def trusted_request_host():
        from .local_address import LocalAddressError, allowed_host, load
        try:
            address = app.config.get('LOCAL_ADDRESS_ACTIVE') or load(data_root)
            return allowed_host(request.host, address,
                                actual_port=app.config.get('LOCAL_ADDRESS_PORT') or int(request.environ.get('SERVER_PORT', 80)))
        except LocalAddressError:
            return False

    @app.get('/settings/updates/status')
    def update_status():
        if not trusted_request_host():
            return '', 403
        status = updates.snapshot()
        documents = app.extensions.get('document_component')
        if documents is not None:
            status['document_component'] = documents.readiness
        return jsonify(status)

    @app.get('/settings/updates/archive')
    def update_archive():
        if not trusted_request_host() or request.args.get('token')!=updates.token:
            return '',403
        try:
            return send_file(updates.manual_archive(),as_attachment=True)
        except UpdateError as error:
            return str(error),409

    @app.post('/settings/updates/<action>')
    def update_action(action):
        if (not trusted_request_host()
            or request.headers.get('X-Update-Token') != updates.token
            or (request.headers.get('Origin') and request.headers['Origin'] != 'http://' + request.host)):
            return '', 403
        try:
            if action in ('check', 'download', 'download-full'):
                updates.start(action)
            elif action == 'seen':
                updates.mark_seen((request.get_json(silent=True) or {}).get('version'))
            elif action == 'install':
                updates.request_install()
            else:
                return '', 404
            return jsonify(updates.snapshot())
        except UpdateError as error:
            return jsonify(error=str(error)), 409
