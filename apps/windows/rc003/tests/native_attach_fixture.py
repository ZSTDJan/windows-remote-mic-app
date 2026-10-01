"""Isolated native regression fixture. Own child and fresh test TEMP only."""
import ctypes as C,os,time,pathlib,tempfile,subprocess,json,sys
from ovb_rc003 import chromecast_hid_tap_windows as tap
from ovb_rc003.chromecast_observation import Observation
from ovb_rc003.chromecast_channel import Channel, SessionIdentity
from ovb_rc003 import log_export
from ovb_rc003.chromecast_attach_diagnostics_windows import AttachEvidence
rows=[]
identity=SessionIdentity.create('a'*64)
sender,receiver=Channel(identity,commands=False),Channel(identity,commands=False)
observation=Observation(lambda row: rows.append(receiver.decode(sender.encode('evidence',record=row))['record']))
def attach(pid):
 instance=tap.HidTap('a'*64);instance.pid=pid
 try:return instance._attach(frida,pid)
 finally:
  for row in instance.poll_startup():observation.hid_startup(row)

from ctypes import wintypes as W
A=C.WinDLL('advapi32',use_last_error=True);K=C.WinDLL('kernel32',use_last_error=True)
class SA(C.Structure):_fields_=[('Sid',W.LPVOID),('Attributes',W.DWORD)]
class TG(C.Structure):_fields_=[('count',W.DWORD),('groups',SA*1)]
class SI(C.Structure):_fields_=[('cb',W.DWORD),('lpReserved',W.LPWSTR),('lpDesktop',W.LPWSTR),('lpTitle',W.LPWSTR),('dwX',W.DWORD),('dwY',W.DWORD),('dwXSize',W.DWORD),('dwYSize',W.DWORD),('dwXCountChars',W.DWORD),('dwYCountChars',W.DWORD),('dwFillAttribute',W.DWORD),('dwFlags',W.DWORD),('wShowWindow',W.WORD),('cbReserved2',W.WORD),('lpReserved2',W.LPVOID),('hStdInput',W.HANDLE),('hStdOutput',W.HANDLE),('hStdError',W.HANDLE)]
class PI(C.Structure):_fields_=[('hProcess',W.HANDLE),('hThread',W.HANDLE),('dwProcessId',W.DWORD),('dwThreadId',W.DWORD)]
def fn(d,n,args,result):f=getattr(d,n);f.argtypes=args;f.restype=result;return f
fn(K,'GetCurrentProcess',[],W.HANDLE);fn(K,'CloseHandle',[W.HANDLE],W.BOOL);fn(K,'LocalFree',[W.LPVOID],W.LPVOID)
fn(K,'TerminateProcess',[W.HANDLE,W.UINT],W.BOOL);fn(K,'WaitForSingleObject',[W.HANDLE,W.DWORD],W.DWORD)
fn(K,'GetExitCodeProcess',[W.HANDLE,C.POINTER(W.DWORD)],W.BOOL)
fn(A,'OpenProcessToken',[W.HANDLE,W.DWORD,C.POINTER(W.HANDLE)],W.BOOL)
fn(A,'ConvertStringSidToSidW',[W.LPCWSTR,C.POINTER(W.LPVOID)],W.BOOL)
fn(A,'GetTokenInformation',[W.HANDLE,C.c_int,W.LPVOID,W.DWORD,C.POINTER(W.DWORD)],W.BOOL)
fn(A,'CreateRestrictedToken',[W.HANDLE,W.DWORD,W.DWORD,W.LPVOID,W.DWORD,W.LPVOID,W.DWORD,C.POINTER(SA),C.POINTER(W.HANDLE)],W.BOOL)
fn(A,'CreateProcessAsUserW',[W.HANDLE,W.LPCWSTR,W.LPWSTR,W.LPVOID,W.LPVOID,W.BOOL,W.DWORD,W.LPVOID,W.LPCWSTR,C.POINTER(SI),C.POINTER(PI)],W.BOOL)
def check(ok):
 if not ok:raise C.WinError(C.get_last_error())
