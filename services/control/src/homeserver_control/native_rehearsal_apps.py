"""Native loopback APIs for freshly created, network-isolated synthetic fixtures."""

import json
import os
import secrets
import time
import xml.etree.ElementTree as ET

from .native_containment import ContainmentError

PORTS = {"jellyfin": 8096, "sonarr": 8989, "radarr": 7878}
MOVIE_PATH = "/data/media/movies/Probe (2026)/Probe (2026).mp4"


def _public_field(body, name):
    """Jellyfin 12 startup uses camelCase; completed startup uses PascalCase."""
    alias = name[0].lower() + name[1:]
    if name in body and alias in body and body[name] != body[alias]:
        raise ContainmentError("native_schema_ambiguous")
    return body.get(name, body.get(alias))


class NativeApps:
    def __init__(self, group, configs, *, evidence=None):
        self.group = group
        self.configs = configs
        self.headers = {}
        self.evidence = evidence

    def _record(self, role, endpoint, method, status, payload):
        if self.evidence:
            descriptor = os.open(
                self.evidence / "native-http.private.jsonl",
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
            with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "role": role,
                            "endpoint": endpoint,
                            "method": method,
                            "status": status,
                            "payload": payload,
                        }
                    )
                    + "\n"
                )
                stream.flush()
                os.fsync(stream.fileno())

    def execute(self, role, *arguments, stdin=None):
        snapshots = self.group.snapshot()
        owned = next(item for item in reversed(self.group.owned) if item.role == role)
        snapshot = next(s for s in snapshots if s["Id"] == owned.identifier)
        if not snapshot["State"]["Running"]:
            raise ContainmentError("native_not_running")
        return self.group.docker.command("exec", "-i", owned.identifier, *arguments, stdin=stdin)

    def request(self, role, endpoint, method="GET", body=None):
        lines = [
            "silent",
            "show-error",
            "max-time = 15",
            "url = " + json.dumps(f"http://127.0.0.1:{PORTS[role]}{endpoint}"),
            "request = " + json.dumps(method),
            'write-out = "\\n__HTTP_STATUS__%{http_code}"',
        ]
        if role in self.headers:
            lines.append("header = " + json.dumps(self.headers[role]))
        if body is not None:
            lines += [
                'header = "Content-Type: application/json"',
                "data = " + json.dumps(json.dumps(body)),
            ]
        output = self.execute(role, "/usr/bin/curl", "--config", "-", stdin="\n".join(lines) + "\n")
        try:
            payload, status = output.rsplit("\n__HTTP_STATUS__", 1)
            self._record(role, endpoint, method, status, payload)
            if int(status) not in (200, 201, 204):
                raise ValueError
            return json.loads(payload) if payload else None
        except (ValueError, TypeError):
            raise ContainmentError("native_http_failed") from None

    def wait(self, role, endpoint, *, required_key=None):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                result = self.request(role, endpoint)
                value = (
                    result.get(required_key)
                    if required_key
                    else _public_field(result, "Version")
                    if role == "jellyfin"
                    else result.get("version")
                )
                if isinstance(value, str) and (required_key or value):
                    return result
            except ContainmentError:
                pass
            time.sleep(1)
        raise ContainmentError("native_readiness_failed")

    def arr_key(self, role):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                key = ET.parse(self.configs[role] / "config.xml").findtext("ApiKey")
                if key:
                    self.headers[role] = f"X-Api-Key: {key}"
                    return
            except (OSError, ET.ParseError):
                pass
            time.sleep(1)
        raise ContainmentError("native_readiness_failed")

    def authenticate(self, username, password):
        self.headers["jellyfin"] = (
            'Authorization: MediaBrowser Client="NativeRehearsal", Device="Fixture", '
            'DeviceId="isolated-native-rehearsal", Version="1"'
        )
        data = self.request(
            "jellyfin", "/Users/AuthenticateByName", "POST", {"Username": username, "Pw": password}
        )
        self.headers["jellyfin"] += f', Token="{data["AccessToken"]}"'
        return data["User"]["Id"]

    def prepare_library(self):
        options = {
            "PathInfos": [{"Path": "/data/media/movies"}],
            "EnableRealtimeMonitor": False,
            "EnableAutomaticSeriesGrouping": False,
            "MetadataSavers": [],
            "DisabledLocalMetadataReaders": [],
            "TypeOptions": [
                {"Type": kind, "MetadataFetchers": [], "ImageFetchers": []}
                for kind in ("Movie", "BoxSet")
            ],
        }
        self.request(
            "jellyfin",
            "/Library/VirtualFolders?name=NativeProbe&collectionType=movies&refreshLibrary=false",
            "POST",
            {"LibraryOptions": options},
        )
        folders = self.request("jellyfin", "/Library/VirtualFolders")
        matching = [folder for folder in folders if folder.get("Name") == "NativeProbe"]
        if len(matching) != 1 or matching[0].get("Locations") != ["/data/media/movies"]:
            raise ContainmentError("native_library_options_failed")
        actual = matching[0].get("LibraryOptions", {})
        if actual.get("EnableRealtimeMonitor") is not False:
            raise ContainmentError("native_library_options_failed")
        for expected in options["TypeOptions"]:
            matches = [
                entry
                for entry in actual.get("TypeOptions", [])
                if entry.get("Type") == expected["Type"]
            ]
            if len(matches) != 1 or any(
                matches[0].get(key) != value for key, value in expected.items()
            ):
                raise ContainmentError("native_library_options_failed")
        # Startup scans may already be running when a library is created. Avoid
        # coalescing the only fixture scan with that earlier empty-library job.
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            scans = [
                task
                for task in self.request("jellyfin", "/ScheduledTasks")
                if task.get("Key") == "RefreshLibrary"
            ]
            if len(scans) != 1:
                raise ContainmentError("native_scan_readiness_failed")
            if scans[0].get("State") == "Idle":
                self.request("jellyfin", "/Library/Refresh", "POST")
                return
            time.sleep(1)
        raise ContainmentError("native_scan_readiness_failed")

    def baseline(self):
        versions = {}
        state = {"username": "nativefixture", "password": secrets.token_urlsafe(24)}
        public = self.wait("jellyfin", "/System/Info/Public")
        versions["jellyfin"] = _public_field(public, "Version")
        self.wait("jellyfin", "/Startup/User", required_key="Name")
        self.request(
            "jellyfin",
            "/Startup/User",
            "POST",
            {"Name": state["username"], "Password": state["password"]},
        )
        state["user"] = self.authenticate(state["username"], state["password"])
        state["authorization"] = self.headers["jellyfin"]
        self.request("jellyfin", "/Startup/Complete", "POST")
        if (
            _public_field(self.request("jellyfin", "/System/Info/Public"), "StartupWizardCompleted")
            is not True
        ):
            raise ContainmentError("native_baseline_failed")
        self.execute(
            "jellyfin",
            "/usr/lib/jellyfin-ffmpeg/ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-n",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=96x64:r=5:d=2",
            "-an",
            "-c:v",
            "mpeg4",
            "-q:v",
            "5",
            MOVIE_PATH,
        )
        self.prepare_library()
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            items = self.request(
                "jellyfin",
                "/Items?Recursive=true&IncludeItemTypes=Movie&Fields=Path,MediaSources,ProviderIds",
            )["Items"]
            matches = [item for item in items if item.get("Path") == MOVIE_PATH]
            if len(matches) == 1:
                state["item"] = matches[0]["Id"]
                self.verify_movie(state)
                break
            time.sleep(1)
        else:
            raise ContainmentError("native_catalog_failed")
        for role in ("sonarr", "radarr"):
            self.arr_key(role)
            versions[role] = self.wait(role, "/api/v3/system/status")["version"]
            tag = self.request(
                role, "/api/v3/tag", "POST", {"label": "native-probe-" + secrets.token_hex(8)}
            )
            state[role] = {"id": tag["id"], "label": tag["label"]}
            self.verify_tag(role, state[role])
        return versions, state

    def verify_movie(self, state):
        item = self.request("jellyfin", f"/Users/{state['user']}/Items/{state['item']}")
        sources = item.get("MediaSources", [])
        if (
            item.get("Id") != state["item"]
            or item.get("Path") != MOVIE_PATH
            or len(sources) != 1
            or sources[0].get("Size", 0) <= 0
            or item.get("RunTimeTicks", 0) <= 0
        ):
            raise ContainmentError("native_catalog_failed")

    def verify_tag(self, role, expected):
        actual = self.request(role, f"/api/v3/tag/{expected['id']}")
        if any(actual.get(key) != value for key, value in expected.items()):
            raise ContainmentError("native_object_changed")

    def verify_jellyfin(self, versions, state):
        public = self.wait("jellyfin", "/System/Info/Public")
        if _public_field(public, "Version") != versions["jellyfin"]:
            raise ContainmentError("native_version_changed")
        self.headers["jellyfin"] = state["authorization"]
        self.wait("jellyfin", "/System/Info")
        if self.authenticate(state["username"], state["password"]) != state["user"]:
            raise ContainmentError("native_object_changed")
        self.verify_movie(state)

    def verify_restored(self, versions, state):
        self.verify_jellyfin(versions, state)
        for role in ("sonarr", "radarr"):
            self.arr_key(role)
            if self.wait(role, "/api/v3/system/status")["version"] != versions[role]:
                raise ContainmentError("native_version_changed")
            self.verify_tag(role, state[role])
