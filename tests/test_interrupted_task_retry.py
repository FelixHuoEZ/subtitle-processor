from pathlib import Path
from types import SimpleNamespace

from flask import Flask

from app.routes import process_routes, view_routes


class _FakeFileService:
    def __init__(self, tasks):
        self.tasks = tasks
        self.claims = {}

    def get_file_info(self, task_id):
        return self.tasks.get(task_id)

    def add_file_info(self, task_id, task_info):
        self.tasks[task_id] = dict(task_info)

    def update_file_info(self, task_id, updates):
        self.tasks[task_id].update(updates)

    def delete_file_info(self, task_id):
        self.tasks.pop(task_id, None)

    def claim_task_operation(self, task_id, operation, ttl_seconds=7200):
        key = (task_id, operation)
        if key in self.claims:
            return None
        self.claims[key] = 'claim-token'
        return 'claim-token'

    def release_task_operation(self, task_id, operation, owner_token):
        key = (task_id, operation)
        if self.claims.get(key) != owner_token:
            return False
        del self.claims[key]
        return True


class _FakeThread:
    instances = []

    def __init__(self, target, args, daemon, name):
        self.target = target
        self.args = args
        self.daemon = daemon
        self.name = name
        self.started = False
        self.instances.append(self)

    def start(self):
        self.started = True


def _build_client(monkeypatch, tasks):
    fake_file_service = _FakeFileService(tasks)
    monkeypatch.setattr(process_routes, 'file_service', fake_file_service)
    monkeypatch.setattr(view_routes, 'file_service', fake_file_service)
    monkeypatch.setattr(
        process_routes,
        'threading',
        SimpleNamespace(Thread=_FakeThread),
    )
    monkeypatch.setattr(process_routes.uuid, 'uuid4', lambda: 'retry-task')
    _FakeThread.instances = []

    app = Flask(
        __name__,
        template_folder=str(Path(__file__).resolve().parents[1] / 'app' / 'templates'),
    )
    app.secret_key = 'test'
    app.register_blueprint(view_routes.view_bp)
    app.register_blueprint(process_routes.process_bp)
    return app.test_client(), fake_file_service


def test_retry_interrupted_task_creates_fresh_linked_task(monkeypatch):
    tasks = {
        'old-task': {
            'id': 'old-task',
            'url': 'https://www.youtube.com/watch?v=video1234567',
            'platform': 'youtube',
            'status': 'interrupted',
            'tags': ['learning'],
            'auto_transcribe': True,
            'extract_audio': True,
            'filename': 'Original title',
            'request_source': 'telegram',
            'audio_file': '/tmp/stale-audio.mp3',
            'error': '服务重启，后台任务已中断，请重新发起。',
        }
    }
    client, fake_file_service = _build_client(monkeypatch, tasks)

    response = client.post(
        '/process/status/old-task/retry',
        headers={'Accept': 'application/json'},
    )

    assert response.status_code == 202
    payload = response.get_json()
    assert payload['process_id'] == 'retry-task'
    assert payload['view_url'] == '/view/retry-task'
    assert payload['reused'] is False

    new_task = fake_file_service.tasks['retry-task']
    assert new_task['retry_of'] == 'old-task'
    assert new_task['retry_root_id'] == 'old-task'
    assert new_task['retry_attempt'] == 1
    assert new_task['status'] == 'pending'
    assert new_task['tags'] == ['learning']
    assert new_task['auto_transcribe'] is True
    assert new_task['request_source'] == 'web_retry'
    assert new_task['original_request_source'] == 'telegram'
    assert 'audio_file' not in new_task
    assert 'error' not in new_task
    assert fake_file_service.tasks['old-task']['retry_task_id'] == 'retry-task'
    assert fake_file_service.claims == {}

    assert len(_FakeThread.instances) == 1
    thread = _FakeThread.instances[0]
    assert thread.started is True
    assert thread.args[1]['id'] == 'retry-task'


def test_retry_interrupted_task_reuses_existing_successor(monkeypatch):
    tasks = {
        'old-task': {
            'id': 'old-task',
            'url': 'https://www.youtube.com/watch?v=video1234567',
            'platform': 'youtube',
            'status': 'interrupted',
            'retry_task_id': 'existing-task',
        },
        'existing-task': {
            'id': 'existing-task',
            'status': 'processing',
        },
    }
    client, fake_file_service = _build_client(monkeypatch, tasks)

    response = client.post(
        '/process/status/old-task/retry',
        headers={'Accept': 'application/json'},
    )

    assert response.status_code == 202
    assert response.get_json()['process_id'] == 'existing-task'
    assert response.get_json()['reused'] is True
    assert set(fake_file_service.tasks) == {'old-task', 'existing-task'}
    assert _FakeThread.instances == []


def test_retry_rejects_non_interrupted_task(monkeypatch):
    tasks = {
        'active-task': {
            'id': 'active-task',
            'url': 'https://www.youtube.com/watch?v=video1234567',
            'platform': 'youtube',
            'status': 'processing',
        }
    }
    client, fake_file_service = _build_client(monkeypatch, tasks)

    response = client.post(
        '/process/status/active-task/retry',
        headers={'Accept': 'application/json'},
    )

    assert response.status_code == 409
    assert response.get_json()['status'] == 'invalid_task_status'
    assert set(fake_file_service.tasks) == {'active-task'}
    assert _FakeThread.instances == []


