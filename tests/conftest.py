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
        yield
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(fresh_db):
    app.config['TESTING'] = True
    app.config['WTF_CSRF_ENABLED'] = False
    yield app.test_client()


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