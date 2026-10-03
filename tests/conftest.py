import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

from tfwrap.main import TfWrap


@pytest.fixture
def make_wrapper():
  def _make(env='dev', region='us-east-1', target_dir='.', app_name_override='', account_id='123456789012'):
    wrapper = TfWrap(env=env, region=region, target_dir=target_dir)
    wrapper.app_name_override = app_name_override

    sts = MagicMock()
    sts.get_caller_identity.return_value = {'Account': account_id}

    ssm = MagicMock()
    ssm.get_parameter.return_value = {'Parameter': {'Value': 'existing-backend'}}

    wrapper._sts = sts
    wrapper._ssm = ssm
    return wrapper

  return _make
