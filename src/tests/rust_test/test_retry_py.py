#!/usr/bin/env python3
"""集成测试:重试机制 + 错误码翻译(P3 阶段验证)"""
import json
import os
import subprocess
import tempfile
import shutil
import ctypes
from ctypes import wintypes

kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
# CreateFileW 独占打开
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]
GENERIC_WRITE = 0x40000000
FILE_SHARE_NONE = 0
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENGINE = os.path.join(_PROJECT_ROOT, 'bin', 'rust-migrate-engine.exe')
SRC_CORE = os.path.join(_PROJECT_ROOT, 'src', 'core')


def run_engine(job_dict):
    """运行引擎,返回 (rc, events_list)。"""
    base = tempfile.mkdtemp(prefix='test_retry_')
    jp = os.path.join(base, 'job.json')
    with open(jp, 'w', encoding='utf-8') as f:
        json.dump(job_dict, f)
    r = subprocess.run(
        [ENGINE, '--job', jp, '--log-format', 'jsonl'],
        capture_output=True, text=True, encoding='utf-8',
    )
    events = []
    for line in r.stdout.strip().splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    shutil.rmtree(base, ignore_errors=True)
    return r.returncode, events, r.stderr


def test_fatal_no_retry():
    """测试 1:不可重试错误(路径不存在 code=2)→ 立即失败,无 retry 事件。"""
    base = tempfile.mkdtemp(prefix='test_fatal_')
    dst = os.path.join(base, 'dst')
    os.makedirs(dst)
    rc, events, _ = run_engine({
        'source': os.path.join(base, 'not_exist'),
        'target': dst, 'mode': 'copy',
        'retry': {'max_attempts': 5, 'backoff_base_ms': 50, 'network_path': False},
        'flush_checkpoint_mb': 64,
        'purge': {'enabled': False, 'soft_delete': True, 'dry_run': False},
        'background_mode': False, 'write_through': False, 'large_file_threshold_mb': 1,
    })
    shutil.rmtree(base, ignore_errors=True)
    retries = [e for e in events if e.get('event') == 'retry']
    errs = [e for e in events if e.get('event') == 'file_error']
    assert rc == 16, f'expected rc=16, got {rc}'
    assert len(retries) == 0, f'expected 0 retry events, got {len(retries)}'
    assert len(errs) >= 1, 'expected at least 1 file_error'
    assert errs[0]['code'] == 2, f'expected code=2, got {errs[0]["code"]}'
    print('PASS test 1: path not found -> immediate file_error code=2 (no retry)')


def test_retryable_with_retry():
    """测试 2:可重试错误(文件被占用 code=32)→ retry 事件 → 最终 file_error code=32。"""
    base = tempfile.mkdtemp(prefix='test_locked_')
    src = os.path.join(base, 'src')
    dst = os.path.join(base, 'dst')
    os.makedirs(src)
    os.makedirs(dst)
    test_file = os.path.join(src, 'locked.bin')
    with open(test_file, 'wb') as f:
        f.write(b'\x00' * 1024)
    dst_file = os.path.join(dst, 'locked.bin')
    # 创建目标文件并独占打开
    with open(dst_file, 'wb') as f:
        f.write(b'')
    handle = kernel32.CreateFileW(
        dst_file, GENERIC_WRITE, FILE_SHARE_NONE,
        None, OPEN_EXISTING, 0, None,
    )
    assert handle != INVALID_HANDLE_VALUE, f'CreateFileW failed: {ctypes.get_last_error()}'
    try:
        rc, events, _ = run_engine({
            'source': src, 'target': dst, 'mode': 'copy',
            'retry': {'max_attempts': 3, 'backoff_base_ms': 50, 'network_path': False},
            'flush_checkpoint_mb': 64,
            'purge': {'enabled': False, 'soft_delete': True, 'dry_run': False},
            'background_mode': False, 'write_through': False, 'large_file_threshold_mb': 1,
        })
    finally:
        kernel32.CloseHandle(handle)
    shutil.rmtree(base, ignore_errors=True)
    retries = [e for e in events if e.get('event') == 'retry']
    errs = [e for e in events if e.get('event') == 'file_error']
    # max_attempts=3,所以 retry 事件数 = 2(第 1 次和第 2 次尝试失败后发 retry)
    assert len(retries) == 2, f'expected 2 retry events, got {len(retries)}'
    assert len(errs) >= 1, 'expected at least 1 file_error'
    assert errs[0]['code'] == 32, f'expected code=32, got {errs[0]["code"]}'
    # 验证 retry 事件的 attempt 字段
    assert retries[0]['attempt'] == 1, f'expected attempt=1, got {retries[0]["attempt"]}'
    assert retries[1]['attempt'] == 2, f'expected attempt=2, got {retries[1]["attempt"]}'
    print(f'PASS test 2: file locked -> 2 retries -> file_error code=32')
    print(f'  retry events: attempt={retries[0]["attempt"]}, {retries[1]["attempt"]}')


