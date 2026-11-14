"""Finalize an immutable operator manifest without selecting or admitting sources.

Each job uses its real whole-payload parent permit. Child records are import intent,
never gateway permits or synthetic episode reservations. Unknown POST outcomes stay
unknown until native readback proves the exact source inode was imported.
"""
from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath

from homeserver_control.domain import media_probe
from homeserver_control.domain.subtitle_content import valid_srt
from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.audited_imports import AuditedImportStore
from homeserver_control.persistence.torrent_artifacts import TorrentArtifactStore
from homeserver_control.storage_paths import source_logical_path

from .finalization import MovieFinalizer, _subtitle_has_content, _write_external_subtitle
from .subtitle_language import (
    has_embedded_english_subtitle,
    is_brazilian_portuguese_subtitle,
    is_english_subtitle,
)
from .subtitle_recovery import recover_subtitle
from .validation import ValidationError


class AuditedManifestImporter:
    def __init__(
        self, *, store: AuditedImportStore, manifest_loader: Callable[[], dict],
        permits: PermitRegistry, torrent_store: TorrentArtifactStore,
        helpers: Mapping[str, MovieFinalizer], gateway_url: str, arr_token: str,
        max_files_per_cycle: int = 4, capacity_provider=None,
    ) -> None:
        if not arr_token or not 1 <= max_files_per_cycle <= 4:
            raise ValueError('audited import credentials or cycle limit are invalid')
        self.store = store
        self.manifest_loader = manifest_loader
        self.permits = permits
        self.torrent_store = torrent_store
        self.helpers = helpers
        self.gateway_url = gateway_url.rstrip('/')
        self.gateway_headers = {'X-Arr-Token': arr_token}
        self.max_files_per_cycle = max_files_per_cycle
        self.capacity_provider = capacity_provider

    def prepare_manifest(self) -> dict:
        # Re-read and seal on every cycle. Missing/changed manifests fail closed.
        manifest = self.manifest_loader()
        self.store.install_manifest(manifest)
        return manifest

    async def run_once(self, *, manifest: dict | None = None) -> None:
        if manifest is None:
            self.prepare_manifest()
        else:
            # Hybrid cycles use exactly the snapshot that established their exclusion set.
            self.store.install_manifest(manifest)
        for row in self.store.pending_files(limit=self.max_files_per_cycle):
            try:
                await self._finalize(row)
            except Exception:
                # Neither HTTP bodies nor upstream exception strings are safe to persist.
                self.store.defer(row['file_id'], 'audited_validation_or_api_pending')

    def _permit(self, job):
        permit = self.permits.get_for_reservation(job['reservation_id'], scope_key=None)
        if (permit is None or permit.permit_id != job['permit_id']
                or permit.reservation_id != job['reservation_id'] or permit.scope_key is not None
                or permit.category != job['category']
                or permit.metadata_sha256 != job['metadata_sha256']
                or permit.budget_bytes != job['expected_payload_bytes']
                or permit.state != 'confirmed'
                or not self.permits.probe_reservation_active(permit)
                or not self.permits.placement_valid(permit, writable=True)):
            raise ValidationError('audited parent permit is not active')
        descriptor = self.torrent_store.get(permit)
        if descriptor is None:
            raise ValidationError('audited descriptor proof is missing')
        inspected = inspect_torrent(descriptor)
        content = {item.path: item.length for item in inspected.files}
        expected = {item['path']: item['size_bytes'] for item in job['payload_files']}
        padding = sum(item.length for item in getattr(inspected, 'padding_files', ()))
        if (content != expected or padding != job['padding_bytes']
                or inspected.total_bytes != job['expected_payload_bytes']
                or set(permit.selected_files) != set(expected)):
            raise ValidationError('audited descriptor payload changed')
        return permit

    async def _download(self, helper, permit, job):
        response = await helper.client.get(
            f'{self.gateway_url}/api/v2/torrents/info', params={'hashes': permit.infohash},
            headers=self.gateway_headers,
        )
        response.raise_for_status()
        torrents = response.json()
        matches = [item for item in torrents if isinstance(item, dict)
                   and item.get('hash', '').lower() == permit.infohash] \
            if isinstance(torrents, list) else []
        if len(matches) != 1:
            raise ValidationError('audited download is not visible')
        torrent = matches[0]
        if (torrent.get('progress') != 1 or torrent.get('amount_left') != 0
                or torrent.get('category') != permit.category
                or torrent.get('save_path', '').rstrip('/') != permit.destination.rstrip('/')):
            raise ValidationError('audited download is not complete at its admitted path')
        raw_content = torrent.get('content_path')
        if not isinstance(raw_content, str):
            raise ValidationError('audited content path is absent')
        content = helper._content_path(permit, raw_content)
        response = await helper.client.get(
            f'{self.gateway_url}/api/v2/torrents/files', params={'hash': permit.infohash},
            headers=self.gateway_headers,
        )
        response.raise_for_status()
        files = response.json()
        if not isinstance(files, list):
            raise ValidationError('audited gateway files are absent')
        observed = {}
        for item in files:
            if (not isinstance(item, dict) or not isinstance(item.get('name'), str)
                    or type(item.get('size')) is not int or item.get('progress') != 1
                    or item['name'] in observed):
                raise ValidationError('audited gateway file vector is invalid')
            observed[item['name']] = item['size']
        expected = {item['path']: item['size_bytes'] for item in job['payload_files']}
        if observed != expected:
            raise ValidationError('audited gateway content vector changed')
        # Verify every content file, including excluded extras. Padding is not exposed by qBit.
        for relative, size in expected.items():
            logical = Path(source_logical_path(permit, relative))
            if not logical.is_relative_to('/data/torrents'):
                raise ValidationError('audited source path escaped torrents')
            raw = helper.torrent_root / logical.relative_to('/data/torrents')
            self._reject_symlink_chain(raw, helper.torrent_root)
            video = helper._source_path(permit, relative)
            if (video.is_symlink() or not video.is_file() or video.stat().st_size != size
                    or (video != content and content not in video.parents)):
                raise ValidationError('audited physical payload changed')
            helper._physical(video, permit)

    @staticmethod
    def _reject_symlink_chain(path, root):
        if not path.is_relative_to(root):
            raise ValidationError('audited path escaped its root')
        while True:
            if path.is_symlink():
                raise ValidationError('audited path contains a symlink')
            if path == root:
                return
            path = path.parent

    async def _get(self, helper, route, **kwargs):
        response = await helper.client.get(
            f'{helper.radarr_url}/api/v3/{route}', headers=helper.radarr_headers, **kwargs,
        )
        response.raise_for_status()
        return response.json()

    async def _native(self, helper, job, target):
        if target['kind'] == 'movie':
            native = await self._get(helper, f"movie/{target['native_movie_id']}")
            if (not isinstance(native, dict) or native.get('id') != target['native_movie_id']
                    or native.get('tmdbId') != job['tmdb_id']):
                raise ValidationError('audited native movie identity changed')
            return native, [native]
        native = await self._get(helper, f"series/{target['native_series_id']}")
        if not isinstance(native, dict):
            raise ValidationError('audited native series identity changed')
        native_tmdb = native.get('tmdbId')
        missing_tmdb = native_tmdb is None or type(native_tmdb) is int and native_tmdb == 0
        tmdb_matches = type(native_tmdb) is int and native_tmdb > 0 and (
            native_tmdb == job['tmdb_id']
        )
        tvdb = job.get('tvdb_id')
        tvdb_matches = type(tvdb) is int and tvdb > 0 and (
            type(native.get('tvdbId')) is int and native['tvdbId'] == tvdb
        )
        # Only the sealed manifest's positive TVDB binding can explain Sonarr's
        # absent/zero TMDB. A conflicting or malformed TMDB never uses fallback.
        if (type(native.get('id')) is not int or native['id'] != target['native_series_id']
                or (tvdb is not None and not tvdb_matches)
                or not (tmdb_matches or missing_tmdb and tvdb_matches)):
            raise ValidationError('audited native series identity changed')
        episodes = []
        for native_id, scope in zip(target['native_episode_ids'], target['scopes'], strict=True):
            episode = await self._get(helper, f'episode/{native_id}')
            match = re.fullmatch(r'S(\d+)E(\d+)', scope)
            if (not isinstance(episode, dict) or episode.get('id') != native_id
                    or episode.get('seriesId') != target['native_series_id']
                    or (episode.get('seasonNumber'), episode.get('episodeNumber'))
                    != (int(match[1]), int(match[2]))):
                raise ValidationError('audited native episode identity changed')
            episodes.append(episode)
        return native, episodes

    def _library_path(self, helper, raw, permit):
        prefix = '/data/media/' + ('movies' if permit.category == 'radarr' else 'tv')
        if (not isinstance(raw, str) or not raw.startswith(prefix + '/')
                or '..' in PurePosixPath(raw).parts):
            raise ValidationError('audited native file path is invalid')
        path = helper.media_root / Path(raw).relative_to(prefix)
        self._reject_symlink_chain(path, helper.media_root)
        if path.is_symlink() or not path.is_file():
            raise ValidationError('audited native file is unavailable')
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(helper.media_root.resolve(strict=True)):
            raise ValidationError('audited native file escaped library')
        helper._physical(resolved, permit)
        return resolved

    async def _readback(self, helper, permit, target, native, episodes, source):
        paths = []
        if target['kind'] == 'movie':
            movie_file = native.get('movieFile')
            if not isinstance(movie_file, dict) or not movie_file.get('relativePath'):
                return None
            relative = PurePosixPath(movie_file['relativePath'])
            if relative.is_absolute() or '..' in relative.parts:
                raise ValidationError('audited movie relative path is invalid')
            paths.append(self._library_path(helper, native['path'] + '/' + str(relative), permit))
        else:
            for episode in episodes:
                if episode.get('hasFile') is not True or not episode.get('episodeFileId'):
                    return None
                file = await self._get(helper, f"episodefile/{episode['episodeFileId']}")
                if not isinstance(file, dict):
                    raise ValidationError('audited native episode file is invalid')
                raw = file.get('path')
                if raw is None and isinstance(file.get('relativePath'), str):
                    relative = PurePosixPath(file['relativePath'])
                    if relative.is_absolute() or '..' in relative.parts:
                        raise ValidationError('audited episode relative path is invalid')
                    raw = native['path'] + '/' + str(relative)
                paths.append(self._library_path(helper, raw, permit))
        if len(set(paths)) != 1:
            raise ValidationError('audited combined episodes point to different files')
        actual = helper._physical(paths[0], permit).stat()
        expected = helper._physical(source, permit).stat()
        if (actual.st_dev, actual.st_ino, actual.st_size) != (
                expected.st_dev, expected.st_ino, expected.st_size):
            raise ValidationError('audited native import is not the exact physical hardlink')
        return paths[0]

    async def _manual_file(self, helper, permit, target, *, logical_source=None):
        logical = logical_source or source_logical_path(permit, target['path'])
        candidates = await self._get(helper, 'manualimport', params={
            # Audited adds need not exist in the native TrackedDownload cache.
            # Discover by folder; the exact pinned hash stays in the import command.
            'folder': str(PurePosixPath(logical).parent),
            'filterExistingFiles': 'false',
        })
        matches = [item for item in candidates if isinstance(item, dict)
                   and item.get('path') == logical] if isinstance(candidates, list) else []
        if len(matches) != 1 or not isinstance(matches[0].get('quality'), dict):
            raise ValidationError('audited exact native manual import metadata is unavailable')
        # Native parsing supplies import descriptors. Approved identity/quality comes from
        # the immutable audited mapping, including operator exceptions and unknown metadata.
        candidate = matches[0]
        payload = {key: candidate[key] for key in (
            'quality', 'languages', 'releaseGroup', 'indexerFlags', 'releaseType',
        ) if key in candidate}
        payload.update(path=logical, folderName=PurePosixPath(logical).parent.name,
                       downloadId=permit.infohash)
        if target['kind'] == 'movie':
            payload['movieId'] = target['native_movie_id']
        else:
            payload.update(seriesId=target['native_series_id'],
                           episodeIds=target['native_episode_ids'])
        return payload

    async def _derived_source(self, row, helper, permit):
        """Publish one recorded inode after bounded stream-copy, retaining originals."""
        import asyncio
        import fcntl
        import os

        from .audited_remux import remux_parts, verify_output

        job, target, file_id = row['job'], row['file'], row['file_id']
        ledger = self.store.derivation(file_id)
        binding = {'job_id': job['job_id'], 'candidate_id': job['candidate_id'],
                   'metadata_sha256': job['metadata_sha256'], 'target': target}
        if (ledger is None or ledger['binding'] != binding
                or ledger['permit_id'] != permit.permit_id
                or ledger['reservation_id'] != permit.reservation_id
                or ledger['pool_id'] != permit.pool_id
                or ledger['filesystem_id'] != permit.filesystem_id
                or not permit.filesystem_id
                or ledger['budget_bytes'] != target['derivation']['max_output_bytes']
                or helper.storage_registry is None or self.capacity_provider is None):
            raise ValidationError('audited derivation reservation is missing or changed')
        logical_dir = permit.destination.rstrip('/') + '/.audited-derived/' + file_id
        directory = helper.torrent_root / Path(logical_dir).relative_to('/data/torrents')
        self._reject_symlink_chain(directory, helper.torrent_root)
        physical = helper.storage_registry.resolve(permit.pool_id, logical_dir, writable=True)
        physical.mkdir(parents=True, mode=0o755, exist_ok=True)
        self._reject_symlink_chain(directory, helper.torrent_root)
        if not directory.is_dir() or directory.is_symlink() \
                or helper._physical(directory, permit) != physical:
            raise ValidationError('audited derived directory is not on the admitted pool')
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
        descriptor = os.open(physical / '.lock', flags, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValidationError('audited derivation is already running') from None
            ledger = self.store.derivation(file_id)
            parts = [helper._physical(helper._source_path(permit, part['path']), permit)
                     for part in target['source_parts']]
            partial = physical / 'output.partial.mp4'
            name = target['scopes'][0] + '.mp4'
            output = physical / name
            if ledger['state'] == 'reserved':
                if (partial.exists() or output.exists()
                        or partial.is_symlink() or output.is_symlink()):
                    raise ValidationError('audited derivation has an unknown existing output')
                # Compatibility probes happen in the child pipeline; failed or unknown
                # writes retain this entire budget and never overwrite a partial.
                self._permit(job)
                self.store.begin_derivation(file_id)
                receipt = await remux_parts(parts, partial, max_bytes=ledger['budget_bytes'])
                if (partial.stat().st_uid, partial.stat().st_gid) != (helper.import_uid,
                                                                      helper.import_gid):
                    os.chown(partial, helper.import_uid, helper.import_gid)
                # Hash/size/inode are unaffected by chown. Store provenance before
                # publishing or submitting any native import.
                receipt['source_paths'] = [part['path'] for part in target['source_parts']]
                receipt['logical_output'] = logical_dir + '/' + name
                receipt['candidate_id'] = job['candidate_id']
                receipt['metadata_sha256'] = job['metadata_sha256']
                self.store.record_derivation_output(file_id, receipt)
                ledger = self.store.derivation(file_id)
            if ledger['state'] == 'writing':
                # A crash or incompatible source cannot justify rerunning into a
                # second allocation, dropping a part, or accepting an unknown file.
                raise ValidationError('audited derivation output requires recovery')
            receipt = ledger['receipt']
            if (not isinstance(receipt, dict)
                    or receipt.get('logical_output') != logical_dir + '/' + name
                    or receipt.get('source_paths') != [p['path'] for p in target['source_parts']]
                    or receipt.get('candidate_id') != job['candidate_id']
                    or receipt.get('metadata_sha256') != job['metadata_sha256']):
                raise ValidationError('audited derivation provenance changed')
            recorded = output if output.exists() else partial
            await asyncio.to_thread(verify_output, parts, recorded, receipt, ledger['budget_bytes'])
            if not output.exists():
                os.link(partial, output)  # Exclusive publication of the recorded inode.
                fd = os.open(physical, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            await asyncio.to_thread(verify_output, parts, output, receipt, ledger['budget_bytes'])
            local = directory / name
            if helper._physical(local, permit) != output:
                raise ValidationError('audited derived output moved to another pool')
            self._permit(job)
            if ledger['state'] != 'published':
                evidence = await self.capacity_provider()
                pool = evidence.pool(permit.pool_id)
                sample = helper.storage_registry.inspect(permit.pool_id, writable=True)
                if (not pool.pools or pool.pools[0].filesystem_id != permit.filesystem_id
                        or sample.filesystem_id != permit.filesystem_id):
                    raise ValidationError('audited derivation fresh physical evidence missing')
                self.store.publish_derivation(file_id, receipt)
            return local, receipt['logical_output']
        finally:
            os.close(descriptor)

    async def _finalize(self, row):
        job, target, file_id = row['job'], row['file'], row['file_id']
        helper = self.helpers.get(job['category'])
        if helper is None:
            raise ValidationError('audited native client is not configured')
        permit = self._permit(job)
        await self._download(helper, permit, job)
        if 'source_parts' in target:
            source, logical_source = await self._derived_source(row, helper, permit)
        else:
            source = helper._source_path(permit, target['path'])
            logical_source = None
        if source.suffix.lower() not in {'.mkv', '.mp4', '.m4v', '.avi', '.mov'}:
            raise ValidationError('audited target is not a video')
        probe = media_probe.probe_media(source)
        if not probe.audio_languages:
            raise ValidationError('audited target has no audio stream')
        # Positive dimensions are validated by ffprobe; no new quality gate is introduced.
        native, episodes = await self._native(helper, job, target)
        imported = await self._readback(helper, permit, target, native, episodes, source)
        if imported is None:
            if row['state'] != 'planned':
                self.store.defer(file_id, 'native_import_readback_pending')
                return
            response = await self._get(helper, 'config/mediamanagement')
            if (not isinstance(response, dict) or response.get('copyUsingHardlinks') is not True
                    or response.get('importExtraFiles') is True):
                raise ValidationError('audited native hardlink import is not enabled')
            directory = helper._import_directory(permit, native.get('path'))
            if not helper._can_hardlink_video(source, permit=permit, directory=directory):
                raise ValidationError('audited hardlink is unavailable for native UID/GID')
            payload = await self._manual_file(helper, permit, target, logical_source=logical_source)
            # Recheck operator cancellation/placement immediately before committing intent.
            self._permit(job)
            if not self.store.claim_import(file_id):
                return
            response = await helper.client.post(
                f'{helper.radarr_url}/api/v3/command', headers=helper.radarr_headers,
                json={'name': 'ManualImport', 'importMode': 'Copy', 'files': [payload]},
            )
            response.raise_for_status()
            command = response.json()
            if not isinstance(command, dict) or type(command.get('id')) is not int:
                raise ValidationError('audited native command receipt is unknown')
            self.store.record_requested(file_id, command['id'])
            native, episodes = await self._native(helper, job, target)
            imported = await self._readback(helper, permit, target, native, episodes, source)
            if imported is None:
                self.store.defer(file_id, 'native_import_readback_pending')
                return
        if row['state'] not in {'imported', 'subtitle_pending'}:
            # Publish only after exact native inode readback and an accepted
            # Jellyfin notification. Failure leaves the existing native command
            # retryable without reimporting or repeating subtitle-cycle updates.
            await helper._notify_jellyfin(imported)
            self.store.mark_imported(file_id, str(imported), probe={
                'width': probe.width, 'height': probe.height,
                'audio_languages': list(probe.audio_languages),
                'duration_seconds': probe.duration_seconds,
            })
        await self._subtitles(row, helper, permit, native, imported, probe)

    async def _subtitles(self, row, helper, permit, native, imported, probe):
        job, target, file_id = row['job'], row['file'], row['file_id']
        policy = helper._subtitle_policy_for(
            native, profile_name=job.get('subtitle_policy', {}).get('profile_name'),
        )
        if policy.waives_subtitles(probe):
            self.store.complete(file_id)
            return

        def present(language):
            if (language == 'EN' and policy.allow_generic_english
                    and has_embedded_english_subtitle(probe)):
                return True
            matcher = (is_brazilian_portuguese_subtitle if language == 'BR_PT'
                       else is_english_subtitle)
            prefix = imported.stem + '.'
            for path in imported.parent.iterdir():
                if not path.name.startswith(prefix) or path.suffix.lower() != '.srt':
                    continue
                tags = path.name[len(prefix):]
                if (not path.is_symlink() and policy.matches(tags) and matcher(tags)
                        and _subtitle_has_content(path) and valid_srt(path.read_bytes())):
                    helper._physical(path, permit)
                    return True
            if target['kind'] == 'movie':
                preferred = 'pt-BR' if language == 'BR_PT' else 'en-US'
                return helper._installed_bazarr_subtitle(
                    imported, permit, policy, preferred,
                ) is not None
            return False

        cached = row.get('subtitle_content')
        language = row.get('subtitle_language')
        if cached is not None and language is not None:
            self._install_subtitle(helper, permit, imported, cached, language)
            self.store.complete(file_id)
            return

        async def fetch(language, match_mode):
            if helper.subtitle_source is None:
                return None
            release = target.get('subtitle_release_title') or PurePosixPath(target['path']).stem
            if target['kind'] == 'episode':
                scope = re.fullmatch(r'S(\d+)E(\d+)', target['scopes'][0])
                return await helper.subtitle_source.fetch(
                    tmdb_id=job['tmdb_id'], release_title=release,
                    season=int(scope[1]), episode=int(scope[2]), language=language,
                    match_mode=match_mode,
                )
            return await helper.subtitle_source.fetch_movie(
                tmdb_id=job['tmdb_id'], release_titles=[release, job['release_title']],
                language=language, match_mode=match_mode,
                movie_duration_seconds=probe.duration_seconds,
            )

        recovered = await recover_subtitle(policy, fetch, already_present=present)
        if recovered is None:
            self.store.subtitle_pending(file_id)
            return
        if recovered.match_mode != 'present':
            self.store.cache_subtitle(file_id, recovered.language, recovered.content)
            self._install_subtitle(helper, permit, imported, recovered.content, recovered.language)
        self.store.complete(file_id)

    @staticmethod
    def _install_subtitle(helper, permit, video, content, language):
        # The imported file has already been verified on its admitted physical pool.
        admitted_video = helper._physical(video, permit)
        _write_external_subtitle(admitted_video, content, admitted_video.parent, language=language)
        suffix = 'pt-BR' if language == 'BR_PT' else 'en'
        target = video.with_name(f'{video.stem}.{suffix}.srt')
        physical = helper._physical(target, permit)
        if os.geteuid() == 0:
            os.chown(physical, helper.import_uid, helper.import_gid)
