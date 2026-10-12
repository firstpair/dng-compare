"""Platform attribute access; the manifest retains base64 values on every platform."""
import ctypes, os, sys

def install():
    if hasattr(os,"listxattr"): return
    if sys.platform != "darwin": raise RuntimeError("This scanning host needs extended-attribute support")
    libc=ctypes.CDLL("/usr/lib/libSystem.B.dylib",use_errno=True)
    libc.listxattr.argtypes=[ctypes.c_char_p,ctypes.c_void_p,ctypes.c_size_t,ctypes.c_int]
    libc.listxattr.restype=ctypes.c_ssize_t
    libc.getxattr.argtypes=[ctypes.c_char_p,ctypes.c_char_p,ctypes.c_void_p,ctypes.c_size_t,ctypes.c_uint32,ctypes.c_int]
    libc.getxattr.restype=ctypes.c_ssize_t
    libc.setxattr.argtypes=[ctypes.c_char_p,ctypes.c_char_p,ctypes.c_void_p,ctypes.c_size_t,ctypes.c_uint32,ctypes.c_int]
    libc.setxattr.restype=ctypes.c_int
    def check(rc,path):
        if rc<0:
            code=ctypes.get_errno();raise OSError(code,os.strerror(code),os.fspath(path))
        return rc
    def listxattr(path,follow_symlinks=True):
        p=os.fsencode(path);options=0 if follow_symlinks else 1
        n=check(libc.listxattr(p,None,0,options),path)
        if not n:return []
        buf=ctypes.create_string_buffer(n);n=check(libc.listxattr(p,buf,n,options),path)
        return [os.fsdecode(x) for x in buf.raw[:n].split(b"\0") if x]
    def getxattr(path,name,follow_symlinks=True):
        p=os.fsencode(path);name=os.fsencode(name);options=0 if follow_symlinks else 1
        n=check(libc.getxattr(p,name,None,0,0,options),path)
        if not n:return b""
        buf=ctypes.create_string_buffer(n);n=check(libc.getxattr(p,name,buf,n,0,options),path)
        return buf.raw[:n]
    def setxattr(path,name,value,follow_symlinks=True):
        buf=ctypes.create_string_buffer(value);options=0 if follow_symlinks else 1
        check(libc.setxattr(os.fsencode(path),os.fsencode(name),buf,len(value),0,options),path)
    os.listxattr=listxattr;os.getxattr=getxattr;os.setxattr=setxattr
