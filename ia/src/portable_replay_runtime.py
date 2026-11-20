"""Archived-calculation execution on the current OS, without claiming OS cloning."""
import importlib.metadata
import platform
from pathlib import Path
import re
import sys


def environment():
    # Engine dependencies only; UI/AI/OCR do not run during a saved rule calculation.
    names=('pyxlsb','lxml','pypdf')
    packages={}
    for name in names:
        try:packages[name]=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:packages[name]=None
    return {'python':platform.python_version(),'implementation':platform.python_implementation(),
            'system':platform.system(),'machine':platform.machine(),'packages':packages}


def choose(manifest,stage):
    archived=manifest.get('environment')
    binary=stage/'ia/.runtime/python/python.exe'
    if platform.system()=='Windows' and binary.is_file():
        return str(binary),'archived_windows_runtime'
    current=environment()
    if archived:
        if archived['python']!=current['python'] or archived['implementation']!=current['implementation']:
            raise ValueError('과거 계산에 필요한 Python 버전이 다릅니다. 보관 버전 '+archived['python']+' 환경에서 다시 실행하세요. 현재 자료는 유지됩니다.')
        differences=[name for name,version in archived['packages'].items() if current['packages'].get(name)!=version]
        if differences:
            raise ValueError('과거 계산 라이브러리 버전이 다릅니다: '+', '.join(differences)+'. 보관된 requirements.txt를 사용하는 별도 설치본에서 재현하세요.')
    elif not binary.is_file():
        raise ValueError('재현 환경 정보와 과거 Windows 실행 파일이 모두 없습니다.')
    # Legacy v1 has no recorded dependency versions: compare complete results and
    # explicitly report unknown environment identity, never exact OS reproduction.
    return sys.executable,('portable_version_checked' if archived else 'legacy_portable_result_checked')


def description(mode):
    if mode=='archived_windows_runtime':
        return '보관된 원본·코드·Windows Python 실행환경으로 격리 재계산했고 저장 결과와 일치했습니다. 현재 자료·결정은 유지합니다.'
    suffix='과거 라이브러리 버전 정보가 없는 이전 백업입니다. ' if mode=='legacy_portable_result_checked' else 'Python·계산 라이브러리 버전도 확인했습니다. '
    return '보관된 원본·코드·기준을 현재 운영체제에서 격리 재계산했고 전체 저장 결과와 일치했습니다. '+suffix+'Windows 운영체제 자체를 복제한 결과는 아닙니다. 현재 자료·결정은 유지합니다.'