token=W.HANDLE();restricted=W.HANDLE();sids=[];pi=PI();session=None
try:
 check(A.OpenProcessToken(K.GetCurrentProcess(),0xf01ff,C.byref(token)))
 for text in ['S-1-1-0','S-1-5-11','S-1-5-32-545','S-1-5-4','S-1-5-19']:
  sid=W.LPVOID();check(A.ConvertStringSidToSidW(text,C.byref(sid)));sids.append(sid)
 needed=W.DWORD();A.GetTokenInformation(token,2,None,0,C.byref(needed));buf=C.create_string_buffer(needed.value)
 check(A.GetTokenInformation(token,2,buf,len(buf),C.byref(needed)))
 header=C.cast(buf,C.POINTER(TG)).contents
 groups=C.cast(C.addressof(buf)+TG.groups.offset,C.POINTER(SA))
 restricting=[SA(sid,0) for sid in sids]+[SA(groups[i].Sid,0) for i in range(header.count) if groups[i].Attributes&0xc0000000==0xc0000000]
 arr=(SA*len(restricting))(*restricting)
 check(A.CreateRestrictedToken(token,0,0,None,0,None,len(arr),arr,C.byref(restricted)))
 from ovb_rc003.hid_elevation_windows import current_user_sid
 # Python's TemporaryDirectory uses OWNER RIGHTS, unlike the user's ordinary
 # TEMP. Give only this test root an explicit owner ACE for the restricted-token
 # counterfactual; the restricting SID pass must still fail until the LS grant.
 testroot=pathlib.Path(sys.argv[1]).resolve()
 assert testroot==pathlib.Path(tempfile.gettempdir()).resolve()
 result=subprocess.run([os.environ['SystemRoot']+r'\System32\icacls.exe',str(testroot),'/grant','*'+current_user_sid()+':(OI)(CI)(F)','/Q'],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
 check(result.returncode==0)
 sd=W.LPVOID()
 fn(A,'ConvertStringSecurityDescriptorToSecurityDescriptorW',[W.LPCWSTR,W.DWORD,C.POINTER(W.LPVOID),C.POINTER(W.DWORD)],W.BOOL)
 fn(A,'GetSecurityDescriptorDacl',[W.LPVOID,C.POINTER(W.BOOL),C.POINTER(W.LPVOID),C.POINTER(W.BOOL)],W.BOOL)
 fn(A,'SetTokenInformation',[W.HANDLE,C.c_int,W.LPVOID,W.DWORD],W.BOOL)
 check(A.ConvertStringSecurityDescriptorToSecurityDescriptorW('D:(A;;GA;;;SY)(A;;GA;;;BU)(A;;GA;;;'+current_user_sid()+')',1,C.byref(sd),None))
 try:
  present=W.BOOL();defaulted=W.BOOL();acl=W.LPVOID()
  check(A.GetSecurityDescriptorDacl(sd,C.byref(present),C.byref(acl),C.byref(defaulted)))
  check(A.SetTokenInformation(restricted,6,C.byref(acl),C.sizeof(acl)))
 finally:K.LocalFree(sd)
 exe=os.environ['SystemRoot']+r'\System32\ping.exe'
 cmd=C.create_unicode_buffer('"'+exe+'" -n 30 127.0.0.1')
 si=SI();si.cb=C.sizeof(si);si.dwFlags=1;si.wShowWindow=0
 check(A.CreateProcessAsUserW(restricted,exe,cmd,None,None,False,0x08000000,None,os.environ['SystemRoot'],C.byref(si),C.byref(pi)))
 time.sleep(1);code=W.DWORD();check(K.GetExitCodeProcess(pi.hProcess,C.byref(code)))
 print('restricted-child','alive' if code.value==259 else hex(code.value))
 assert code.value==259, 'owned_child_did_not_start'
 if code.value==259:
  import frida
  root=pathlib.Path(tempfile.gettempdir());before=set(root.iterdir())
  try:session=attach(pi.dwProcessId);print('BEFORE_READ_GRANT','success')
  except Exception as e:print('BEFORE_READ_GRANT',type(e).__name__,str(e))
  check(K.GetExitCodeProcess(pi.hProcess,C.byref(code)));print('child-still-alive',code.value==259)
  if session is None and code.value==259:
   candidates=[p for p in set(root.iterdir())-before if p.is_dir() and (p/'x86_64'/'frida-agent.dll').is_file()]
   print('test-owned-frida-folders',len(candidates))
   for p in candidates:
    assert p.resolve().parent==root.resolve()
    assert root.resolve()==pathlib.Path(sys.argv[1]).resolve()
    for directory in (root,p):
     result=subprocess.run([os.environ['SystemRoot']+r'\System32\icacls.exe',str(directory),'/grant','*S-1-5-19:(OI)(CI)(RX)','/T','/Q'],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
     assert result.returncode==0
    print('test-asset-read-grant',result.returncode)
   assert len(candidates)==1
   agent=candidates[0]/'x86_64'/'frida-agent.dll'
   # Separate loader failure from the permission failure, only in our fresh
   # extracted test DLL. Restore its original header before the success case.
   with agent.open('r+b') as f:
    header=f.read(2);f.seek(0);f.write(b'NO')
   try:
    unexpected=None
    try:unexpected=attach(pi.dwProcessId)
    except frida.ProcessNotRespondingError:pass
    else:
     unexpected.detach()
     raise AssertionError('malformed_test_agent_loaded')
   finally:
    with agent.open('r+b') as f:f.write(header)
   try:session=attach(pi.dwProcessId);print('AFTER_READ_GRANT','success')
   except Exception as e:print('AFTER_READ_GRANT',type(e).__name__,str(e))
finally:
 try:
  if session:
   sc=None
   try:
    sc=session.create_script('rpc.exports={ping(){return "isolated-attach-ok"}}')
    sc.load()
    assert sc.exports_sync.ping()=='isolated-attach-ok'
   finally:
    try:
     if sc:sc.unload()
    finally:session.detach()
 finally:
  try:
   if pi.hProcess:
    native=AttachEvidence(pi.dwProcessId,lambda step,state,**kw: observation.hid_startup(tap.startup_record(step,state,host_pid=pi.dwProcessId,**kw)))
    try:
     native.begin();K.TerminateProcess(pi.hProcess,0);K.WaitForSingleObject(pi.hProcess,5000);native.finish()
    finally:
     native.close();K.TerminateProcess(pi.hProcess,0);K.WaitForSingleObject(pi.hProcess,5000);K.CloseHandle(pi.hProcess)
  finally:
   if pi.hThread:K.CloseHandle(pi.hThread)
   for h in [token,restricted]:
    if h:K.CloseHandle(h)
   for sid in sids:K.LocalFree(sid)

assert session is not None, 'owned_child_attach_not_recovered'
observation.flush(final=True)
logs=pathlib.Path(sys.argv[1])/'logs';logs.mkdir()
content='\n'.join('2026-09-28 23:00:00,000 INFO ovb_rc003: Chromecast evidence run=aaaaaaaaaaaa seq='+str(i+1)+' record='+json.dumps(row) for i,row in enumerate(rows))
(logs/'app.log').write_text(content,encoding='utf-8')
result=log_export.export_logs(pathlib.Path(sys.argv[1])/'native-evidence.zip',root=pathlib.Path(sys.argv[1]))
assert result.outcome=='exported'
print(json.dumps({'rows':rows,'exported':True}))
