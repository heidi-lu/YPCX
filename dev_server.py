"""Local development launcher: uvicorn reload, but ignore venv changes."""
import sys

from uvicorn.supervisors.watchfilesreload import FileFilter

_original_call = FileFilter.__call__


def _call(self, path):
    text = str(path)
    if "/venv/" in text or "/.venv/" in text or text.endswith("/venv") or text.endswith("/.venv"):
        return False
    return _original_call(self, path)


FileFilter.__call__ = _call


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit("usage: dev_server.py <app> <port>")
    import uvicorn

    uvicorn.run(sys.argv[1], host="127.0.0.1", port=int(sys.argv[2]), reload=True)
