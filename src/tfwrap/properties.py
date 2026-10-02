"""Discovery and loading of per-environment properties files."""

import json
import logging
import os

PROPERTIES_EXTENSIONS = ('json', 'yaml', 'yml')


class PropertiesError(Exception):
  """Raised when a properties file is missing, unparsable, not a mapping, or needs PyYAML."""


def candidate_paths(env, base_dir):
  """Return the ordered list of properties.<env>.<ext> paths under base_dir."""
  return [os.path.join(base_dir, 'properties.{}.{}'.format(env, ext)) for ext in PROPERTIES_EXTENSIONS]


def find_properties_file(env, base_dir):
  """Return the first existing candidate path (or None); log a warning if several exist."""
  existing = [p for p in candidate_paths(env, base_dir) if os.path.isfile(p)]
  if not existing:
    return None
  if len(existing) > 1:
    logging.warning(
      "Found multiple properties files for env '%s': %s. Using %s.",
      env, ', '.join(existing), existing[0],
    )
  return existing[0]


def _parse_json(path):
  with open(path, 'r') as f:
    try:
      return json.load(f)
    except ValueError as e:
      raise PropertiesError('Failed to parse {}: {}'.format(path, e))


def _parse_yaml(path):
  try:
    import yaml
  except ImportError:
    raise PropertiesError("PyYAML is required to read {}; install with: pip install 'tfwrap[yaml]'".format(path))
  with open(path, 'r') as f:
    try:
      return yaml.safe_load(f)
    except yaml.YAMLError as e:
      raise PropertiesError('Failed to parse {}: {}'.format(path, e))


def load_properties(path):
  """Parse by extension and return a dict; raise PropertiesError on any failure."""
  if path.endswith('.json'):
    data = _parse_json(path)
  else:
    data = _parse_yaml(path)
  if not isinstance(data, dict):
    raise PropertiesError('{} must contain a top-level mapping (got {})'.format(path, type(data).__name__))
  return data
