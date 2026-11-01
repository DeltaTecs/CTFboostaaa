"""Check Docker-to-host HTTP routing using Linux host networking and a cached image.

No image is pulled. No VPN peer or remote callback is contacted. A successful
health request proves HTTP routing only, not an externally reachable callback.
"""
import argparse
import json
import shutil
import subprocess

from .http import local_url


def check(image, endpoint="http://127.0.0.1:8888", sudo=False):
    endpoint = local_url(endpoint)
    if not shutil.which("docker"):
        return {"status": "unverified", "reason": "Docker is not installed",
                "callback_routing": "unverified"}
    if not image or image.startswith("-"):
        raise ValueError("Supply a locally cached image containing Python 3")
    docker = ["sudo", "-n", "docker"] if sudo else ["docker"]
    inspect = subprocess.run([*docker, "image", "inspect", "--format", "{{.Id}}", image],
                             capture_output=True, text=True, timeout=10)
    if inspect.returncode:
        return {"status": "unverified", "reason": "Docker unavailable or image not cached; no pull attempted",
                "callback_routing": "unverified"}
    image_id = inspect.stdout.strip()
    script = f"""
import json, urllib.request
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Assessment redirects are disabled')
opener = urllib.request.build_opener(urllib.request.ProxyHandler({{}}), NoRedirect())
with opener.open({(endpoint + '/health')!r}, timeout=10) as response:
    health = json.load(response)
if health['status'] != 'healthy':
    raise RuntimeError('HexStrike is unhealthy')
print(json.dumps({{'status':health['status'], 'version':health['version']}}))
"""
    # Execute the inspected content-addressed image ID, with no registry access.
    result = subprocess.run([*docker, "run", "--rm", "--pull=never", "--network=host",
                             "--read-only", "--cap-drop=ALL", "--user=65534:65534",
                             image_id, "python3", "-c", script],
                            capture_output=True, text=True, timeout=20)
    return {"status": "passed" if result.returncode == 0 else "failed",
            "network": "Linux host", "endpoint": endpoint, "image_id": image_id,
            "health": json.loads(result.stdout) if result.returncode == 0 else None,
            "callback_routing": "unverified; no remote callbacks are attempted"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="python:3.14-slim", help="Must already be cached locally")
    parser.add_argument("--url", default="http://127.0.0.1:8888", type=local_url)
    parser.add_argument("--sudo", action="store_true", help="Use sudo -n for the local Docker daemon")
    args = parser.parse_args()
    result = check(args.image, args.url, args.sudo)
    print(json.dumps(result, indent=2))
    if result["status"] == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
