"""Project grants and content-free model invocation audit."""

import json
import time


def initialize_model_access(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS model_profile_access (
        profile_id VARCHAR(64) PRIMARY KEY, restricted INTEGER NOT NULL,
        project_ids_json LONGTEXT NOT NULL, updated_at DOUBLE NOT NULL)""")


def get_model_access(store, profile_id):
    with store._connection() as connection:
        row = connection.execute('SELECT * FROM model_profile_access WHERE profile_id=?', (profile_id,)).fetchone()
    return {'restricted': bool(row['restricted']) if row else False, 'project_ids': json.loads(row['project_ids_json']) if row else []}


def set_model_access(store, profile_id, restricted, project_ids):
    if not store.get_model_profile(profile_id):
        raise KeyError(profile_id)
    ids = list(dict.fromkeys(project_ids))
    if len(ids) > 200 or any(not store.get_project(identifier) for identifier in ids):
        raise ValueError('授权项目不存在或超过 200 项')
    with store._connection() as connection:
        store.lock_interface_dispatch(connection)
        connection.execute('DELETE FROM model_profile_access WHERE profile_id=?', (profile_id,))
        connection.execute('INSERT INTO model_profile_access(profile_id,restricted,project_ids_json,updated_at) VALUES(?,?,?,?)', (profile_id, int(restricted), json.dumps(ids), time.time()))
    return {'restricted': bool(restricted), 'project_ids': ids}


class ProjectModelStore:
    def __init__(self, store, project_id, actor_id=''):
        self.store, self.project_id, self.actor_id = store, project_id, actor_id

    def __getattr__(self, name):
        return getattr(self.store, name)

    def permitted(self, profile_id):
        access = get_model_access(self.store, profile_id)
        return not access['restricted'] or self.project_id in access['project_ids']

    def get_model_profile(self, profile_id):
        return self.store.get_model_profile(profile_id) if self.permitted(profile_id) else None

    def list_model_profiles(self):
        return [profile for profile in self.store.list_model_profiles() if self.permitted(profile['id'])]

    def active_model_profile(self):
        profile = self.store.active_model_profile()
        return profile if profile and self.permitted(profile['id']) else None

    def call_model(self, profile, prompt, **kwargs):
        from auto_test.platform.models import call_model
        if not self.permitted(profile['id']):
            raise PermissionError('当前项目未获模型使用授权')
        started, outcome = time.monotonic(), 'failed'
        try:
            response = call_model(profile, prompt, **kwargs)
            outcome = 'success'
            return response
        finally:
            self.store.add_audit_event(actor_user_id=self.actor_id or None, project_id=self.project_id or None,
                action='model.invoke', target_type='model_profile', target_id=profile['id'], outcome=outcome,
                detail={'elapsed_ms': round((time.monotonic() - started) * 1000, 1)})
