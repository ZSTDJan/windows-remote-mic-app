"""First frozen runtime hook; activate diagnostics only for device-list children."""
import sys

if len(sys.argv) >= 4 and sys.argv[1] == "--list-remote-devices":
    import remote_scan_bootstrap
    remote_scan_bootstrap.activate()
