"""Build and verify exact distributions and independent installs."""

import argparse
import base64
import csv
from email.parser import BytesParser
from email.policy import default
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import venv
import zipfile

PACKAGE_FILES = {'requ/__init__.py', 'requ/_pool.py', 'requ/py.typed'}
TEST_FILES = {'test_requ.py', 'test_transport.py', 'test_async.py'}
TEST_DATA_FILES = set()
EXAMPLE_FILES = {'local_http.py'}
DIAGNOSTIC_FILES = set()
SOURCE_FILES = ({'README.md', 'LICENSE', 'CHANGELOG.md', 'docs/api.md',
                 'scripts/check_distribution.py'}
                | {f'src/{name}' for name in PACKAGE_FILES}
                | {f'tests/{name}' for name in TEST_FILES}
                | {f'examples/{name}' for name in EXAMPLE_FILES})


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def run(args: list[str], *, cwd: Path, capture: bool = False) -> str:
    env = os.environ.copy()
    env.pop('PYTHONPATH', None)
    env.pop('VIRTUAL_ENV', None)
    result = subprocess.run(args, cwd=cwd, env=env, text=True, check=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout if capture else ''


def wheel_files(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        entries = [entry for entry in archive.infolist() if not entry.is_dir()]
        names = [entry.filename for entry in entries]
        require(len(names) == len(set(names)), 'duplicate wheel entries')
        return {name: archive.read(name) for name in names}


def source_files(path: Path, prefix: str) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    with tarfile.open(path, 'r:gz') as archive:
        for member in archive:
            require(not member.issym() and not member.islnk(), 'links are forbidden in sdist')
            require(member.isdir() or member.isfile(), 'unexpected sdist member type')
            if member.isdir():
                continue
            require(member.name.startswith(prefix + '/'), 'unexpected sdist root')
            name = member.name.removeprefix(prefix + '/')
            require(name not in result, 'duplicate sdist member')
            file = archive.extractfile(member)
            require(file is not None, 'unreadable sdist member')
            with file:
                result[name] = file.read()
    return result


def verify_metadata(data: bytes, readme: bytes, version: str) -> None:
    metadata = BytesParser(policy=default).parsebytes(data)
    for key, expected in {'Name': 'requ', 'Version': version,
                          'Requires-Python': '>=3.14', 'License-Expression': 'MIT',
                          'Description-Content-Type': 'text/markdown'}.items():
        require(metadata.get(key) == expected, f'incorrect metadata: {key}')
    require(not metadata.get_all('Requires-Dist'), 'runtime dependencies must be empty')
    require(metadata.get_all('License-File') == ['LICENSE'], 'incorrect license metadata')
    require('Typing :: Typed' in metadata.get_all('Classifier', []), 'missing typing classifier')
    payload = metadata.get_payload(decode=True)
    require(payload is not None, 'README metadata payload is missing')
    require(payload.decode('utf-8').strip() == readme.decode('utf-8').strip(), 'README metadata differs')


def verify_record(files: dict[str, bytes], dist_info: str) -> None:
    record = f'{dist_info}/RECORD'
    rows = list(csv.reader(io.StringIO(files[record].decode())))
    require(len(rows) == len(files), 'RECORD file count differs')
    require(len({row[0] for row in rows}) == len(rows), 'duplicate RECORD paths')
    require({row[0] for row in rows} == set(files), 'RECORD paths differ')
    for name, digest, size in rows:
        if name == record:
            require(digest == size == '', 'RECORD must not hash itself')
            continue
        expected = base64.urlsafe_b64encode(hashlib.sha256(files[name]).digest()).rstrip(b'=').decode()
        require(digest == f'sha256={expected}', f'RECORD digest differs: {name}')
        require(size == str(len(files[name])), f'RECORD size differs: {name}')


def verify_install(artifact: Path, sandbox: Path, root: Path, version: str) -> None:
    sandbox.mkdir()
    environment = sandbox / 'environment'
    venv.EnvBuilder(with_pip=False).create(environment)
    python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    # Source installation may resolve only the already-declared build backend.
    args = ['uv', 'pip', 'install', '--python', str(python), '--no-deps']
    if artifact.suffix == '.whl':
        args.append('--no-index')
    run([*args, str(artifact)], cwd=sandbox)
    probe = (
        'import requ, importlib.metadata, json; '
        'print(json.dumps({"file": requ.__file__, "version": requ.__version__, '
        '"metadata": importlib.metadata.version("requ"), '
        '"requirements": importlib.metadata.requires("requ")}))'
    )
    installed = json.loads(run([str(python), '-I', '-c', probe], cwd=sandbox, capture=True))
    require(Path(installed['file']).resolve().is_relative_to(environment.resolve()),
            'package imported from outside its isolated environment')
    require(installed['version'] == installed['metadata'] == version, 'installed version differs')
    require(installed['requirements'] in (None, []), 'installed runtime requirements are nonempty')
    tests, examples = sandbox / 'tests', sandbox / 'examples'
    tests.mkdir()
    examples.mkdir()
    for name in TEST_FILES:
        shutil.copyfile(root / 'tests' / name, tests / name)
    for name in TEST_DATA_FILES:
        target = tests / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / 'tests' / name, target)
    for name in EXAMPLE_FILES:
        shutil.copyfile(root / 'examples' / name, examples / name)
    scripts = sandbox / 'scripts'
    scripts.mkdir()
    for name in DIAGNOSTIC_FILES | {'check_distribution.py'}:
        shutil.copyfile(root / 'scripts' / name, scripts / name)
    run([str(python), '-I', '-W', 'error::ResourceWarning', '-m', 'unittest',
         'discover', '-s', str(tests), '-v'], cwd=sandbox)
    for name in sorted(EXAMPLE_FILES):
        run([str(python), '-I', str(examples / name)], cwd=sandbox)
    print(f'Independent install passed: {artifact.name}', flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', default='')
    parser.add_argument('--output-dir', type=Path, default=Path('dist'))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / 'pyproject.toml').read_text())
    version = config['project']['version']
    require(not args.tag or args.tag == f'v{version}',
            'release tag must match the package version')
    require(config['project']['dependencies'] == [], 'runtime dependencies must be empty')
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    wheel = output / f'requ-{version}-py3-none-any.whl'
    source = output / f'requ-{version}.tar.gz'
    require({path.name for path in output.iterdir()} <= {wheel.name, source.name, '.gitignore'},
            'output directory contains unexpected files; use an empty build directory')
    run(['uv', 'build', '--sdist', '--wheel', '--no-create-gitignore',
         '--out-dir', str(output), str(root)], cwd=root)
    require(wheel.is_file() and source.is_file(), 'expected distributions were not built')
    files = wheel_files(wheel)
    dist_info = f'requ-{version}.dist-info'
    expected_wheel = PACKAGE_FILES | {f'{dist_info}/{name}' for name in
                                      ('METADATA', 'WHEEL', 'RECORD', 'licenses/LICENSE')}
    require(set(files) == expected_wheel, f'wheel allowlist differs: {set(files) ^ expected_wheel}')
    for name in PACKAGE_FILES:
        require(files[name] == (root / 'src' / name).read_bytes(), f'wheel source differs: {name}')
    require(files[f'{dist_info}/licenses/LICENSE'] == (root / 'LICENSE').read_bytes(), 'wheel license differs')
    wheel_metadata = BytesParser(policy=default).parsebytes(files[f'{dist_info}/WHEEL'])
    require(wheel_metadata.get('Root-Is-Purelib') == 'true', 'wheel must be pure Python')
    require(wheel_metadata.get_all('Tag') == ['py3-none-any'], 'unexpected wheel tag')
    verify_metadata(files[f'{dist_info}/METADATA'], (root / 'README.md').read_bytes(), version)
    verify_record(files, dist_info)
    archived = source_files(source, f'requ-{version}')
    expected_source = SOURCE_FILES | {'PKG-INFO', 'pyproject.toml', 'pyproject.toml.orig'}
    require(set(archived) == expected_source, f'sdist allowlist differs: {set(archived) ^ expected_source}')
    for name in SOURCE_FILES:
        require(archived[name] == (root / name).read_bytes(), f'sdist source differs: {name}')
    require(archived['pyproject.toml.orig'] == (root / 'pyproject.toml').read_bytes(), 'original configuration differs')
    require(tomllib.loads(archived['pyproject.toml'].decode()) == config, 'sdist configuration semantics differ')
    require(archived['PKG-INFO'] == files[f'{dist_info}/METADATA'], 'sdist and wheel metadata differ')
    with tempfile.TemporaryDirectory(prefix='requ-distributions-') as temporary:
        sandbox = Path(temporary)
        rebuild = sandbox / 'rebuild'
        run(['uv', 'build', '--wheel', '--no-create-gitignore', '--out-dir', str(rebuild), str(source)], cwd=sandbox)
        rebuilt = wheel_files(rebuild / wheel.name)
        require(set(rebuilt) == expected_wheel, 'source rebuild allowlist differs')
        build_headers = {f'{dist_info}/WHEEL', f'{dist_info}/RECORD'}
        require({name: data for name, data in rebuilt.items() if name not in build_headers}
                == {name: data for name, data in files.items() if name not in build_headers},
                'source rebuild package, license or metadata bytes differ')
        rebuilt_metadata = BytesParser(policy=default).parsebytes(rebuilt[f'{dist_info}/WHEEL'])
        # The declared backend range can resolve a newer uv_build than the
        # invoking uv binary. Only the build-tool Generator header may differ.
        require([(key, value) for key, value in rebuilt_metadata.items() if key != 'Generator']
                == [(key, value) for key, value in wheel_metadata.items() if key != 'Generator'],
                'source rebuild wheel compatibility metadata differs')
        require(len(rebuilt_metadata.get_all('Generator', [])) == 1, 'missing build generator')
        verify_record(rebuilt, dist_info)
        verify_install(wheel, sandbox / 'wheel-install', root, version)
        verify_install(source, sandbox / 'source-install', root, version)
    print('Exact archives, metadata, RECORD, rebuild parity, installed tests and examples passed.')
    for artifact in (wheel, source):
        print(f'{hashlib.sha256(artifact.read_bytes()).hexdigest()}  {artifact.name}')


if __name__ == '__main__':
    main()
