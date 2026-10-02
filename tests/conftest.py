import os

import pytest


@pytest.fixture
def write_props():
  def _write(base_dir, ext, content, env='dev'):
    path = os.path.join(str(base_dir), 'properties.{}.{}'.format(env, ext))
    with open(path, 'w') as f:
      f.write(content)
    return path
  return _write
