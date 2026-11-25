"""Per-process volume control via Windows Core Audio (no extra packages, plain ctypes COM).

The engine (uxplay.exe) plays sound through WASAPI, which gives it its own audio session —
the same one shown in the Windows volume mixer. Setting that session's volume works on top
of the iPhone's own volume buttons.
"""
import ctypes
import ctypes.wintypes as wt
import uuid

ole32 = ctypes.WinDLL("ole32")
ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wt.DWORD]
ole32.CoCreateInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p,
                                   ctypes.POINTER(ctypes.c_void_p)]

HRESULT = ctypes.c_long
CLSCTX_ALL = 0x17


class GUID(ctypes.Structure):
    _fields_ = [("data", ctypes.c_ubyte * 16)]

    def __init__(self, s):
        super().__init__()
        ctypes.memmove(self.data, uuid.UUID(s).bytes_le, 16)


CLSID_MMDeviceEnumerator = GUID("BCDE0395-E52F-467C-8E3D-C4579291692E")
IID_IMMDeviceEnumerator = GUID("A95664D2-9614-4F35-A746-DE8DB63617E6")
IID_IAudioSessionManager2 = GUID("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F")
IID_IAudioSessionControl2 = GUID("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D")
IID_ISimpleAudioVolume = GUID("87CE5498-68D6-44E5-9215-6DA47EF883D8")


def _call(obj, index, *args, argtypes=()):
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    fn = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, *argtypes)(vtbl[index])
    return fn(obj, *args)


def _release(obj):
    if obj:
        _call(obj, 2)


def _out(index, obj, *args, argtypes=()):
    """call a method whose last parameter is an output interface pointer"""
    p = ctypes.c_void_p()
    hr = _call(obj, index, *args, ctypes.byref(p), argtypes=(*argtypes, ctypes.POINTER(ctypes.c_void_p)))
    return p if hr >= 0 and p.value else None


def _qi(obj, iid):
    return _out(0, obj, ctypes.byref(iid), argtypes=(ctypes.c_void_p,))


_com_ready = False


def _init():
    global _com_ready
    if not _com_ready:
        ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED (ok if already initialised)
        _com_ready = True


def _volumes_of(pid):
    """ISimpleAudioVolume pointers for every audio session of process `pid` (any output device).
    The caller must _release() each of them."""
    _init()
    found = []
    enum = ctypes.c_void_p()
    if ole32.CoCreateInstance(ctypes.byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL,
                              ctypes.byref(IID_IMMDeviceEnumerator), ctypes.byref(enum)) < 0:
        return found
    try:
        devices = _out(3, enum, 0, 1, argtypes=(ctypes.c_int, wt.DWORD))  # eRender, DEVICE_STATE_ACTIVE
        if not devices:
            return found
        count = wt.UINT()
        _call(devices, 3, ctypes.byref(count), argtypes=(ctypes.POINTER(wt.UINT),))
        for i in range(count.value):
            dev = _out(4, devices, i, argtypes=(wt.UINT,))
            if not dev:
                continue
            mgr = _out(3, dev, ctypes.byref(IID_IAudioSessionManager2), CLSCTX_ALL, None,
                       argtypes=(ctypes.c_void_p, wt.DWORD, ctypes.c_void_p))
            sessions = _out(5, mgr) if mgr else None
            if sessions:
                n = ctypes.c_int()
                _call(sessions, 3, ctypes.byref(n), argtypes=(ctypes.POINTER(ctypes.c_int),))
                for j in range(n.value):
                    ctl = _out(4, sessions, j, argtypes=(ctypes.c_int,))
                    ctl2 = _qi(ctl, IID_IAudioSessionControl2) if ctl else None
                    if ctl2:
                        spid = wt.DWORD()
                        _call(ctl2, 14, ctypes.byref(spid), argtypes=(ctypes.POINTER(wt.DWORD),))
                        if spid.value == pid:
                            vol = _qi(ctl2, IID_ISimpleAudioVolume)
                            if vol:
                                found.append(vol)
                    _release(ctl2)
                    _release(ctl)
            _release(sessions)
            _release(mgr)
            _release(dev)
        _release(devices)
    finally:
        _release(enum)
    return found


def set_volume(pid, level, mute=False):
    """set volume (0.0-1.0) and mute of process `pid`. Returns True if a session was found."""
    found = False
    level = min(max(float(level), 0.0), 1.0)
    for vol in _volumes_of(pid):
        _call(vol, 3, ctypes.c_float(level), None, argtypes=(ctypes.c_float, ctypes.c_void_p))
        _call(vol, 5, bool(mute), None, argtypes=(wt.BOOL, ctypes.c_void_p))
        _release(vol)
        found = True
    return found


def get_volume(pid):
    """(level, mute) of the first session of `pid`, or None"""
    result = None
    for vol in _volumes_of(pid):
        if result is None:
            lv, mu = ctypes.c_float(), wt.BOOL()
            _call(vol, 4, ctypes.byref(lv), argtypes=(ctypes.POINTER(ctypes.c_float),))
            _call(vol, 6, ctypes.byref(mu), argtypes=(ctypes.POINTER(wt.BOOL),))
            result = (round(lv.value, 3), bool(mu.value))
        _release(vol)
    return result
