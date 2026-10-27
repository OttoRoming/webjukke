from __future__ import annotations

import asyncio
import json
import os
import shlex
from typing import Any, Literal, Sequence, TypedDict, cast, overload

#: Executable to invoke.  Override with the ``RMPC_BIN`` environment variable.
RMPC_BIN = os.environ.get("RMPC_BIN", "rmpc")

_DEFAULT_TIMEOUT = 30.0
# ``rmpc listall`` can walk a very large music database.
_LISTALL_TIMEOUT = 300.0


class RmpcError(RuntimeError):
    """Raised when ``rmpc`` cannot be executed or returns an error."""

    def __init__(
        self,
        command: Sequence[str],
        returncode: int | None,
        stdout: str,
        stderr: str,
    ) -> None:
        self.command = tuple(command)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

        detail = stderr.strip() or stdout.strip()
        if not detail:
            detail = "rmpc produced no output" if returncode is not None else "rmpc failed"
        if returncode is not None:
            detail = f"{detail} (exit status {returncode})"

        super().__init__(f"{shlex.join(self.command)}: {detail}")


class Duration(TypedDict):
    """A duration in seconds and nanoseconds."""

    secs: int
    nanos: int


MetadataValue = str | list[str]
"""A metadata value, either single or multi-valued."""

OnOffOneshot = Literal["On", "Off", "Oneshot"]
"""Possible values for the ``single`` and ``consume`` status fields."""


class Song(TypedDict):
    """A song in the queue or music database."""

    id: int
    file: str
    duration: Duration | None
    metadata: dict[str, MetadataValue]
    last_modified: str
    added: str | None


class Status(TypedDict):
    """The current player status."""

    partition: str
    volume: int
    repeat: bool
    random: bool
    single: OnOffOneshot
    consume: OnOffOneshot
    playlist: int | None
    playlistlength: int
    state: Literal["Play", "Pause", "Stop"]
    song: int | None
    songid: int | None
    nextsong: int | None
    nextsongid: int | None
    elapsed: Duration
    duration: Duration
    bitrate: int | None
    xfade: int | None
    mixrampdb: str | None
    mixrampdelay: str | None
    audio: str | None
    updating_db: int | None
    error: str | None
    lastloadedplaylist: str | None


class Output(TypedDict):
    """An MPD output."""

    id: int
    name: str
    enabled: bool
    plugin: str


class Decoder(TypedDict):
    """An MPD decoder plugin."""

    name: str
    suffixes: list[str]
    mime_types: list[str]


class Mount(TypedDict):
    """A mounted storage."""

    mount: str
    storage: str


class LsDirectory(TypedDict):
    """A directory entry from ``lsinfo``."""

    path: str
    last_modified: str


class LsPlaylist(TypedDict):
    """A playlist entry from ``lsinfo``."""

    name: str
    last_modified: str


class LsFile(TypedDict):
    """A file entry from ``lsinfo``."""

    path: str
    last_modified: str
    added: str | None
    format: str | None
    duration: float | None
    metadata: dict[str, MetadataValue]


class LsInfo(TypedDict):
    """The direct contents of a directory."""

    directories: list[LsDirectory]
    files: list[LsFile]
    playlists: list[LsPlaylist]


