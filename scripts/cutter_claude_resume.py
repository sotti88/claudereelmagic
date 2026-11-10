"""Keep legacy Claude resume-or-fresh behavior without shell interpolation.

argv: binary agent identity [extra...] — extra (z. B. --strict-mcp-config)
gilt fuer den Resume-Versuch UND den frischen Fallback (18.09.2026)."""
import signal
import subprocess
import sys


def main(argv):
    binary, agent, identity, *extra = argv
    child = None
    stopping = False
    def stop(signum, frame):
        nonlocal stopping
        stopping = True
        if child is not None:
            child.send_signal(signum)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGHUP, stop)
    child = subprocess.Popen([binary, '--agent', agent, '--resume', identity, *extra])
    code = child.wait()
    if code != 0 and not stopping:
        child = subprocess.Popen([binary, '--agent', agent, *extra])
        code = child.wait()
    return code


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
