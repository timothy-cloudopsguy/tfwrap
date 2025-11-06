import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime

try:
  import boto3
  from botocore.exceptions import ClientError
except Exception:
  boto3 = None

# Configuration defaults (mirror the bash defaults)
ENV = os.environ.get('ENV', '')
REGION = os.environ.get('AWS_REGION', os.environ.get('AWS_DEFAULT_REGION', 'us-east-1'))
BUCKET_OVERRIDE = ''
TARGET_DIR = os.environ.get('TARGET_DIR', '.')
FORCE_COPY = False
APP_NAME_OVERRIDE = ''
FORCE_DELETE = False

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
log = logging.info
err = logging.error


def run_and_log(cmd, cwd=None, check=True):
  log('CMD: %s', ' '.join(cmd))
  try:
    subprocess.run(cmd, cwd=cwd, check=check)
  except subprocess.CalledProcessError:
    err('Command failed: %s', ' '.join(cmd))
    if check:
      sys.exit(1)


def confirm_prompt(prompt):
  if FORCE_DELETE:
    return True
  try:
    resp = input(f"{prompt} [y/N]: ")
  except EOFError:
    return False
  return resp.lower().startswith('y')


class TfWrapper:
  def __init__(self, env, region, target_dir):
    self.env = env or 'dev'
    self.region = region
    self.target_dir = target_dir or '.'
    self.bucket_override = ''
    self.force_copy = False
    self.app_name_override = ''
    self.force_delete = False

    self.app_name = ''
    self.safe_app_name = ''
    self.account_id = ''
    self.ssm_param_name = ''

    # boto3 clients (created lazily)
    self._ssm = None
    self._sts = None
    self._s3 = None

  @property
  def ssm(self):
    if self._ssm is None:
      if boto3 is None:
        err('boto3 is required for SSM operations; please install boto3')
        sys.exit(2)
      self._ssm = boto3.client('ssm', region_name=self.region)
    return self._ssm

  @property
  def sts(self):
    if self._sts is None:
      if boto3 is None:
        err('boto3 is required for STS operations; please install boto3')
        sys.exit(2)
      print(f'sts: {self.region}')
      self._sts = boto3.client('sts', region_name=self.region)
    return self._sts

  @property
  def s3(self):
    if self._s3 is None:
      if boto3 is None:
        err('boto3 is required for S3 operations; please install boto3')
        sys.exit(2)
      self._s3 = boto3.client('s3', region_name=self.region)
    return self._s3

  def synthesize_app_name_and_account(self):
    log('synthesize_app_name_and_account: BEGIN')
    if self.app_name_override:
      self.app_name = self.app_name_override
    else:
      props_path = f'properties.{self.env}.json'
      if os.path.isfile(props_path):
        try:
          with open(props_path, 'r') as f:
            props = json.load(f)
            self.app_name = props.get('app_name', '')
        except Exception:
          self.app_name = ''
      else:
        self.app_name = ''

    if not self.app_name:
      err("Unable to determine app name. Ensure properties.{}.json exists and contains an 'app_name' field, or provide --app-name.".format(self.env))
      sys.exit(2)

    self.safe_app_name = (self.app_name + self.env).lower()

    try:
      identity = self.sts.get_caller_identity()
      self.account_id = identity.get('Account', '')
    except ClientError:
      err('Unable to determine AWS account id. Ensure AWS credentials are configured.')
      sys.exit(2)

    if not self.account_id:
      err('Unable to determine AWS account id. Ensure AWS credentials are configured.')
      sys.exit(2)

    self.ssm_param_name = f"/terraform/backend/{self.account_id}-{self.safe_app_name}"

  def get_ssm_backend(self):
    try:
      resp = self.ssm.get_parameter(Name=self.ssm_param_name, WithDecryption=True)
      return resp['Parameter']['Value']
    except ClientError as e:
      if e.response['Error']['Code'] in ('ParameterNotFound',):
        return ''
      err('SSM get_parameter failed: %s', e)
      return ''

  def put_ssm_backend(self, value):
    try:
      self.ssm.put_parameter(Name=self.ssm_param_name, Value=value, Type='String', Overwrite=True)
    except ClientError as e:
      err('Failed to put SSM parameter: %s', e)
      sys.exit(1)

  def delete_ssm_backend(self):
    log('delete_ssm_backend: BEGIN')
    try:
      self.ssm.delete_parameter(Name=self.ssm_param_name)
      log('Deleted SSM parameter %s', self.ssm_param_name)
    except ClientError:
      log('SSM parameter %s not found or could not be deleted', self.ssm_param_name)

  def write_backend_hcl_to_file(self, content):
    log('write_backend_hcl_to_file: BEGIN')
    out_path = os.path.join(self.target_dir, 'backend.tf')
    with open(out_path, 'w') as f:
      f.write(content)
    log('Wrote %s', out_path)

  def write_local_backend_tf(self, dirpath):
    log('write_local_backend_tf: BEGIN')
    backend_tf_path = os.path.join(dirpath, 'backend.tf')
    content = '''terraform {
  backend "local" {
    path = "bootstrap.tfstate"
  }
}
'''
    with open(backend_tf_path, 'w') as f:
      f.write(content)
    log('Wrote local backend.tf at %s', backend_tf_path)

  def build_backend_content(self, bucket, region, account):
    return f'''terraform {{
  backend "s3" {{
    bucket = "{bucket}"
    key    = "terraform.{account}-{region}-{self.safe_app_name}.tfstate"
    region = "{region}"
    encrypt = true
    use_lockfile = true
  }}
}}
'''

  def erase_backend_tf(self, dirpath):
    log('erase_backend_tf: BEGIN')
    backend_tf_path = os.path.join(dirpath, 'backend.tf')
    if os.path.isfile(backend_tf_path):
      os.remove(backend_tf_path)
    log('Erased backend.tf at %s', backend_tf_path)

  def empty_s3_bucket(self, bucket, region=None):
    log('empty_s3_bucket: BEGIN')
    region = region or self.region
    if not bucket:
      log('No bucket name provided; skipping empty_s3_bucket')
      return

    log('Emptying S3 bucket %s in region %s', bucket, region)

    # Try simple recursive delete via CLI as a fast path (matching original behavior)
    try:
      run_and_log(['aws', 's3', 'rm', f's3://{bucket}', '--recursive', '--region', region], check=False)
    except Exception:
      pass

    # Now ensure versioned objects and delete markers are removed via API
    if boto3 is None:
      log('boto3 not available; skipping API-based emptying of versions')
      return

    s3 = boto3.client('s3', region_name=region)

    paginator = s3.get_paginator('list_object_versions')
    try:
      for page in paginator.paginate(Bucket=bucket):
        objects = []
        for v in page.get('Versions', []) + page.get('DeleteMarkers', []):
          objects.append({'Key': v['Key'], 'VersionId': v['VersionId']})
        if objects:
          for i in range(0, len(objects), 1000):
            chunk = objects[i:i+1000]
            s3.delete_objects(Bucket=bucket, Delete={'Objects': chunk, 'Quiet': False})
    except ClientError:
      log('list-object-versions failed or no versions; continuing')

    log('empty_s3_bucket: DONE for %s', bucket)

  def ensure_minimal_backend_tf(self, dirpath):
    log('ensure_minimal_backend_tf: BEGIN')
    backend_tf_path = os.path.join(dirpath, 'backend.tf')
    if not os.path.isfile(backend_tf_path):
      content = '''terraform {
  backend "s3" {}
}
'''
      with open(backend_tf_path, 'w') as f:
        f.write(content)
      log('Created minimal backend.tf at %s', backend_tf_path)
    else:
      log('Found existing backend.tf at %s', backend_tf_path)

  def run_terraform_init_with_backend_file(self):
    log('run_terraform_init_with_backend_file: BEGIN')
    cmd = ['terraform', 'init', '-reconfigure', '-input=false']
    if self.force_copy:
      cmd.append('-force-copy')
    run_and_log(cmd, cwd=self.target_dir)

  def run_bootstrap_and_create_ssm(self):
    log('run_bootstrap_and_create_ssm: BEGIN')
    candidates = [os.path.join(self.target_dir, 'bootstrap'), 'bootstrap']
    found = None
    for d in candidates:
      if os.path.isdir(d):
        found = d
        break
    if not found:
      err('Bootstrap directory not found in {}. Cannot bootstrap.'.format(' '.join(candidates)))
      sys.exit(1)

    log('Found bootstrap directory at %s. Running terraform init and apply...', found)
    self.erase_backend_tf(found)

    # terraform init/apply in bootstrap dir
    run_and_log(['terraform', 'init', '-input=false', '-reconfigure'], cwd=found)
    run_and_log(['terraform', 'apply', '-auto-approve', '-input=false', '-var', f'environment={self.env}', '-var', f'region={self.region}'], cwd=found)
    log('Bootstrap terraform apply completed in %s.', found)

    # Determine bucket name
    if self.bucket_override:
      bucket_name = self.bucket_override
    else:
      # Try terraform output -json
      bucket_name = ''
      try:
        out = subprocess.run(['terraform', 'output', '-json'], cwd=found, capture_output=True, text=True, check=True)
        jout = json.loads(out.stdout)
        if 'bucket_name' in jout and 'value' in jout['bucket_name']:
          bucket_name = jout['bucket_name']['value']
      except Exception:
        pass
      if not bucket_name:
        bucket_name = f"{self.account_id}-{self.safe_app_name}-tfstate"

    backend_content = self.build_backend_content(bucket_name, self.region, self.account_id)
    self.put_ssm_backend(backend_content)
    log('Stored backend configuration into SSM parameter %s', self.ssm_param_name)
    self.write_backend_hcl_to_file(backend_content)

  def ensure_backend_via_ssm_or_bootstrap(self):
    log('ensure_backend_via_ssm_or_bootstrap: BEGIN')
    ssm_value = self.get_ssm_backend() or ''
    if ssm_value and ssm_value != 'None':
      log('Found backend configuration in SSM %s', self.ssm_param_name)
      self.write_backend_hcl_to_file(ssm_value)
    else:
      log('Backend SSM parameter %s not found or empty. Running bootstrap to create backend and SSM entry.', self.ssm_param_name)
      self.run_bootstrap_and_create_ssm()

  def delete_top_level_stack(self):
    log('delete_top_level_stack: BEGIN')
    log('Destroying top-level stack in %s', self.target_dir)
    self.ensure_backend_via_ssm_or_bootstrap()
    self.run_terraform_init_with_backend_file()
    run_and_log(['terraform', 'destroy', '-auto-approve', '-var', f'environment={self.env}', '-var', f'region={self.region}'], cwd=self.target_dir)
    log('Top-level stack destroyed.')

  def delete_bootstrap_stack(self):
    log('delete_bootstrap_stack: BEGIN')
    candidates = [os.path.join(self.target_dir, 'bootstrap'), 'bootstrap']
    found = None
    for d in candidates:
      if os.path.isdir(d):
        found = d
        break
    if not found:
      log('Bootstrap directory not found; skipping bootstrap destroy.')
      return

    log('Preparing to destroy bootstrap resources in %s', found)
    ssm_value = self.get_ssm_backend() or ''

    # Extract bucket name from HCL-like content
    m = re.search(r'bucket\s*=\s*"([^"]+)"', ssm_value)
    bucket_name = m.group(1) if m else f"{self.account_id}-{self.safe_app_name}-tfstate"

    # Remove SSM param
    self.delete_ssm_backend()

    # Remove any temporary backend.tf left in the bootstrap dir
    self.erase_backend_tf(found)

    if bucket_name:
      log('Emptying bootstrap S3 bucket %s in region %s', bucket_name, self.region)
      try:
        self.empty_s3_bucket(bucket_name, self.region)
      except Exception:
        err('Failed to empty S3 bucket %s', bucket_name)
      log('Deleting bootstrap S3 bucket %s', bucket_name)
      try:
        self.s3.delete_bucket(Bucket=bucket_name)
        log('Deleted S3 bucket %s', bucket_name)
      except ClientError:
        err('Failed to delete S3 bucket %s. Please delete it manually if it still exists.', bucket_name)

    log('Bootstrap resources destroyed. If bucket deletion failed, empty and delete the S3 bucket manually.')

  def clean_terraform_files(self):
    log('clean_terraform_files: BEGIN')
    log('Cleaning Terraform files and directories from %s', self.target_dir)

    # Patterns to remove
    dirs_to_remove = ['.terraform']
    files_to_remove = ['.terraform.lock.hcl', 'backend.tf', 'terraform.tfstate', 'terraform.tfstate.backup']

    removed_count = 0

    for root, dirs, files in os.walk(self.target_dir, topdown=False):
      # Remove matching directories
      for dirname in dirs:
        if dirname in dirs_to_remove:
          dir_path = os.path.join(root, dirname)
          try:
            shutil.rmtree(dir_path)
            log('Removed directory: %s', dir_path)
            removed_count += 1
          except Exception as e:
            err('Failed to remove directory %s: %s', dir_path, e)

      # Remove matching files
      for filename in files:
        if filename in files_to_remove:
          file_path = os.path.join(root, filename)
          try:
            os.remove(file_path)
            log('Removed file: %s', file_path)
            removed_count += 1
          except Exception as e:
            err('Failed to remove file %s: %s', file_path, e)

    log('Clean completed. Removed %d items.', removed_count)