def _as_text(value: bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace")


def _mpd_quote(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError("paths cannot contain newlines")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _add_metadata(
    metadata: dict[str, MetadataValue],
    key: str,
    value: str,
) -> None:
    existing = metadata.get(key)
    if existing is None:
        metadata[key] = value
    elif isinstance(existing, str):
        metadata[key] = [existing, value]
    else:
        existing.append(value)


def _parse_lsinfo(output: str) -> LsInfo:
    directories: list[LsDirectory] = []
    files: list[LsFile] = []
    playlists: list[LsPlaylist] = []

    current: dict[str, Any] | None = None
    kind: Literal["directory", "file", "playlist"] | None = None
    fallback_time: int | None = None

    def finish() -> None:
        nonlocal current, kind, fallback_time
        if current is None:
            return
        if kind == "file":
            if current["duration"] is None and fallback_time is not None:
                current["duration"] = float(fallback_time)
            files.append(cast(LsFile, current))
        elif kind == "directory":
            directories.append(cast(LsDirectory, current))
        elif kind == "playlist":
            playlists.append(cast(LsPlaylist, current))
        current = None
        kind = None
        fallback_time = None

    for line in output.splitlines():
        if not line or line == "OK" or line.startswith("ACK"):
            continue

        key, separator, value = line.partition(": ")
        if not separator:
            continue

        if key == "file":
            finish()
            current = {
                "path": value,
                "last_modified": "",
                "added": None,
                "format": None,
                "duration": None,
                "metadata": {},
            }
            kind = "file"
        elif key == "directory":
            finish()
            current = {"path": value, "last_modified": ""}
            kind = "directory"
        elif key == "playlist":
            finish()
            current = {"name": value, "last_modified": ""}
            kind = "playlist"
        elif current is None:
            continue
        elif key == "Last-Modified":
            current["last_modified"] = value
        elif kind == "file":
            if key == "Added":
                current["added"] = value
            elif key == "Format":
                current["format"] = value
            elif key == "duration":
                try:
                    current["duration"] = float(value)
                except ValueError:
                    pass
            elif key == "Time":
                try:
                    fallback_time = int(value)
                except ValueError:
                    pass
            else:
                _add_metadata(current["metadata"], key, value)

    finish()
    return {
        "directories": directories,
        "files": files,
        "playlists": playlists,
    }


class Rmpc:
    """Async client for the ``rmpc`` command line program."""

    def __init__(self, address: str | None = None) -> None:
        self.address = address

    def _build_command(self, *args: str) -> list[str]:
        command = [RMPC_BIN]
        if self.address is not None:
            # Must come before the subcommand, otherwise clap rejects it.
            command.extend(("-a", self.address))
        command.extend(args)
        return command

    async def _execute(
        self,
        *args: str,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> tuple[list[str], int | None, bytes, bytes]:
        """Run rmpc and return its result."""
        command = self._build_command(*args)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RmpcError(
                command,
                None,
                "",
                f"executable {RMPC_BIN!r} not found",
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            await self._stop(process)
            raise RmpcError(
                command,
                None,
                "",
                f"timed out after {timeout:g}s",
            ) from exc
        except asyncio.CancelledError:
            await self._stop(process)
            raise

        return command, process.returncode, stdout, stderr

    @staticmethod
    async def _stop(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()

    async def _run(
        self,
        *args: str,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> str:
        command, returncode, stdout, stderr = await self._execute(
            *args,
            timeout=timeout,
        )
        out = _as_text(stdout)
        err = _as_text(stderr)

        if returncode != 0:
            raise RmpcError(command, returncode, out, err)

        return out

    async def _run_json(
        self,
        *args: str,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> Any:
        output = await self._run(*args, timeout=timeout)
        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            raise RmpcError(
                self._build_command(*args),
                0,
                output,
                f"invalid JSON: {exc}",
            ) from exc

    async def status(self) -> Status:
        """Return the current MPD status."""
        return cast(Status, await self._run_json("status"))

    async def albumart(self) -> bytes | None:
        """Return the current album art as bytes, or ``None`` if unavailable."""
        command, returncode, stdout, stderr = await self._execute(
            "albumart",
            "--output",
            "-",
        )

        if returncode in (2, 3):
            return None
        if returncode != 0:
            raise RmpcError(
                command,
                returncode,
                _as_text(stdout),
                _as_text(stderr),
            )

        return stdout

    async def pause(self) -> None:
        """Pause playback."""
        await self._run("pause")

    async def unpause(self) -> None:
        """Resume playback."""
        await self._run("unpause")

    async def play(self) -> None:
        """Plays song at the position in the current playlist. Defaults to current paused songa."""
        await self._run("play")

    async def next(self, *, keep_state: bool = False) -> None:
        """Skip to the next song."""
        args = ["next"]
        if keep_state:
            args.append("--keep-state")
        await self._run(*args)

    async def prev(
        self,
        *,
        rewind_to_start: int | None = None,
        keep_state: bool = False,
    ) -> None:
        """Go back to the previous song."""
        args = ["prev"]
        if rewind_to_start is not None:
            args.extend(("--rewind-to-start", str(rewind_to_start)))
        if keep_state:
            args.append("--keep-state")
        await self._run(*args)

    async def add(
        self,
        files: Sequence[str],
        *,
        position: str | int | None = None,
        skip_ext_check: bool = False,
    ) -> None:
        """Add files to the queue."""
        args = ["add"]
        if skip_ext_check:
            args.append("--skip-ext-check")
        if position is not None:
            args.extend(("--position", str(position)))
        args.extend(files)
        await self._run(*args)

    async def clear(self) -> None:
        """Clear the queue."""
        await self._run("clear")

    async def queue(self) -> list[Song]:
        """Return the current queue."""
        try:
            return cast(list[Song], await self._run_json("queue"))
        except RmpcError as exc:
            if (
                exc.returncode == 1
                and not exc.stdout.strip()
                and not exc.stderr.strip()
            ):
                return []
            raise

    @overload
    async def song(self) -> Song | None: ...

    @overload
    async def song(self, path: str) -> Song | None: ...

    @overload
    async def song(self, *, paths: Sequence[str]) -> list[Song] | None: ...

    async def song(
        self,
        path: str | None = None,
        *,
        paths: Sequence[str] | None = None,
    ) -> Song | list[Song] | None:
        """Return the current song or information about the given path(s)."""
        if path is not None and paths is not None:
            raise ValueError("pass either 'path' or 'paths', not both")

        try:
            if paths is not None:
                if not paths:
                    return []
                args = ["song"]
                for item in paths:
                    args.extend(("-p", item))
                result = await self._run_json(*args)
                if result is None:
                    return None
                if isinstance(result, list):
                    return cast(list[Song], result)
                # rmpc returns a single object when only one --path was supplied.
                return [cast(Song, result)]

            args = ["song"]
            if path is not None:
                args.extend(("-p", path))
            return cast(Song | None, await self._run_json(*args))
        except RmpcError as exc:
            if exc.returncode == 1:
                return None
            raise

    async def lsinfo(self, path: str = "") -> LsInfo:
        """List the direct contents of a directory."""
        path = path.rstrip("/")
        command = "lsinfo" if not path else f"lsinfo {_mpd_quote(path)}"
        output = await self._run("raw", command)

        if output.startswith("ACK"):
            raise RmpcError(
                self._build_command("raw", command),
                None,
                output,
                "",
            )

        return _parse_lsinfo(output)

    async def listall(self, files: Sequence[str] = ()) -> list[str]:
        """List files in MPD's music database."""
        output = await self._run(
            "listall",
            *files,
            timeout=_LISTALL_TIMEOUT,
        )
        return [line for line in output.splitlines() if line]

    async def outputs(self) -> list[Output]:
        """Return the available MPD outputs."""
        return cast(list[Output], await self._run_json("outputs"))

    async def decoders(self) -> list[Decoder]:
        """Return the available MPD decoder plugins."""
        return cast(list[Decoder], await self._run_json("decoders"))

    async def listmounts(self) -> list[Mount]:
        """Return the currently mounted storages."""
        return cast(list[Mount], await self._run_json("listmounts"))

    async def listpartitions(self) -> list[str]:
        """Return the existing MPD partition names."""
        return cast(list[str], await self._run_json("listpartitions"))

    async def volume(self) -> int:
        """Return the current MPD volume."""
        output = await self._run("volume")
        try:
            return int(output.strip())
        except ValueError as exc:
            raise RmpcError(
                self._build_command("volume"),
                0,
                output,
                f"invalid volume output: {output!r}",
            ) from exc


if __name__ == "__main__":
    # Minimal demo: ``python rmpc.py [ADDRESS]`` prints the status as JSON.
    import sys

    _client = Rmpc(sys.argv[1] if len(sys.argv) > 1 else None)
    print(json.dumps(asyncio.run(_client.status()), indent=2, ensure_ascii=False))
