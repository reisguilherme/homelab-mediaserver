"""Bounded concatenation of two compatible MP4 parts, without decoding streams."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import stat
import subprocess
import sys
from pathlib import Path

RECIPE = "concat_mp4_streamcopy_v1"
_FIELDS = (
    "codec_type",
    "codec_name",
    "codec_tag_string",
    "profile",
    "level",
    "time_base",
    "width",
    "height",
    "pix_fmt",
    "sample_aspect_ratio",
    "r_frame_rate",
    "sample_fmt",
    "sample_rate",
    "channels",
    "channel_layout",
    "extradata_hash",
)


def file_identity(path: Path) -> dict:
    """Record content and the physical inode; reject mutation during hashing."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
            raise ValueError("derivation source is not a positive regular file")
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        after = os.fstat(stream.fileno())
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ValueError("derivation source changed during hashing")
    return {
        "device": before.st_dev,
        "inode": before.st_ino,
        "size_bytes": before.st_size,
        "mtime_ns": before.st_mtime_ns,
        "sha256": digest,
    }


def probe_streams(path: Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-show_data",
            "-show_data_hash",
            "sha256",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        timeout=60,
        check=False,
    )
    if result.returncode or len(result.stdout) > 4 * 1024 * 1024:
        raise ValueError("derivation media probe failed")
    try:
        data = json.loads(result.stdout)
        streams = data["streams"]
        duration = float(data["format"]["duration"])
        if (
            not isinstance(streams, list)
            or not streams
            or not math.isfinite(duration)
            or duration <= 0
            or not any(s.get("codec_type") == "video" for s in streams)
            or not any(s.get("codec_type") == "audio" for s in streams)
            or any(s.get("codec_type") not in ("video", "audio", "subtitle") for s in streams)
        ):
            raise ValueError
        return data
    except (ValueError, KeyError, TypeError):
        raise ValueError("derivation media streams are invalid") from None


def signature(stream: dict, *, input_part: bool = True) -> dict:
    fields = _FIELDS if input_part else tuple(f for f in _FIELDS if f != "r_frame_rate")
    # r_frame_rate may change after timestamp concatenation; codecs, timebases,
    # extradata, order, language metadata and dispositions must remain exact.
    values = {key: stream.get(key) for key in fields}
    if stream.get("codec_name") == "mov_text":
        # MP4 btrt stores measured bitrates, not subtitle rendering/decoder data.
        # Preserve every style/font byte while normalizing this optional box.
        raw = b"".join(
            bytes.fromhex(line.split(":", 1)[1].strip().split("  ", 1)[0].replace(" ", ""))
            for line in stream.get("extradata", "").splitlines()
            if ":" in line
        )
        # The fixed tx3g sample entry is 30 bytes, followed by sized boxes.
        normalized, remaining = raw[:30], raw[30:]
        while remaining:
            size = int.from_bytes(remaining[:4], "big")
            if size < 8 or size > len(remaining):
                raise ValueError("invalid MP4 subtitle decoder data")
            box, remaining = remaining[:size], remaining[size:]
            if box[4:8] != b"btrt" or size != 20:
                normalized += box
        raw = normalized
        values["extradata_hash"] = hashlib.sha256(raw).hexdigest()
    return {
        **values,
        "language": stream.get("tags", {}).get("language"),
        "disposition": stream.get("disposition", {}),
    }


def verify_output(parts: list[Path], output: Path, receipt: dict, max_bytes: int) -> None:
    """Recovery requires the exact original inputs and recorded output inode/hash."""
    if [file_identity(p) for p in parts] != receipt["inputs"]:
        raise ValueError("derivation input provenance changed")
    current = file_identity(output)
    if current != receipt["output"] or current["size_bytes"] > max_bytes:
        raise ValueError("derivation output provenance changed")
    if receipt.get("recipe") != RECIPE:
        raise ValueError("derivation recipe changed")


async def remux_parts(parts: list[Path], output: Path, *, max_bytes: int) -> dict:
    if (
        sys.platform != "linux"
        or len(parts) != 2
        or type(max_bytes) is not int
        or not 0 < max_bytes <= 2**63 - 1
        or output.suffix.lower() != ".mp4"
        or output.exists()
        or output.is_symlink()
    ):
        raise ValueError("exclusive bounded Linux MP4 derivation required")
    parts = [Path(p) for p in parts]
    if any(p.suffix.lower() != ".mp4" or p.is_symlink() for p in parts):
        raise ValueError("MP4 regular source parts required")
    inputs = await asyncio.to_thread(lambda: [file_identity(p) for p in parts])
    if len({(p["device"], p["inode"]) for p in inputs}) != 2:
        raise ValueError("two distinct source parts required")
    probes = await asyncio.to_thread(lambda: [probe_streams(p) for p in parts])
    if [signature(s) for s in probes[0]["streams"]] != [signature(s) for s in probes[1]["streams"]]:
        raise ValueError("incompatible MP4 source streams")
    aliases = [output.parent / f"part{i}.mp4" for i in (1, 2)]
    for source, alias in zip(parts, aliases, strict=True):
        if alias.exists() or alias.is_symlink():
            if (
                alias.is_symlink()
                or alias.stat().st_ino != source.stat().st_ino
                or alias.stat().st_dev != source.stat().st_dev
            ):
                raise ValueError("derivation source alias changed")
        else:
            os.link(source, alias)
    concat = output.parent / "inputs.ffconcat"
    content = b"ffconcat version 1.0\nfile part1.mp4\nfile part2.mp4\n"
    if concat.exists():
        if concat.is_symlink() or concat.read_bytes() != content:
            raise ValueError("derivation concat recipe changed")
    else:
        with concat.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    # Set the hard limit inside a dedicated child before exec, avoiding preexec_fn
    # in this threaded/async worker. -fs can truncate successfully and is unsuitable.
    limit = (
        "import os,resource,sys; n=int(sys.argv[1]); "
        "resource.setrlimit(resource.RLIMIT_FSIZE,(n,n)); "
        "os.execvp(sys.argv[2],sys.argv[2:])"
    )
    command = [
        sys.executable,
        "-c",
        limit,
        str(max_bytes),
        "ffmpeg",
        "-nostdin",
        "-n",
        "-v",
        "error",
        "-f",
        "concat",
        "-safe",
        "1",
        "-protocol_whitelist",
        "file",
        "-i",
        str(concat),
        "-map",
        "0",
        "-c",
        "copy",
        "-map_metadata",
        "0",
        "-f",
        "mp4",
        str(output),
    ]
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(1800):
            code = await process.wait()
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if code:
        raise ValueError("bounded stream-copy derivation failed")
    output.chmod(0o644)
    with output.open("rb") as stream:
        os.fsync(stream.fileno())
    derived = await asyncio.to_thread(probe_streams, output)
    expected_duration = sum(float(p["format"]["duration"]) for p in probes)
    if abs(float(derived["format"]["duration"]) - expected_duration) > 0.5 or [
        signature(s, input_part=False) for s in derived["streams"]
    ] != [signature(s, input_part=False) for s in probes[0]["streams"]]:
        raise ValueError("derivation stream preservation failed")
    version = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=10, check=True)
    receipt = {
        "recipe": RECIPE,
        "ffmpeg_version": version.stdout.decode("utf-8", errors="replace").splitlines()[0],
        "inputs": inputs,
        "output": await asyncio.to_thread(file_identity, output),
        "duration_seconds": float(derived["format"]["duration"]),
        "stream_count": len(derived["streams"]),
    }
    await asyncio.to_thread(verify_output, parts, output, receipt, max_bytes)
    return receipt
