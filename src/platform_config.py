"""Shared real-path configuration. Source provenance remains unchanged."""
from pathlib import Path
import tomllib
import subprocess
from importlib.metadata import version, PackageNotFoundError

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / 'config' / 'paths.toml'


def settings():
    with CONFIG_FILE.open('rb') as stream:
        result = tomllib.load(stream)
    if result.get('config_version') != 1:
        raise ValueError('不支持的平台路径配置版本。')
    local = CONFIG_FILE.with_name('paths.local.toml')
    if local.is_file():
        with local.open('rb') as stream:
            result['source_roots'] = result.get('source_roots', []) + tomllib.load(stream).get('source_roots', [])
    return result


def project_path(key):
    value = Path(settings()['paths'][key]).expanduser()
    return (ROOT / value).resolve() if not value.is_absolute() else value.resolve()


def database_id(kind, path):
    """Only the configured database receives the persistent project identity."""
    return settings()['database_ids'][kind] if Path(path).resolve() == project_path(kind + '_database') else None


def locate_database(kind, identity, saved_path):
    if identity and identity == settings()['database_ids'][kind]:
        return project_path(kind + '_database')
    return Path(saved_path).resolve()


def require_database(kind):
    path = project_path(kind + '_database')
    if not path.is_file():
        raise FileNotFoundError(f'未找到配置的数据库：{path}。请恢复数据或修改 config/paths.toml。')
    return path


def state_path(filename):
    folder = project_path('state')
    folder.mkdir(parents=True, exist_ok=True)
    return folder / filename


def resolve_source_path(original, kind):
    """Resolve archived source names to real files without rewriting source records."""
    source = Path(original)
    if not source.is_absolute():
        base = project_path(kind + '_raw')
        target = (base / source).resolve()
        if not target.is_relative_to(base):
            raise ValueError('原始文件的相对路径超出数据目录。')
        return target
    for mapping in settings().get('source_roots', []):
        try:
            relative = source.relative_to(Path(mapping['original']))
        except ValueError:
            continue
        base = (ROOT / mapping['current']).resolve()
        target = (base / relative).resolve()
        if not target.is_relative_to(base):
            raise ValueError('原始文件映射超出数据目录。')
        return target
    return source.resolve()


def battery_mapping_paths():
    base = project_path('battery_raw')
    return [p for p in (base/'21700测试结果/21700电池记录.xlsx',
                       base/'18650测试结果/18650电池记录.xlsx',
                       base/'软包电芯/软包电芯记录.xlsx', base/'其他电池记录.xlsx') if p.is_file()]


def software_identity():
    try:
        release = version('uav-performance-platform')
    except PackageNotFoundError:
        release = 'source checkout'
    identity = {'package': 'uav-performance-platform', 'version': release}
    try:
        kwargs = {'cwd': ROOT, 'capture_output': True, 'text': True, 'check': True, 'timeout': 3,
                  'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)}
        identity['git_commit'] = subprocess.run(['git', 'rev-parse', 'HEAD'], **kwargs).stdout.strip()
        identity['working_tree_modified'] = bool(subprocess.run(['git', 'status', '--porcelain'], **kwargs).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        identity['git_commit'] = None
    return identity