def _interrupted_task(stage_code, **overrides):
    task = {
        'id': 'old-task',
        'url': 'https://www.youtube.com/watch?v=video1234567',
        'platform': 'youtube',
        'status': 'interrupted',
        'request_source': 'chrome_extension',
        'progress_runs': [
            {
                'status': 'interrupted',
                'stages': [
                    {
                        'code': stage_code,
                        'status': 'interrupted',
                    }
                ],
            }
        ],
    }
    task.update(overrides)
    return task


def _call_auto_retry(app, task_id='old-task'):
    with app.test_request_context(headers={'Accept': 'application/json'}):
        return process_routes.retry_interrupted_task(
            task_id,
            request_source='auto_restart_retry',
            enforce_auto_safety=True,
        )


def test_auto_retry_replays_safe_stage_as_linked_task(monkeypatch):
    client, fake_file_service = _build_client(
        monkeypatch,
        {'old-task': _interrupted_task('transcribe_audio')},
    )

    response, status_code = _call_auto_retry(client.application)

    assert status_code == 202
    assert response.get_json()['process_id'] == 'retry-task'
    new_task = fake_file_service.tasks['retry-task']
    assert new_task['request_source'] == 'auto_restart_retry'
    assert new_task['original_request_source'] == 'chrome_extension'
    assert new_task['retry_of'] == 'old-task'
    assert fake_file_service.tasks['old-task']['auto_retry_status'] == 'scheduled'
    assert fake_file_service.tasks['old-task']['auto_retry_reason'] == 'transcribe_audio'


def test_auto_retry_skips_stage_with_possible_readwise_side_effect(monkeypatch):
    client, fake_file_service = _build_client(
        monkeypatch,
        {'old-task': _interrupted_task('send_readwise')},
    )

    response, status_code = _call_auto_retry(client.application)

    assert status_code == 409
    assert response.get_json()['status'] == 'auto_retry_skipped'
    assert response.get_json()['reason'] == 'unsafe_interrupted_stage:send_readwise'
    assert set(fake_file_service.tasks) == {'old-task'}
    assert _FakeThread.instances == []


def test_auto_retry_skips_safe_stage_with_existing_readwise_article(monkeypatch):
    client, fake_file_service = _build_client(
        monkeypatch,
        {
            'old-task': _interrupted_task(
                'transcribe_audio',
                readwise_url_only_article_id='reader-article',
            )
        },
    )

    response, status_code = _call_auto_retry(client.application)

    assert status_code == 409
    assert response.get_json()['reason'] == 'readwise_side_effect_may_exist'
    assert set(fake_file_service.tasks) == {'old-task'}
    assert _FakeThread.instances == []


def test_auto_retry_stops_at_configured_attempt_limit(monkeypatch):
    monkeypatch.setenv('AUTO_RETRY_INTERRUPTED_MAX_ATTEMPTS', '3')
    client, fake_file_service = _build_client(
        monkeypatch,
        {'old-task': _interrupted_task('download_prepare', retry_attempt=3)},
    )

    response, status_code = _call_auto_retry(client.application)

    assert status_code == 409
    assert response.get_json()['reason'] == 'max_attempts_reached'
    assert fake_file_service.tasks['old-task']['auto_retry_status'] == 'skipped'
    assert _FakeThread.instances == []


def test_startup_auto_retry_scheduler_is_opt_in(monkeypatch):
    client, _ = _build_client(monkeypatch, {})
    monkeypatch.setenv('AUTO_RETRY_INTERRUPTED_TASKS', 'true')
    monkeypatch.setenv('AUTO_RETRY_INTERRUPTED_DELAY_SECONDS', '0')

    scheduled = process_routes.schedule_auto_retry_interrupted_tasks(
        client.application,
        ['old-task'],
    )

    assert scheduled is True
    assert len(_FakeThread.instances) == 1
    thread = _FakeThread.instances[0]
    assert thread.started is True
    assert thread.name == 'auto-retry-interrupted-tasks'
    assert thread.args[1] == ['old-task']


def test_startup_auto_retry_records_aggregate_outcome(monkeypatch):
    app = Flask(__name__)
    recorded = []
    app.runtime_metrics_service = SimpleNamespace(
        record_auto_restart_retry=lambda outcome, status_code: recorded.append(
            (outcome, status_code)
        )
    )
    responses = iter(
        [SimpleNamespace(status_code=202), SimpleNamespace(status_code=409)]
    )
    monkeypatch.setattr(process_routes.time, 'sleep', lambda _seconds: None)
    monkeypatch.setattr(
        process_routes,
        'retry_interrupted_task',
        lambda *_args, **_kwargs: next(responses),
    )

    process_routes._run_startup_auto_retries(
        app,
        ['scheduled-task', 'skipped-task'],
        delay_seconds=0,
    )

    assert recorded == [('scheduled', 202), ('skipped', 409)]


def test_interrupted_task_detail_shows_retry_button(monkeypatch):
    tasks = {
        'old-task': {
            'id': 'old-task',
            'url': 'https://www.youtube.com/watch?v=video1234567',
            'platform': 'youtube',
            'status': 'interrupted',
            'error': '服务重启，后台任务已中断，请重新发起。',
        }
    }
    client, _ = _build_client(monkeypatch, tasks)

    response = client.get('/view/old-task')

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'action="/process/status/old-task/retry"' in html
    assert '>重新发起</button>' in html