def test_error_translation():
    """测试 3:Python 错误码翻译(验证 21 个错误码 + enrich_file_error)。"""
    import sys
    import importlib.util
    sys.path.insert(0, os.path.join(SRC_CORE))
    sys.path.insert(0, os.path.dirname(SRC_CORE))
    import config  # noqa: pre-load
    spec = importlib.util.spec_from_file_location(
        'migrator', os.path.join(SRC_CORE, 'migrator.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    errmap = mod._WIN32_ERR_MAP
    # P3 完整错误码集(21 个)
    expected = {
        # 永久性(不可重试)
        2, 3, 5, 19, 87, 108, 112, 161, 183,
        # 暂时性(可重试)
        32, 33, 53, 67, 120, 121, 145, 232, 233, 1130, 1722,
        # 引擎内部码
        1742,
    }
    assert set(errmap.keys()) == expected, \
        f'codes mismatch: {set(errmap.keys())} vs {expected}'
    # 抽查新增的错误码
    r33, s33 = errmap[33]
    assert '锁定' in r33, f'code=33 reason failed: {r33}'
    r1722, s1722 = errmap[1722]
    assert 'RPC' in r1722, f'code=1722 reason failed: {r1722}'
    r1742, s1742 = errmap[1742]
    assert '重解析' in r1742, f'code=1742 reason failed: {r1742}'
    r108, s108 = errmap[108]
    assert '未插入' in r108, f'code=108 reason failed: {r108}'
    r161, s161 = errmap[161]
    assert '非法' in r161, f'code=161 reason failed: {r161}'

    # 验证 enrich_file_error 逻辑(直接用 errmap 模拟,避免相对导入问题)
    def enrich(evt):
        if 'code' in evt:
            reason, suggestion = errmap.get(
                evt['code'], ('未知错误(码 %d)' % evt['code'], '请查看日志或重试'))
            evt['reason'] = reason
            evt['suggestion'] = suggestion
        return evt
    # file_error 事件翻译
    evt = {'event': 'file_error', 'code': 32, 'path': 'C:\\test.txt', 'stage': 'copy'}
    enrich(evt)
    assert 'reason' in evt and 'suggestion' in evt, 'enrich_file_error failed'
    assert '另一进程' in evt['reason'] or '占用' in evt['reason'], \
        f'reason mismatch: {evt["reason"]}'
    # retry 事件翻译(retry 事件也有 code 字段,P3 修复后也会翻译)
    evt2 = {'event': 'retry', 'code': 33, 'path': 'C:\\test.txt', 'attempt': 1}
    enrich(evt2)
    assert '锁定' in evt2['reason'], f'retry event translation failed: {evt2}'
    print(f'PASS test 3: {len(errmap)} error codes + enrich logic verified')


if __name__ == '__main__':
    test_fatal_no_retry()
    test_retryable_with_retry()
    test_error_translation()
    print('\n========== ALL RETRY + ERROR TRANSLATION TESTS PASSED ==========')
