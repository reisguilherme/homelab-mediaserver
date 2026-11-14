"""Native Jellyfin contracts for filling missing collection posters safely."""

import copy
import json

import httpx
import pytest

POSTER = "https://image.tmdb.org/t/p/original/collection.jpg"


class JellyfinFixture:
    def __init__(self):
        self.items = [{
            "Id": "collection", "Type": "BoxSet", "Name": "Owner's collection",
            "LockData": True, "LockedFields": ["Name"], "ProviderIds": {"Other": "keep"},
            "ImageTags": {}, "Overview": "Owner's description", "Genres": ["Action"],
            "Tags": ["custom"], "Studios": [{"Name": "Studio"}], "People": [],
            "PreferredMetadataLanguage": "pt-BR", "ProductionYear": 2006,
        }]
        self.members = [
            {"Id": "movie1", "Type": "Movie", "ProviderIds": {"TmdbCollection": "645"}},
            {"Id": "movie2", "Type": "Movie", "ProviderIds": {"TmdbCollection": "645"}},
        ]
        self.users = [{"Id": "admin", "Name": "owner"}]
        self.results = [{"ProviderIds": {"Tmdb": "645"}, "ImageUrl": POSTER}]
        self.requests = []
        self.accept_update = True
        self.accept_download = True
        self.fail_item = None
        self.manual_on_read = None
        self.reads = 0
        self.page_size = None

    def __call__(self, request):
        self.requests.append(request)
        assert 'Token="fixture-private-token"' in request.headers["Authorization"]
        assert "X-Emby-Token" not in request.headers
        path = request.url.path
        if path == "/Users":
            return httpx.Response(200, json=self.users)
        if path == "/Items":
            assert request.url.params["IncludeItemTypes"] == "BoxSet"
            start = int(request.url.params["StartIndex"])
            count = self.page_size or int(request.url.params["Limit"])
            return httpx.Response(200, json={
                "Items": self.items[start:start + count], "TotalRecordCount": len(self.items),
            })
        if path == "/Users/admin/Items":
            assert request.url.params["ParentId"] in {i["Id"] for i in self.items}
            assert request.url.params["Fields"] == "ProviderIds"
            start = int(request.url.params["StartIndex"])
            return httpx.Response(200, json={
                "Items": self.members[start:start + 1000], "TotalRecordCount": len(self.members),
            })
        if path.startswith("/Users/admin/Items/"):
            item_id = path.rsplit("/", 1)[1]
            if self.fail_item == item_id:
                return httpx.Response(503)
            self.reads += 1
            item = next(i for i in self.items if i["Id"] == item_id)
            if self.manual_on_read == self.reads:
                item["ImageTags"]["Primary"] = "owner-manual-image"
            return httpx.Response(200, json=item)
        if path == "/Items/RemoteSearch/BoxSet":
            body = json.loads(request.content)
            assert body["SearchInfo"]["ProviderIds"] == {"Tmdb": "645"}
            assert body["IncludeDisabledProviders"] is True
            return httpx.Response(200, json=self.results)
        item = next(i for i in self.items if path.startswith("/Items/" + i["Id"]))
        if path == "/Items/" + item["Id"] and request.method == "POST":
            if self.accept_update:
                item.update(json.loads(request.content))
            return httpx.Response(204)
        if path.endswith("/RemoteImages/Download"):
            assert request.method == "POST"
            assert request.url.params["type"] == "Primary"
            assert request.url.params["imageUrl"] == POSTER
            if self.accept_download:
                item["ImageTags"]["Primary"] = "official-poster"
            return httpx.Response(204)
        raise AssertionError(f"Unexpected native request {request.method} {path}")


def reconciler(client, **kwargs):
    from homeserver_control.worker.jellyfin_collections import JellyfinCollectionReconciler

    return JellyfinCollectionReconciler(
        client, base_url="http://jellyfin:8096", api_key="fixture-private-token",
        admin_username="owner", interval_seconds=60, **kwargs,
    )


def writes(native):
    return [r for r in native.requests if r.method != "GET"
            and r.url.path != "/Items/RemoteSearch/BoxSet"]


@pytest.mark.asyncio
async def test_missing_poster_gets_exact_member_identity_without_changing_owner_metadata():
    native = JellyfinFixture()
    before = copy.deepcopy(native.items[0])
    async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
        report = await reconciler(client).reconcile()
    assert report["repaired"] == 1
    assert report["missing_primary"] == 1
    assert native.items[0] == {
        **before, "ProviderIds": {"Other": "keep", "Tmdb": "645"},
        "ImageTags": {"Primary": "official-poster"},
    }
    assert [r.url.path for r in writes(native)] == [
        "/Items/collection", "/Items/collection/RemoteImages/Download",
    ]
    assert "fixture-private-token" not in json.dumps(report)
    assert POSTER not in json.dumps(report)


