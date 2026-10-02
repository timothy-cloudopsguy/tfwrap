import logging
import os
import sys

import pytest

from tfwrap.properties import (
  PROPERTIES_EXTENSIONS,
  PropertiesError,
  candidate_paths,
  find_properties_file,
  load_properties,
)


def test_candidate_paths_joined_under_base_dir_in_order(tmp_path):
  base = str(tmp_path)
  assert candidate_paths('dev', base) == [
    os.path.join(base, 'properties.dev.json'),
    os.path.join(base, 'properties.dev.yaml'),
    os.path.join(base, 'properties.dev.yml'),
  ]
  assert PROPERTIES_EXTENSIONS == ('json', 'yaml', 'yml')


@pytest.mark.parametrize('ext', ['json', 'yaml', 'yml'])
def test_find_single_file(tmp_path, write_props, caplog, ext):
  path = write_props(tmp_path, ext, '{}')
  with caplog.at_level(logging.WARNING):
    assert find_properties_file('dev', str(tmp_path)) == path
  assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_find_none(tmp_path):
  assert find_properties_file('dev', str(tmp_path)) is None


def test_find_ignores_other_env(tmp_path, write_props):
  write_props(tmp_path, 'json', '{}', env='prod')
  assert find_properties_file('dev', str(tmp_path)) is None


@pytest.mark.parametrize('exts,expected', [
  (['json', 'yaml'], 'json'),
  (['yaml', 'yml'], 'yaml'),
  (['json', 'yaml', 'yml'], 'json'),
])
def test_find_precedence_warns_on_multiple(tmp_path, write_props, caplog, exts, expected):
  paths = {ext: write_props(tmp_path, ext, '{}') for ext in exts}
  with caplog.at_level(logging.WARNING):
    assert find_properties_file('dev', str(tmp_path)) == paths[expected]
  warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
  assert len(warnings) == 1
  for path in paths.values():
    assert path in warnings[0]
  assert 'Using {}'.format(paths[expected]) in warnings[0]


def test_load_valid_json(tmp_path, write_props):
  path = write_props(tmp_path, 'json', '{"app_name": "foo", "route53": {"ttl": 60}}')
  assert load_properties(path) == {'app_name': 'foo', 'route53': {'ttl': 60}}


@pytest.mark.parametrize('ext', ['yaml', 'yml'])
def test_load_valid_yaml(tmp_path, write_props, ext):
  path = write_props(tmp_path, ext, 'app_name: foo\nroute53:\n  ttl: 60\n')
  assert load_properties(path) == {'app_name': 'foo', 'route53': {'ttl': 60}}


def test_load_malformed_json(tmp_path, write_props):
  path = write_props(tmp_path, 'json', '{"app_name": ')
  with pytest.raises(PropertiesError) as exc:
    load_properties(path)
  assert str(exc.value).startswith('Failed to parse {}: '.format(path))


def test_load_malformed_yaml(tmp_path, write_props):
  path = write_props(tmp_path, 'yaml', 'app_name: foo\n  bad: [\n')
  with pytest.raises(PropertiesError) as exc:
    load_properties(path)
  assert str(exc.value).startswith('Failed to parse {}: '.format(path))


def test_load_empty_yaml_rejected(tmp_path, write_props):
  path = write_props(tmp_path, 'yaml', '')
  with pytest.raises(PropertiesError) as exc:
    load_properties(path)
  assert str(exc.value) == '{} must contain a top-level mapping (got NoneType)'.format(path)


def test_load_empty_json_rejected(tmp_path, write_props):
  path = write_props(tmp_path, 'json', '')
  with pytest.raises(PropertiesError) as exc:
    load_properties(path)
  assert path in str(exc.value)


@pytest.mark.parametrize('ext,content,type_name', [
  ('json', '["a", "b"]', 'list'),
  ('yaml', '- a\n- b\n', 'list'),
  ('json', '"foo"', 'str'),
  ('yaml', '42\n', 'int'),
])
def test_load_non_mapping_rejected(tmp_path, write_props, ext, content, type_name):
  path = write_props(tmp_path, ext, content)
  with pytest.raises(PropertiesError) as exc:
    load_properties(path)
  assert str(exc.value) == '{} must contain a top-level mapping (got {})'.format(path, type_name)


def test_load_yaml_rejects_python_object_tags(tmp_path, write_props):
  marker = tmp_path / 'pwned'
  path = write_props(
    tmp_path, 'yaml',
    "app_name: !!python/object/apply:os.system ['touch {}']\n".format(marker),
  )
  with pytest.raises(PropertiesError) as exc:
    load_properties(path)
  assert str(exc.value).startswith('Failed to parse {}: '.format(path))
  assert not marker.exists()


def test_missing_pyyaml_raises_install_hint(tmp_path, write_props, monkeypatch):
  monkeypatch.setitem(sys.modules, 'yaml', None)
  path = write_props(tmp_path, 'yaml', 'app_name: foo\n')
  with pytest.raises(PropertiesError) as exc:
    load_properties(path)
  assert str(exc.value) == "PyYAML is required to read {}; install with: pip install 'tfwrap[yaml]'".format(path)


def test_missing_pyyaml_json_still_loads(tmp_path, write_props, monkeypatch):
  monkeypatch.setitem(sys.modules, 'yaml', None)
  path = write_props(tmp_path, 'json', '{"app_name": "foo"}')
  assert load_properties(path) == {'app_name': 'foo'}
