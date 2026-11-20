"""Publish a bounded local artifact set with a durable journal and rollback.

Individual replacements are atomic; readers requiring a consistent *set* must use
the same transaction lock or wait for publication to finish. A process interruption
is recovered on the next explicit recovery/publication call. This is not a claim of
instant multi-file filesystem atomicity or protection from uncooperative writers.
"""
from contextlib import contextmanager
import hashlib
import json
import ntpath
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import uuid


class TransactionError(RuntimeError):
    pass


class TransactionBusyError(TransactionError):
    pass


WORKSPACE = 'ia/.runtime/artifact_transaction'
LOCK_FILE = 'ia/.runtime/artifact_transaction.lock'
_ALLOWED = re.compile(
    r'(?:ia/config/[^/]+\.json|data/demo_company/derived/[^/]+\.(?:json|csv)|'
    r'data/demo_company/runs/가상센터/(?:report\.md|agent_state\.json))\Z')
_SHA = re.compile(r'[a-f0-9]{64}\Z')


def _root(root):
    value = Path(root).resolve()
    if not value.is_dir():
        raise TransactionError('project_root_required')
    return value


def _contained(root, relative):
    """Reject redirected ancestors (including junctions), not just outside paths."""
    path = root.joinpath(*PurePosixPath(relative).parts)
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or resolved != path or path.is_symlink():
        raise TransactionError('redirected_or_outside_path:' + relative)
    return path


def _target(root, relative):
    if (not isinstance(relative, str) or not relative or '\\' in relative or ':' in relative
            or ntpath.splitdrive(relative)[0] or relative.startswith('/')
            or any(p in ('', '.', '..') for p in relative.split('/'))
            or not _ALLOWED.fullmatch(relative)):
        raise TransactionError('target_not_allowed:' + str(relative))
    path = _contained(root, relative)
    if path.exists() and not path.is_file():
        raise TransactionError('target_is_not_file:' + relative)
    return path


def _workspace(root):
    return _contained(root, WORKSPACE)