@pytest.mark.asyncio
async def test_existing_manual_poster_is_preserved_without_metadata_writes():
    native = JellyfinFixture()
    native.items[0]["ImageTags"] = {"Primary": "manual"}
    before = copy.deepcopy(native.items)
    async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
        report = await reconciler(client).reconcile()
    assert native.items == before
    assert writes(native) == []
    assert report["preserved"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("members", [
    [],
    [{"Type": "Movie", "ProviderIds": {}}],
    [{"Type": "Movie", "ProviderIds": {"TmdbCollection": "0"}}],
    [{"Type": "Movie", "ProviderIds": {"TmdbCollection": "-1"}}],
    [{"Type": "Movie", "ProviderIds": {"TmdbCollection": True}}],
    [{"Type": "Episode", "ProviderIds": {"TmdbCollection": "645"}}],
    [{"Type": "Movie", "ProviderIds": {"TmdbCollection": "645"}},
     {"Type": "Movie", "ProviderIds": {"TmdbCollection": "264"}}],
    [{"Type": "Movie", "ProviderIds": {"TmdbCollection": "645"}},
     {"Type": "Movie", "ProviderIds": {}}],
])
async def test_unresolved_or_conflicting_members_never_write(members):
    native = JellyfinFixture()
    native.members = [dict(item, Id=str(i)) for i, item in enumerate(members)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
        report = await reconciler(client).reconcile()
    assert report["unresolved"] == 1
    assert writes(native) == []


@pytest.mark.asyncio
async def test_existing_conflicting_collection_identity_is_not_replaced():
    native = JellyfinFixture()
    native.items[0]["ProviderIds"]["Tmdb"] = "264"
    async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
        report = await reconciler(client).reconcile()
    assert report["unresolved"] == 1
    assert writes(native) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("results", [
    [], [{"ProviderIds": {"Tmdb": "264"}, "ImageUrl": POSTER}],
    [{"ProviderIds": {"Tmdb": "645"}, "ImageUrl": None}],
    [{"ProviderIds": {"Tmdb": "645"}, "ImageUrl": "http://internal/secret"}],
    [{"ProviderIds": {"Tmdb": "645"}, "ImageUrl": "https://attacker/t/p/a.jpg"}],
    [{"ProviderIds": {"Tmdb": "645"}, "ImageUrl": POSTER}] * 2,
])
async def test_missing_ambiguous_or_non_tmdb_art_never_mutates_identity(results):
    native = JellyfinFixture()
    native.results = results
    async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
        report = await reconciler(client).reconcile()
    assert report["unresolved"] == 1
    assert writes(native) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("read", [2, 3])
async def test_manual_poster_added_during_reconciliation_is_not_overwritten(read):
    native = JellyfinFixture()
    native.manual_on_read = read
    async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
        report = await reconciler(client).reconcile()
    assert native.items[0]["ImageTags"]["Primary"] == "owner-manual-image"
    assert report["preserved"] == 1
    assert all(not r.url.path.endswith("/RemoteImages/Download") for r in writes(native))


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "download"])
async def test_native_acknowledgement_requires_readback_before_reporting_repair(operation):
    native = JellyfinFixture()
    setattr(native, "accept_" + operation, False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
        report = await reconciler(client).reconcile()
    assert report["failed"] == 1
    assert report["repaired"] == 0
    assert "Primary" not in native.items[0]["ImageTags"]
    if operation == "update":
        assert all(not r.url.path.endswith("/RemoteImages/Download") for r in writes(native))


@pytest.mark.asyncio
async def test_interval_throttles_requests_and_future_new_collection_is_repaired():
    native = JellyfinFixture()
    now = [0.0]
    async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
        worker = reconciler(client, clock=lambda: now[0])
        await worker.reconcile()
        count = len(native.requests)
        now[0] = 59.0
        assert (await worker.reconcile())["status"] == "waiting"
        assert len(native.requests) == count
        native.items.append(dict(copy.deepcopy(native.items[0]), Id="new", ImageTags={}))
        now[0] = 60.0
        report = await worker.reconcile()
    assert report["collections"] == 2
    assert report["preserved"] == 1
    assert report["repaired"] == 1
    assert native.items[1]["ImageTags"]["Primary"] == "official-poster"


@pytest.mark.asyncio
async def test_every_collection_page_is_considered_and_one_failure_does_not_stop_others():
    native = JellyfinFixture()
    native.items.append(dict(copy.deepcopy(native.items[0]), Id="next"))
    native.page_size = 1
    native.fail_item = "collection"
    async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
        report = await reconciler(client).reconcile()
    assert report["collections"] == 2
    assert report["failed"] == 1
    assert report["repaired"] == 1
    assert native.items[1]["ImageTags"]["Primary"] == "official-poster"


@pytest.mark.asyncio
async def test_ambiguous_owner_account_never_writes():
    native = JellyfinFixture()
    native.users.append({"Id": "duplicate", "Name": "owner"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
        report = await reconciler(client).reconcile()
    assert report["status"] == "unavailable"
    assert writes(native) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("LockData", None), ("LockData", "true"), ("Name", None), ("ImageTags", None),
])
async def test_incomplete_native_collection_dto_cannot_unlock_or_overwrite_metadata(field, value):
    native = JellyfinFixture()
    native.items[0][field] = value
    async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
        report = await reconciler(client).reconcile()
    assert report["failed"] == 1
    assert report["repaired"] == 0
    assert writes(native) == []