def parse_args(argv):
  cmds = ['bootstrap', 'init', 'plan', 'apply', 'destroy', 'destroy-all', 'clean']

  parser = argparse.ArgumentParser(prog='tfwrapper')
  parser.add_argument('command', nargs='?', choices=cmds, help='Command to run')
  parser.add_argument('-e', '--env', default=ENV)
  parser.add_argument('-r', '--region', default=REGION)
  parser.add_argument('--target-dir', default=TARGET_DIR)
  parser.add_argument('--force-copy', action='store_true')
  parser.add_argument('--app-name', default='')
  parser.add_argument('--force', action='store_true')

  args = parser.parse_args(argv)

  if not args.command:
    parser.print_help()
    sys.exit(0)

  return args.command, args


def main(argv):
  global FORCE_COPY, APP_NAME_OVERRIDE, FORCE_DELETE

  command, args = parse_args(argv)
  wrapper = TfWrapper(env=args.env, region=args.region, target_dir=args.target_dir)
  wrapper.force_copy = args.force_copy
  wrapper.app_name_override = args.app_name
  wrapper.force_delete = args.force
  wrapper.bucket_override = os.environ.get('BUCKET_OVERRIDE', '')
  wrapper.force_copy = args.force_copy
  wrapper.app_name_override = args.app_name
  wrapper.force_delete = args.force

  # Propagate some flags
  wrapper.force_copy = args.force_copy
  wrapper.app_name_override = args.app_name
  global FORCE_DELETE
  FORCE_DELETE = args.force
  wrapper.force_copy = args.force_copy
  wrapper.force_copy = args.force_copy
  wrapper.force_copy = args.force_copy

  if command == 'bootstrap':
    # synthesize
    wrapper.synthesize_app_name_and_account()
    wrapper.run_bootstrap_and_create_ssm()
    log('Bootstrap completed. You can now run terraform init/plan/apply in %s using the SSM-provided backend.', wrapper.target_dir)
  elif command == 'init':
    # synthesize
    wrapper.synthesize_app_name_and_account()
    wrapper.ensure_backend_via_ssm_or_bootstrap()
    wrapper.run_terraform_init_with_backend_file()
  elif command == 'plan':
    # synthesize
    wrapper.synthesize_app_name_and_account()
    wrapper.ensure_backend_via_ssm_or_bootstrap()
    wrapper.run_terraform_init_with_backend_file()
    run_and_log(['terraform', 'plan', '-input=false', '-var', f'environment={wrapper.env}', '-var', f'region={wrapper.region}'], cwd=wrapper.target_dir)
  elif command == 'apply':
    # synthesize
    wrapper.synthesize_app_name_and_account()
    wrapper.ensure_backend_via_ssm_or_bootstrap()
    wrapper.run_terraform_init_with_backend_file()
    run_and_log(['terraform', 'apply', '-auto-approve', '-input=false', '-var', f'environment={wrapper.env}', '-var', f'region={wrapper.region}'], cwd=wrapper.target_dir)
  elif command == 'destroy':
    # synthesize
    wrapper.synthesize_app_name_and_account()
    if confirm_prompt(f"Destroy the top-level stack in {wrapper.target_dir}? This will permanently delete resources."):
      wrapper.delete_top_level_stack()
    else:
      log('Aborted top-level destroy.')
  elif command == 'destroy-all':
    # synthesize
    wrapper.synthesize_app_name_and_account()
    if confirm_prompt(f"Destroy the top-level stack and bootstrap S3 bucket? This will permanently delete resources and remove the backend SSM entry."):
      wrapper.delete_top_level_stack()
      wrapper.delete_bootstrap_stack()
    else:
      log('Aborted destroy-all.')
  elif command == 'clean':
    if confirm_prompt(f"Clean Terraform files and directories from {wrapper.target_dir}? This will remove .terraform folders, .terraform.lock.hcl, backend.tf, and terraform.state files."):
      wrapper.clean_terraform_files()
    else:
      log('Aborted clean.')
  else:
    print('Unknown command')
    sys.exit(1)

  log('Done.')


def cli_main():
    """Entry point for command line interface."""
    try:
        main(sys.argv[1:])
    except SystemExit as e:
        sys.exit(e.code)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        err(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    cli_main() 