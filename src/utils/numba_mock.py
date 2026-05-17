"""numba mock — pandas_ta가 numba를 optional import할 때 에러 방지.

PyInstaller exe 및 numba 미설치 환경에서 ImportError 없이 pandas_ta를
사용하기 위해 sys.modules에 더미 numba를 등록한다.

사용: pandas_ta import 전에 ensure_numba_mock()을 반드시 호출.
"""

import sys
import types


def ensure_numba_mock() -> None:
    """numba가 없으면 no-op 더미를 sys.modules에 등록."""
    if "numba" in sys.modules:
        return

    def _noop_decorator(f=None, **kwargs):
        if f is not None:
            return f
        return _noop_decorator

    mock = types.ModuleType("numba")
    mock.jit = _noop_decorator
    mock.njit = _noop_decorator
    mock.vectorize = _noop_decorator
    mock.prange = range
    sys.modules["numba"] = mock