def _sha_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _sha_file(path):
    if not path.exists():
        return None
    if not path.is_file() or path.is_symlink():
        raise TransactionError('unexpected_artifact_type:' + str(path))
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _write_bytes(path, payload):
    with path.open('wb') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _write_manifest(workspace, manifest):
    temp = workspace/'manifest.json.tmp'
    _write_bytes(temp, (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
    os.replace(temp, workspace/'manifest.json')


def _cleanup_workspace(root):
    workspace = _workspace(root)
    # Resolve the exact recursive-delete boundary immediately before deletion.
    if workspace.exists():
        if workspace.resolve() != root/WORKSPACE or not workspace.is_dir():
            raise TransactionError('unsafe_transaction_workspace')
        shutil.rmtree(workspace)


@contextmanager
def _transaction_lock(root):
    runtime = _contained(root, 'ia/.runtime')
    runtime.mkdir(parents=True, exist_ok=True)
    path = _contained(root, LOCK_FILE)
    with path.open('a+b') as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b'0');stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise TransactionBusyError('artifact_transaction_busy') from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    # Keep the one-byte lock file. Unlinking it races with a waiting lock holder.


def _load_manifest(root, workspace):
    def unique_pairs(pairs):
        result = {}
        for key,value in pairs:
            if key in result:
                raise TransactionError('duplicate_manifest_key')
            result[key] = value
        return result
    try:
        manifest = json.loads((workspace/'manifest.json').read_text('utf-8'), object_pairs_hook=unique_pairs)
    except (ValueError, OSError) as exc:
        raise TransactionError('invalid_transaction_manifest') from exc
    if (not isinstance(manifest, dict) or manifest.get('schema_version') != 1
            or manifest.get('phase') not in ('prepared','publishing','committed')
            or not isinstance(manifest.get('entries'), list) or not manifest['entries']
            or not re.fullmatch(r'[a-f0-9]{32}', str(manifest.get('transaction_id', '')))):
        raise TransactionError('invalid_transaction_manifest')
    targets = set()
    for index,entry in enumerate(manifest['entries']):
        if not isinstance(entry, dict):
            raise TransactionError('invalid_transaction_entry')
        target = entry.get('target')
        _target(root, target)
        if target.casefold() in targets:
            raise TransactionError('duplicate_transaction_target')
        targets.add(target.casefold())
        before,after = entry.get('before_sha256'),entry.get('after_sha256')
        if (before is not None and not isinstance(before,str)) or not isinstance(after,str):
            raise TransactionError('invalid_transaction_hash')
        if (before is not None and not _SHA.fullmatch(before)) or not _SHA.fullmatch(after):
            raise TransactionError('invalid_transaction_hash')
        if entry.get('payload') != f'payload/{index:06}.bin':
            raise TransactionError('invalid_payload_path')
        if entry.get('backup') != (f'backup/{index:06}.bin' if before is not None else None):
            raise TransactionError('invalid_backup_path')
        for key in ('payload','backup'):
            if entry.get(key):
                _contained(root, WORKSPACE + '/' + entry[key])
    return manifest


def _verify_targets(root, manifest, allowed):
    actual = {}
    for entry in manifest['entries']:
        target = _target(root, entry['target'])
        value = _sha_file(target)
        if value not in allowed(entry):
            raise TransactionError('target_changed_during_transaction:' + entry['target'])
        actual[entry['target']] = value
    return actual


def _verify_backups(root, workspace, manifest):
    for entry in manifest['entries']:
        if entry['before_sha256'] is not None:
            backup = _contained(root, WORKSPACE + '/' + entry['backup'])
            if _sha_file(backup) != entry['before_sha256']:
                raise TransactionError('backup_changed_or_missing:' + entry['target'])


def _recover_locked(root):
    workspace = _workspace(root)
    if not workspace.exists():
        return {'status':'nothing_to_recover'}
    if not workspace.is_dir():
        raise TransactionError('unsafe_transaction_workspace')
    if not (workspace/'manifest.json').exists():
        # Publication cannot start before a durable manifest exists.
        if any(p.name not in ('payload','backup','manifest.json.tmp') for p in workspace.iterdir()):
            raise TransactionError('unrecognized_transaction_workspace')
        _cleanup_workspace(root)
        return {'status':'discarded_unpublished_staging'}
    manifest = _load_manifest(root, workspace)
    transaction_id = manifest['transaction_id']
    if manifest['phase'] == 'prepared':
        # No replacement is permitted in this phase; do not undo external changes.
        _verify_targets(root, manifest, lambda e:{e['before_sha256']})
        _cleanup_workspace(root)
        return {'status':'discarded_prepared','transaction_id':transaction_id}
    if manifest['phase'] == 'committed':
        _verify_targets(root, manifest, lambda e:{e['after_sha256']})
        _cleanup_workspace(root)
        return {'status':'committed_cleanup','transaction_id':transaction_id}
    # Validate every target and backup before restoring even the first file.
    _verify_targets(root, manifest, lambda e:{e['before_sha256'],e['after_sha256']})
    _verify_backups(root, workspace, manifest)
    for index,entry in enumerate(manifest['entries']):
        target = _target(root, entry['target'])
        current = _sha_file(target)
        if current == entry['before_sha256']:
            continue
        if current != entry['after_sha256']:
            raise TransactionError('target_changed_during_recovery:' + entry['target'])
        if entry['before_sha256'] is None:
            target.unlink()
        else:
            backup = _contained(root, WORKSPACE + '/' + entry['backup'])
            payload = backup.read_bytes()
            if _sha_bytes(payload) != entry['before_sha256']:
                raise TransactionError('backup_changed_during_recovery:' + entry['target'])
            restore = workspace/f'restore-{index:06}.tmp'
            _write_bytes(restore, payload)
            # Keep backups intact so recovery itself can be interrupted and retried.
            os.replace(restore, target)
    _verify_targets(root, manifest, lambda e:{e['before_sha256']})
    _cleanup_workspace(root)
    return {'status':'rolled_back','transaction_id':transaction_id,
            'files':[e['target'] for e in manifest['entries']]}


def recover_pending(root):
    """Recover an interrupted publication; never overwrite an unrecognized version."""
    root = _root(root)
    with _transaction_lock(root):
        return _recover_locked(root)


def _replace_target(source, target):
    os.replace(source, target)


def publish_files(root, files_mapping, expected_files=None):
    """Publish {allowed_relative_target: bytes}; exceptions restore the prior set.

    A rollback blocked by an independently changed target preserves the journal and
    backups and raises TransactionError. The caller must report that blocked state.
    No source/archive file is an allowed target. Caller payloads are never decoded.
    """
    root = _root(root)
    if not isinstance(files_mapping, dict) or not files_mapping:
        raise TransactionError('nonempty_payload_mapping_required')
    prepared = {};seen = set()
    for relative,payload in files_mapping.items():
        _target(root, relative)
        if not isinstance(payload, bytes):
            raise TransactionError('bytes_payload_required:' + relative)
        if relative.casefold() in seen:
            raise TransactionError('duplicate_transaction_target')
        seen.add(relative.casefold());prepared[relative] = payload
    with _transaction_lock(root):
        previous = _recover_locked(root)
        # Compare under the publication lock; a later calculation cannot overwrite
        # a newer completed result based on an older baseline.
        for relative, expected in (expected_files or {}).items():
            target=_target(root,relative)
            if expected is not None and (not isinstance(expected,str) or not _SHA.fullmatch(expected.lower())):
                raise TransactionError('invalid_expected_hash:'+relative)
            if _sha_file(target)!=(expected.lower() if expected is not None else None):
                raise TransactionError('publication_baseline_changed:'+relative)
        workspace = _workspace(root)
        workspace.mkdir()
        (workspace/'payload').mkdir();(workspace/'backup').mkdir()
        manifest = {'schema_version':1,'transaction_id':uuid.uuid4().hex,
                    'phase':'prepared','entries':[]}
        try:
            for index,(relative,payload) in enumerate(sorted(prepared.items())):
                target = _target(root, relative)
                prior = target.read_bytes() if target.is_file() else None
                before = _sha_bytes(prior) if prior is not None else None
                entry = {'target':relative,'before_sha256':before,'after_sha256':_sha_bytes(payload),
                         'payload':f'payload/{index:06}.bin',
                         'backup':f'backup/{index:06}.bin' if prior is not None else None}
                _write_bytes(workspace/entry['payload'],payload)
                if prior is not None:
                    _write_bytes(workspace/entry['backup'],prior)
                manifest['entries'].append(entry)
            _verify_targets(root,manifest,lambda e:{e['before_sha256']})
            _write_manifest(workspace,manifest)
            manifest['phase'] = 'publishing'
            _write_manifest(workspace,manifest)
            for entry in manifest['entries']:
                target = _target(root,entry['target'])
                if _sha_file(target) != entry['before_sha256']:
                    raise TransactionError('target_changed_before_publish:' + entry['target'])
                if entry['before_sha256'] == entry['after_sha256']:
                    continue
                target.parent.mkdir(parents=True,exist_ok=True)
                _target(root,entry['target'])
                staged = _contained(root,WORKSPACE + '/' + entry['payload'])
                if _sha_file(staged) != entry['after_sha256']:
                    raise TransactionError('payload_changed_before_publish:' + entry['target'])
                _replace_target(staged,target)
            _verify_targets(root,manifest,lambda e:{e['after_sha256']})
            manifest['phase'] = 'committed'
            _write_manifest(workspace,manifest)
            _cleanup_workspace(root)
            return {'status':'committed','transaction_id':manifest['transaction_id'],
                    'files':{e['target']:e['after_sha256'] for e in manifest['entries']},
                    'changed_files':[e['target'] for e in manifest['entries'] if e['before_sha256'] != e['after_sha256']],
                    'recovered_previous':previous}
        except BaseException as exc:
            try:
                _recover_locked(root)
            except BaseException as recovery_exc:
                raise TransactionError('publication_failed_recovery_blocked:' + str(recovery_exc)) from exc
            raise
