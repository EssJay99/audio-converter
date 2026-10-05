import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

# Flask-SQLAlchemy pins the engine URI at init_app time, so the test DB must
# be chosen via env var BEFORE the app package is imported.
_test_db_dir = tempfile.mkdtemp(prefix='audio-converter-tests-')
os.environ['AUDIO_CONVERTER_DB_PATH'] = os.path.join(_test_db_dir, 'test.db')
os.environ['AUDIO_CONVERTER_SECRET_KEY'] = 'test-secret-key'

from app import app, db  # noqa: E402
from app.models import ConversionHistory  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    """Create the schema once; wipe tables between tests."""
    with app.app_context():
        db.drop_all()
        db.create_all()
        # The in-memory work queue is global: drain it so one test's
        # un-consumed job ids can't leak into another test's assertions.
        # (Worker threads are mocked out in most tests, so nothing drains
        # it otherwise.)
        import app.routes.convert as convert_module
        try:
            while True:
                convert_module._conversion_queue.get_nowait()
        except Exception:
            pass
        try:
            while True:
                convert_module._priority_queue.get_nowait()
        except Exception:
            pass
        try:
            convert_module._paused_jobs.clear()
        except Exception:
            pass
        yield
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(fresh_db):
    app.config['TESTING'] = True
    app.config['WTF_CSRF_ENABLED'] = False
    # Tests use placeholder paths like '/tmp/x' that don't exist; let the
    # settings route accept them so the round-trip tests stay focused on
    # the fields they care about. Path-traversal defence is covered by
    # the dedicated _safe_output_path unit tests.
    import app.routes.convert as cm
    orig = cm._safe_output_path
    cm._safe_output_path = lambda raw: raw
    try:
        yield app.test_client()
    finally:
        cm._safe_output_path = orig


@pytest.fixture()
def completed_conversion(client):
    """A completed conversion row pointing at a real scratch file."""
    scratch = tempfile.NamedTemporaryFile(delete=False, suffix='.flac')
    scratch.write(b'fLaC testdata')
    scratch.close()

    with app.app_context():
        job = ConversionHistory(
            url='https://www.youtube.com/watch?v=test',
            format='FLAC',
            output_path=scratch.name,
            status='completed',
            progress=100,
        )
        db.session.add(job)
        db.session.commit()
        job_id = job.id

    yield job_id, scratch.name
    if os.path.exists(scratch.name):
        os.unlink(scratch.name)